"""Editing suggestions: a model proposes a direction, the app renders it, the model checks its own work.

Vision models are good at saying which way a photo should move ("open the
shadows, crop the clutter on the right") and poor at exact numbers, because
they cannot see the result. So a suggestion is a set of small directional
steps, turned into slider values here, and then the before/after pair goes
back to the model for one or two corrections.

The on-device model does this for free. The external model contributes twice:
its suggestion for each finalist rides along in the finals call (no extra
upload), and it can review an edit on request.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from PIL import Image

from . import contest, editing, imaging, local_vlm
from .cloud import get_provider, Img
from .context import Ctx

EDIT_PX = 1024  # working size for suggestions and live preview

# direction keys the models use -> (recipe key, value of one step)
STEPS: dict[str, tuple[str, float]] = {
    "exposure": ("exposure", 0.2),
    "contrast": ("contrast", 10),
    "highlights": ("highlights", 15),
    "shadows": ("shadows", 15),
    "warmth": ("temperature", 8),
    "saturation": ("vibrance", 10),
    "clarity": ("clarity", 10),
    "vignette": ("vignette", -10),
}
STRENGTH = {1: 0.6, 2: 0.85, 3: 1.0}  # share of a measured shortfall that a step of this size corrects
SIGNED = ["exposure", "contrast", "highlights", "shadows", "warmth", "saturation"]  # -3..3
UNSIGNED = ["clarity", "vignette"]  # 0..3
CROP = ["crop_left", "crop_top", "crop_right", "crop_bottom"]

# How far a model's suggestion may go. The sliders themselves allow more.
LIMITS = {"exposure": 1.2, "contrast": 45, "highlights": 60, "shadows": 60, "temperature": 30, "vibrance": 35,
          "clarity": 30, "vignette": 30}  # fmt: skip


def capped(recipe: dict) -> dict:
    out = dict(recipe)
    for k, lim in LIMITS.items():
        if k in out:
            out[k] = min(max(out[k], -lim), lim)
    return editing.clean(out)


ZH = {
    "exposure": "曝光", "contrast": "對比", "highlights": "亮部", "shadows": "暗部", "warmth": "色溫",
    "saturation": "飽和度", "clarity": "清晰度", "vignette": "暗角",
}  # fmt: skip

ADJUST_PROPS = {
    **{k: {"type": "integer"} for k in SIGNED + UNSIGNED},
    "black_white": {"type": "boolean"},
    "straighten": {"type": "boolean"},
    **{k: {"type": "integer"} for k in CROP},
}
ADJUST_SCHEMA = {
    "type": "object",
    "properties": ADJUST_PROPS,
    "required": list(ADJUST_PROPS),
    "additionalProperties": False,
}

ADJUST_GUIDE = (
    "每個調整項目用整數表示方向與幅度：0 是不調整，±1 輕微，±2 明顯，±3 很強。\n"
    "- exposure 曝光（-3～3）：正值變亮\n"
    "- contrast 對比（-3～3）\n"
    "- highlights 亮部（-3～3）：負值壓暗亮部，救回過亮的細節\n"
    "- shadows 暗部（-3～3）：正值提亮暗部\n"
    "- warmth 色溫（-3～3）：正值偏暖，負值偏冷\n"
    "- saturation 飽和度（-3～3）\n"
    "- clarity 清晰度（0～3）：增加質感與立體感\n"
    "- vignette 暗角（0～3）：四周壓暗，把視線引向中央\n"
    "- black_white：轉成黑白是否更好\n"
    "- straighten：畫面明顯歪斜時給 true\n"
    "- crop_left、crop_top、crop_right、crop_bottom：裁切後要保留的範圍，用百分比表示。"
    "crop_left 與 crop_top 是保留區域左上角的位置，crop_right 與 crop_bottom 是右下角的位置。"
    "不需要裁切就給 0、0、100、100。"
)

SUGGEST_SYSTEM = (
    "你是資深的修圖師，替攝影師的照片決定後製方向。只能做整體調整、裁切和轉正，"
    "不能移除、添加或改變畫面裡的任何東西。\n"
    "修圖要克制。曝光和顏色已經正常的照片不需要後製：全部給 0，並在 summary 說明不需要調整。"
    "只處理看得出來的問題，通常動一到三項就夠了，其他項目給 0。不要為了調整而調整。\n"
    "只輸出 JSON，文字用繁體中文。"
)


def _label(what: str) -> str:
    """Models sometimes answer with the field name instead of words; show people the words."""
    names = {**ZH, "straighten": "轉正", "crop": "裁切", "black_white": "轉成黑白"}
    return names.get(what.strip().lower(), what.strip())[:60]


def _int(v, lo: int, hi: int) -> int:
    try:
        return int(min(max(round(float(v)), lo), hi))
    except (TypeError, ValueError):
        return 0


def intents_to_recipe(d: dict, st: dict | None = None) -> dict:
    """Directional steps from a model -> a recipe in slider units."""
    r: dict = {}
    need = (st or {}).get("need", {})
    for k in SIGNED:
        key, unit = STEPS[k]
        n = _int(d.get(k), -3, 3)
        v = n * unit
        # The model picks the direction; where measurement agrees, measurement sets how far to go.
        want = need.get(k, 0)
        if n and want and (n > 0) == (want > 0):
            reach = want * STRENGTH[abs(n)]
            v = reach if abs(reach) > abs(v) else v
        r[key] = v
    for k in UNSIGNED:
        key, unit = STEPS[k]
        r[key] = _int(d.get(k), 0, 3) * unit
    if d.get("black_white") is True:
        r["bw"] = True
        r.pop("vibrance", None)
    # A model can tell that a picture is crooked but not by how much: the angle comes from measurement.
    if d.get("straighten") is True and st and st.get("tilt_confidence", 0) >= 0.6 and 0.4 <= abs(st.get("tilt", 0)) <= 6:
        r["rotate"] = st["tilt"]
    l, t, rgt, b = (_int(d.get(k), 0, 100) / 100.0 for k in CROP)
    if rgt - l >= 0.5 and b - t >= 0.5:  # anything tighter is more likely a misread than a crop
        r["crop"] = {"left": l, "top": t, "right": rgt, "bottom": b}
    return capped(r)


def describe(recipe: dict) -> str:
    """The current edit in words, for the review prompt."""
    back = {key: (k, unit) for k, (key, unit) in STEPS.items()}
    parts = []
    for key, v in recipe.items():
        if key in back:
            k, unit = back[key]
            parts.append(f"{ZH[k]} {v / unit:+.1f}")
    if recipe.get("bw"):
        parts.append("轉黑白")
    if "rotate" in recipe:
        parts.append(f"轉正 {recipe['rotate']:+.1f}°")
    if "crop" in recipe:
        c = recipe["crop"]
        parts.append(f"裁切保留 {c['left']:.0%}～{c['right']:.0%}（左右）、{c['top']:.0%}～{c['bottom']:.0%}（上下）")
    return "、".join(parts) or "沒有調整"


def _measured(st: dict) -> str:
    """The measurements in plain words. Only what is out of the ordinary is called out."""
    need = st.get("need", {})
    notes = [f"平均亮度 {st['brightness']}（0～100，36～64 算正常）"]
    if st.get("dark_scene"):
        notes.append(f"這是{st['dark_scene']}，畫面暗是正常的，不要為了數據而提亮")
    if "exposure" in need:
        notes.append(f"整體明顯偏{'暗' if need['exposure'] > 0 else '亮'}，大約差 {abs(need['exposure']):.1f} 級曝光")
    if "contrast" in need:
        notes.append("明暗範圍很窄，畫面偏灰")
    if st["blown"] >= 3:
        notes.append(f"{st['blown']}% 的區域過亮而失去細節")
    if st["crushed"] >= 8:
        notes.append(f"{st['crushed']}% 的區域全黑")
    return "、".join(notes)


def _suggest_prompt(st: dict, brief: dict | None) -> str:
    tilt = "沒有量到明顯的歪斜"
    if st["tilt_confidence"] >= 0.6 and abs(st["tilt"]) >= 0.4:
        tilt = f"畫面可能歪了 {abs(st['tilt']):.1f} 度"
    ctx = ""
    if brief and brief.get("theme"):
        ctx = f"這張照片要參加攝影比賽，主題是「{brief['theme']}」。後製應該加強它和主題有關的氛圍。\n"
    return (
        f"{ctx}請看這張照片，決定後製的方向。\n\n"
        f"量測數據（參考用，夜景或刻意的暗調不需要照數據修正）：{_measured(st)}、{tilt}。\n\n"
        f"{ADJUST_GUIDE}\n"
        "- summary：一句話說明這張照片的後製方向；不需要後製就直接說\n"
        "- steps：你實際調整的項目（最多 3 項），what 寫做什麼，why 用一句話寫原因；沒有調整就給空陣列"
    )


SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        **ADJUST_PROPS,
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"what": {"type": "string"}, "why": {"type": "string"}},
                "required": ["what", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", *ADJUST_PROPS, "steps"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["good", "more", "less", "worse"]},
        **{k: {"type": "integer"} for k in SIGNED},
        "comment": {"type": "string"},
    },
    "required": ["verdict", *SIGNED, "comment"],
    "additionalProperties": False,
}

REVIEW_SYSTEM = (
    "你是資深的修圖師，正在檢查一張照片修圖前後的差異。要誠實：修過頭、顏色不自然、"
    "或其實原圖比較好，都要直接說。只輸出 JSON，文字用繁體中文。"
)


def _review_prompt(recipe: dict, before: dict, after: dict) -> str:
    return (
        "這張圖左半邊是原圖，右半邊是修圖後的結果。\n"
        f"目前套用的調整：{describe(recipe)}。\n"
        f"原圖的量測：{_measured(before)}。\n修圖後的量測：{_measured(after)}。\n\n"
        "請比較左右兩邊，並參考量測數據（夜景或刻意的暗調除外）：\n"
        "- verdict：good（曝光正常、顏色自然，不需要再動）、more（方向對，但修圖後仍然明顯太暗、太亮、太灰或太淡）、"
        "less（修過頭，不自然）、worse（比原圖差）\n"
        "- exposure、contrast、highlights、shadows、warmth、saturation：在目前的設定上還要再加減多少，"
        "-2～2 的整數，0 表示維持不變\n"
        "- comment：一句話說明你的判斷"
    )


def apply_review(recipe: dict, rv: dict, weight: float = 1.0, need: dict | None = None) -> dict:
    """Nudge a recipe by a reviewer's corrections. A 'worse' verdict halves the whole edit instead."""
    verdict = rv.get("verdict")
    out = dict(recipe)
    if verdict == "good":
        return editing.clean(out)
    if verdict == "worse":
        for key, v in list(out.items()):
            if key in editing.PARAMS and key != "rotate":
                out[key] = v * 0.5
        return editing.clean(out)
    for k in SIGNED:
        key, unit = STEPS[k]
        if out.get("bw") and key == "vibrance":
            continue
        n = _int(rv.get(k), -2, 2)
        delta = n * unit * weight
        # Same rule as the first pass: a measured shortfall in the same direction sets the size of the step.
        want = (need or {}).get(k, 0)
        if n and want and (n > 0) == (want > 0):
            delta = max(abs(delta), abs(want) * STRENGTH[abs(n)]) * (1 if n > 0 else -1)
        if delta:
            out[key] = out.get(key, 0) + delta
    return capped(out)


# ------------------------------------------------------------------ storage


def load(ctx: Ctx, pid: int) -> dict:
    r = ctx.db.one("SELECT recipe, suggested FROM edits WHERE photo_id=?", (pid,))
    return {
        "recipe": json.loads(r["recipe"]) if r and r["recipe"] else {},
        "suggested": json.loads(r["suggested"]) if r and r["suggested"] else {},
    }


def save_recipe(ctx: Ctx, pid: int, recipe: dict) -> dict:
    r = editing.clean(recipe)
    ctx.db.run(
        "INSERT INTO edits(photo_id, recipe, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(photo_id) DO UPDATE SET recipe=excluded.recipe, updated_at=excluded.updated_at",
        (pid, json.dumps(r) if r else None, time.time()),
    )
    return r


def store_suggestion(ctx: Ctx, pid: int, source: str, suggestion: dict) -> None:
    cur = load(ctx, pid)["suggested"]
    cur[source] = suggestion
    ctx.db.run(
        "INSERT INTO edits(photo_id, suggested, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(photo_id) DO UPDATE SET suggested=excluded.suggested, updated_at=excluded.updated_at",
        (pid, json.dumps(cur, ensure_ascii=False), time.time()),
    )


# Scenes that are meant to be dark: measured "too dark" must not push them brighter.
DARK_BY_NATURE = {"night": "夜景", "sunset": "日出或日落", "event": "表演或活動現場"}


def photo_stats(ctx: Ctx, pid: int, base: Image.Image) -> dict:
    """Measurements for one photo, minus the ones its subject makes meaningless."""
    st = editing.stats(base)
    cat = ctx.db.val("SELECT category FROM photos WHERE id=?", (pid,))
    if cat in DARK_BY_NATURE and st["need"].get("exposure", 0) > 0:
        st["need"] = {k: v for k, v in st["need"].items() if k != "exposure"}
        st["dark_scene"] = DARK_BY_NATURE[cat]
    return st


def base_image(ctx: Ctx, pid: int, px: int = EDIT_PX) -> Image.Image:
    im = Image.open(ctx.preview(pid, 1600 if px > 1024 else 1024))
    im.load()
    return im


# --------------------------------------------------------------- suggestions


def suggest_local(ctx: Ctx, pid: int, rounds: int = 1, contest_id: int | None = None,
                  progress: Callable[[str], None] = lambda msg: None) -> dict:  # fmt: skip
    """The on-device model proposes an edit, then reviews its own before/after. Costs no tokens."""
    vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge")
    base = base_image(ctx, pid)
    st = photo_stats(ctx, pid, base)
    brief = contest.get(ctx, contest_id)["brief"] if contest_id else None
    px = ctx.settings.local.image_px
    progress("地端模型正在看照片…")
    d = vlm.chat_json(SUGGEST_SYSTEM, _suggest_prompt(st, brief), [imaging.jpeg_bytes(imaging.resized(base, px))],
                      SUGGEST_SCHEMA, max_tokens=1200)  # fmt: skip
    recipe = intents_to_recipe(d, st)
    history = []
    for i in range(max(rounds, 0)):
        if not recipe:
            break
        progress(f"地端模型正在檢查修圖結果（第 {i + 1} 輪）…")
        edited = editing.render(base, recipe)
        pair = editing.side_by_side(base, edited)
        try:
            rv = vlm.chat_json(REVIEW_SYSTEM, _review_prompt(recipe, st, editing.tone_stats(edited)),
                               [imaging.jpeg_bytes(pair, 88)], REVIEW_SCHEMA, max_tokens=500)  # fmt: skip
        except local_vlm.LocalModelError:
            break  # the first proposal still stands
        new = apply_review(recipe, rv, weight=1.0 if i == 0 else 0.5, need=editing.tone_stats(edited)["need"])
        history.append({"verdict": rv.get("verdict"), "comment": str(rv.get("comment", ""))[:200], "changed": new != recipe})
        recipe = new
        if rv.get("verdict") in ("good", "worse"):
            break
    out = {
        "source": "local",
        "model": vlm.model,
        "summary": str(d.get("summary", "")).strip()[:200],
        "steps": [
            {"what": _label(str(s.get("what", ""))), "why": str(s.get("why", ""))[:160]}
            for s in (d.get("steps") or [])[:4]
            if isinstance(s, dict)
        ],
        "recipe": recipe,
        "reviews": history,
        "at": time.time(),
    }
    store_suggestion(ctx, pid, "local", out)
    return out


def review_cloud(ctx: Ctx, pid: int, recipe: dict) -> dict:
    """One external-model look at a before/after pair. Uploads a single small composite image."""
    recipe = editing.clean(recipe)
    base = base_image(ctx, pid)
    edited = editing.render(base, recipe)
    pair = editing.side_by_side(base, edited, height=560)
    prov = get_provider(ctx.settings)
    prompt = _review_prompt(recipe, editing.tone_stats(base), editing.tone_stats(edited))
    res = prov.generate(system=REVIEW_SYSTEM, parts=[prompt, Img(imaging.jpeg_bytes(pair, 86))],
                        schema=REVIEW_SCHEMA, max_tokens=4000)  # fmt: skip
    contest.log_call(ctx, res, prov.name, "retouch_review", None)
    return {
        "verdict": res.data.get("verdict"),
        "comment": str(res.data.get("comment", ""))[:300],
        "recipe": apply_review(recipe, res.data, need=editing.tone_stats(edited)["need"]),
        "tokens": res.input_tokens + res.output_tokens,
        "model": res.model,
    }


def store_cloud_adjust(ctx: Ctx, pid: int, adjust: dict, summary: str, model: str) -> dict | None:
    """Keeps the finals jury's edit for a photo so it is waiting in the editor."""
    if not isinstance(adjust, dict):
        return None
    base = base_image(ctx, pid)
    recipe = intents_to_recipe(adjust, photo_stats(ctx, pid, base))
    out = {"source": "cloud", "model": model, "summary": summary[:300], "steps": [], "recipe": recipe, "reviews": [], "at": time.time()}
    store_suggestion(ctx, pid, "cloud", out)
    return out
