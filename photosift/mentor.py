"""Mentor mode: how to take the next picture better.

The rest of the app answers "which of these is best". This part answers "what
should I do differently next time", at four distances:

    one photo      what the settings did, what to dial in instead, where to stand
    one outing     what kept going wrong and right on that day
    the library    habits that only show up across hundreds of pictures
    next outing    see shootplan.py

The division of labour is the same as everywhere else. Numbers come from
measurement and rules (shooting.py, sun.py) and cost nothing. The on-device
model looks at single photos. The external model is called once per review,
with one contact sheet and the statistics, to say what a teacher would.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import mean

from PIL import Image

from . import contest, editing, imaging, ingest, local_vlm, shooting, sun, taxonomy
from .cloud import CloudError, Img, estimate_image_tokens, get_provider
from .context import Ctx

COLS = (
    "p.id, p.filename, p.camera, p.lens, p.focal, p.focal35, p.aperture, p.shutter_s, p.iso, p.exp_program, p.exp_bias, "
    "p.metering, p.flash, p.wb, p.heading, p.tilt, p.category, p.flags, p.sharp, p.tech, p.aesthetic, p.vlm, p.vlm_score, "
    "p.caption, p.sun_elev, p.light, p.lat, p.lon, p.tz_offset, p.taken_at, p.taken_ts, p.time_source, p.width, p.height, "
    "p.group_id, p.group_size, p.is_best, p.event_id, p.rating, p.flag"
)
SHEET_PHOTOS = 12     # one contact sheet: what the external model sees of a review
MIN_GROUP = 4         # fewer photos than this on either side and a comparison is not reported
MIN_PROGRESS = 5      # new photos needed before an exercise is judged
TILT_LEAN = 1.0       # degrees

KEY_ZH = {
    "slow_shutter": "快門太慢造成模糊", "slow_shutter_risk": "快門低於安全快門", "focus_miss": "對焦沒有落在主體上",
    "long_shake": "長曝光晃動", "iso_high": "ISO 比需要的高", "diffraction": "光圈縮得太小", "nd_filter": "靠縮光圈拉長曝光",
    "wide_open_scene": "風景用大光圈拍", "stopped_down_portrait": "人像的光圈縮得太小", "overexposed": "亮部過曝",
    "underexposed": "曝光不足", "post_exposure": "事後才在後製修正曝光", "auto_mode": "全自動模式做了不理想的決定",
    "tilt": "水平歪斜", "heavy_crop": "後製大幅裁切", "midday": "在正午的頂光下拍", "near_golden": "差一點就趕上黃金時刻",
    "flash_far": "對遠景用閃光燈", "steady": "慢速快門也拿得穩", "good_aperture": "風景的光圈選得好", "golden": "在黃金或藍調時刻拍",
}  # fmt: skip
SCORE_ZH = {"composition": "構圖", "lighting": "光線", "color": "色彩", "subject": "主體明確度", "story": "故事性與意境", "overall": "整體印象"}
FOCAL = [(0, 24, "超廣角（24mm 以下）"), (24, 35, "廣角（24–35mm）"), (35, 70, "標準（35–70mm）"), (70, 135, "中長焦（70–135mm）"),
         (135, 1e9, "望遠（135mm 以上）")]  # fmt: skip
APERTURE = [(0, 2.3, "f/2 以下"), (2.3, 4.6, "f/2.8–f/4"), (4.6, 9.5, "f/5.6–f/8"), (9.5, 99, "f/11 以上")]
ISO = [(0, 250, "ISO 200 以下"), (250, 900, "ISO 400–800"), (900, 3600, "ISO 1600–3200"), (3600, 1e9, "ISO 6400 以上")]


def now_local() -> float:
    """The wall clock read as UTC, which is how the library stores capture times."""
    return datetime.now().replace(tzinfo=timezone.utc).timestamp()


# ------------------------------------------------------------------- loading


def load(ctx: Ctx, where: str = "1", params: tuple = ()) -> list[dict]:
    rows = ctx.db.all(
        f"SELECT {COLS}, e.recipe FROM photos p LEFT JOIN edits e ON e.photo_id=p.id "
        f"WHERE p.error IS NULL AND ({where}) ORDER BY p.taken_ts, p.id",
        params,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["flags"] = json.loads(d["flags"] or "[]")
        d["vlm"] = json.loads(d["vlm"]) if d["vlm"] else {}
        d["recipe"] = json.loads(d["recipe"]) if d["recipe"] else {}
        out.append(d)
    return out


def lens_book(ctx: Ctx) -> dict:
    """The widest aperture each named lens has been seen at, by focal length. This is how the app knows what a lens can do."""
    book: dict = defaultdict(list)
    for r in ctx.db.all(
        "SELECT camera, lens, ROUND(focal) focal, MIN(aperture) a FROM photos "
        "WHERE aperture IS NOT NULL AND focal IS NOT NULL AND lens IS NOT NULL AND error IS NULL GROUP BY camera, lens, ROUND(focal)"
    ):
        book[(r["camera"], r["lens"])].append((r["focal"], r["a"]))
    return book


def widest(book: dict, p: dict) -> float | None:
    if not p.get("focal") or not p.get("lens"):
        return None  # without a lens name two different lenses could be mixed up
    near = [a for f, a in book.get((p.get("camera"), p["lens"]), []) if abs(f - p["focal"]) <= 0.12 * p["focal"]]
    return min(near) if near else None


def ensure_tilt(ctx: Ctx, rows: list[dict], progress=None, cap: int | None = None) -> int:
    """Measures how far each photo leans, once. Only a confident reading counts; the rest are stored as level."""
    from concurrent.futures import ThreadPoolExecutor

    todo = [p for p in rows if p.get("tilt") is None][: cap or None]

    def one(p: dict) -> float:
        try:
            with Image.open(ctx.thumb_path(p["id"])) as im:
                angle, conf = editing.detect_tilt(im.convert("RGB"))
        except Exception:
            return 0.0
        return round(-angle, 2) if conf >= 0.6 else 0.0  # detect_tilt returns the correction; the lean is its opposite

    done = []
    with ThreadPoolExecutor(max_workers=4) as pool:  # about 40 ms a photo, so a large library takes a few minutes the first time
        for i, (p, tilt) in enumerate(zip(todo, pool.map(one, todo))):
            p["tilt"] = tilt
            done.append((tilt, p["id"]))
            if progress and i % 20 == 0:
                progress(i, len(todo), "量測水平")
    if done:
        ctx.db.many("UPDATE photos SET tilt=? WHERE id=?", done)
    return len(done)


def enrich(ctx: Ctx, rows: list[dict], book: dict | None = None) -> list[dict]:
    """Runs the rules on every row. Adds `_d` (the diagnosis), `_keys`, `_blurry`, `_phone`, `_slow` and the sun's bearing."""
    book = lens_book(ctx) if book is None else book
    days: dict = {}
    blur_below = ctx.settings.analysis.blur_below
    for p in rows:
        day = None
        if p.get("lat") is not None and p.get("lon") is not None and p.get("time_source") == "exif":
            key = (p["taken_at"][:10], round(p["lat"], 1), round(p["lon"], 1), p.get("tz_offset"))
            if key not in days:
                days[key] = sun.day_events(key[0], p["lat"], p["lon"], p.get("tz_offset"))
            day = days[key]
            offset = p["tz_offset"] if p.get("tz_offset") is not None else sun.guess_offset(p["lon"])
            p["sun_az"] = round(sun.position(p["taken_ts"] - offset * 60, p["lat"], p["lon"])[1], 1)
        p["_day"] = day
        p["_d"] = shooting.diagnose(p, lens_max=widest(book, p), blur_below=blur_below, day=day)
        p["_keys"] = {f["key"] for f in p["_d"]["findings"]}
        p["_blurry"] = p.get("sharp") is not None and p["sharp"] < blur_below
        p["_phone"] = shooting.is_phone(p.get("camera"))
        p["_slow"] = None
        if p.get("shutter_s") and not p["_phone"]:
            p["_slow"] = shooting.stops(p["shutter_s"], shooting.safe_shutter(p.get("focal35") or p.get("focal"), p.get("category")))
    return rows


# -------------------------------------------------------------- measurements

# Things that can be counted again on later photos, so an exercise has something to be checked against.
# name: (label, which way is better, unit, function(rows) -> (value, how many photos it rests on))


def _rate(rows, pred, base=lambda p: True):
    pool = [p for p in rows if base(p)]
    return (sum(1 for p in pool if pred(p)) / len(pool) if pool else None), len(pool)


def _settings(p) -> bool:
    return bool(p.get("aperture") and p.get("shutter_s") and p.get("iso")) and not p["_phone"]


def _outdoor(p) -> bool:
    return (p["vlm"].get("setting") == "室外") if p["vlm"].get("setting") else p.get("category") in shooting.OUTDOOR


def _avg_score(key):
    def fn(rows):
        vals = [p["vlm"][key] for p in rows if p["vlm"].get(key)]
        return (round(mean(vals), 2) if vals else None), len(vals)

    return fn


def _variety(values, base):
    def fn(rows):
        pool = [p for p in rows if base(p)]
        seen = set()
        for p in pool:
            seen.update(values(p))
        return (float(len(seen)) if pool else None), len(pool)

    return fn


def _bucket(v, table) -> str | None:
    if v is None:
        return None
    return next((label for lo, hi, label in table if lo <= v < hi), None)


def _full_stop(n: float) -> float:
    return min((1.0, 1.4, 2.0, 2.8, 4.0, 5.6, 8.0, 11.0, 16.0, 22.0, 32.0), key=lambda s: abs(shooting.stops(s, n)))


def keeper(p) -> bool:
    return bool(p["is_best"]) and not p["_blurry"] and not ({"overexposed", "underexposed"} & set(p["flags"]))


METRICS: dict[str, tuple] = {
    "blur_rate": ("模糊照片的比例", "lower", "%", lambda rows: _rate(rows, lambda p: p["_blurry"])),
    "slow_shutter_rate": ("快門低於安全快門的比例", "lower", "%",
                          lambda rows: _rate(rows, lambda p: p["_slow"] is not None and p["_slow"] >= 1 and p["shutter_s"] < 0.5, _settings)),
    "exposure_miss_rate": ("過曝或曝光不足的比例", "lower", "%",
                           lambda rows: _rate(rows, lambda p: bool({"overexposed", "underexposed"} & set(p["flags"])))),
    "tilt_rate": ("水平歪斜的比例", "lower", "%",
                  lambda rows: _rate(rows, lambda p: abs(p["tilt"]) >= TILT_LEAN, lambda p: p.get("tilt") is not None)),
    "high_iso_rate": ("ISO 比需要的高的比例", "lower", "%", lambda rows: _rate(rows, lambda p: "iso_high" in p["_keys"], _settings)),
    "golden_share": ("在黃金或藍調時刻拍攝的比例", "higher", "%",
                     lambda rows: _rate(rows, lambda p: p["light"] in ("golden", "blue"), lambda p: p.get("light") and _outdoor(p))),
    "midday_share": ("在正午頂光下拍攝的比例", "lower", "%",
                     lambda rows: _rate(rows, lambda p: p["sun_elev"] > sun.HARSH, lambda p: p.get("sun_elev") is not None and _outdoor(p))),
    "center_share": ("中央構圖的比例", "lower", "%",
                     lambda rows: _rate(rows, lambda p: "中央構圖" in (p["vlm"].get("techniques") or []), lambda p: bool(p["vlm"]))),
    "technique_variety": ("用到的構圖手法種類", "higher", "種",
                          _variety(lambda p: p["vlm"].get("techniques") or [], lambda p: bool(p["vlm"]))),
    "aperture_variety": ("用到的光圈級數", "higher", "級", _variety(lambda p: [_full_stop(p["aperture"])], _settings)),
    "focal_variety": ("用到的焦段種類", "higher", "種",
                      _variety(lambda p: [_bucket(p.get("focal35") or p.get("focal"), FOCAL)], lambda p: bool(p.get("focal35") or p.get("focal")))),
    "score_composition": ("地端評審的構圖平均分", "higher", "分", _avg_score("composition")),
    "score_lighting": ("地端評審的光線平均分", "higher", "分", _avg_score("lighting")),
    "score_story": ("地端評審的故事性平均分", "higher", "分", _avg_score("story")),
    "keep_rate": ("值得留下的比例", "higher", "%", lambda rows: _rate(rows, keeper)),
}  # fmt: skip
TOLERANCE = {"%": 0.05, "分": 0.3, "種": 0.5, "級": 0.5}


def measure(rows: list[dict]) -> dict:
    out = {}
    for key, (label, better, unit, fn) in METRICS.items():
        value, n = fn(rows)
        out[key] = {"label": label, "better": better, "unit": unit, "value": None if value is None else round(value, 3), "n": n}
    return out


def show_metric(m: dict) -> str:
    if m["value"] is None:
        return "—"
    return f"{round(m['value'] * 100)}%" if m["unit"] == "%" else f"{m['value']:g} {m['unit']}"


# --------------------------------------------------------------------- pairs


def compare(better: dict, worse: dict, blur_below: float) -> dict | None:
    """Two frames of the same scene: what was different, in numbers, and what that teaches."""
    diffs = []
    a, b = better, worse
    slower = None
    if a.get("shutter_s") and b.get("shutter_s") and abs(shooting.stops(a["shutter_s"], b["shutter_s"])) >= 0.9:
        slower = shooting.stops(b["shutter_s"], a["shutter_s"])  # positive: the worse frame was slower
        diffs.append(f"快門 {shooting.fmt_shutter(a['shutter_s'])} 對 {shooting.fmt_shutter(b['shutter_s'])}（差 {abs(slower):.0f} 級）")
    if a.get("aperture") and b.get("aperture") and abs(shooting.stops(a["aperture"], b["aperture"])) >= 0.45:
        diffs.append(f"光圈 {shooting.fmt_aperture(a['aperture'])} 對 {shooting.fmt_aperture(b['aperture'])}")
    if a.get("iso") and b.get("iso") and abs(shooting.stops(a["iso"], b["iso"])) >= 0.9:
        diffs.append(f"ISO {a['iso']} 對 {b['iso']}")
    fa, fb = a.get("focal35") or a.get("focal"), b.get("focal35") or b.get("focal")
    if fa and fb and abs(fa - fb) > 0.12 * fa:
        diffs.append(f"焦段 {fa:.0f}mm 對 {fb:.0f}mm")
    sharp_gap = (a.get("sharp") or 0) - (b.get("sharp") or 0)
    if abs(sharp_gap) >= 12:
        diffs.append(f"清晰度 {a['sharp']:.0f} 對 {b['sharp']:.0f}")
    ta, tb = a.get("tilt") or 0.0, b.get("tilt") or 0.0
    if abs(abs(ta) - abs(tb)) >= TILT_LEAN:
        diffs.append(f"水平 {abs(ta):.1f}° 對 {abs(tb):.1f}°")
    ea, eb = {"overexposed", "underexposed"} & set(a["flags"]), {"overexposed", "underexposed"} & set(b["flags"])
    if ea != eb:
        diffs.append("曝光：" + ("正常" if not ea else "有問題") + " 對 " + ("正常" if not eb else "過曝" if "overexposed" in eb else "不足"))
    if a.get("vlm_score") and b.get("vlm_score") and abs(a["vlm_score"] - b["vlm_score"]) >= 8:
        diffs.append(f"地端評分 {a['vlm_score']:.0f} 對 {b['vlm_score']:.0f}")
    if not diffs:
        return None
    b_blur = b.get("sharp") is not None and b["sharp"] < blur_below and (a.get("sharp") or 0) >= blur_below
    same_settings = not any(d.startswith(("快門", "光圈", "ISO", "焦段")) for d in diffs)
    if sharp_gap >= 12 and slower and slower >= 0.9:
        lesson = f"同一個畫面，快門快了 {slower:.0f} 級就清楚{'了' if b_blur else '得多'}。"
    elif sharp_gap >= 12 and slower is not None and slower <= -0.9:
        lesson = (f"{'不清楚' if b_blur else '比較軟'}的那張快門其實更快，所以問題不在手震，而在對焦沒有落在主體上。"
                  "按快門前先確認對焦點的位置。")  # fmt: skip
    elif b_blur and same_settings:
        lesson = "設定幾乎一樣，差別在按下快門那一瞬間穩不穩、對焦有沒有落在主體上。重要的畫面連拍幾張再挑，是對付手震最簡單的方法。"
    elif b_blur:
        lesson = "模糊的那張不是快門太慢造成的，比較可能是對焦沒有落在主體上，或按快門時晃到了。重要的畫面連拍幾張再挑。"
    elif eb and not ea:
        lesson = "同一個場景，曝光對了細節就都在。拍完看一眼直方圖或高光警告，現場就能補拍。"
    elif abs(abs(tb) - abs(ta)) >= TILT_LEAN and abs(tb) > abs(ta):
        lesson = "水平拉正的那張看起來穩定得多。這是現場花一秒鐘就能做到的事。"
    elif sharp_gap >= 12:
        lesson = "兩張的差別主要在清晰度。按快門前先停一下呼吸，或把快門再加快一級。"
    else:
        lesson = "設定差不多，差別在取景和按快門的時機。"
    return {"better": a["id"], "worse": b["id"], "diffs": diffs, "lesson": lesson, "gap": round(sharp_gap, 1)}


def pairs(ctx: Ctx, rows: list[dict], limit: int = 6) -> list[dict]:
    """From bursts and repeated framings: the frame that worked next to one that did not."""
    groups: dict = defaultdict(list)
    for p in rows:
        if p.get("group_id") is not None and (p.get("group_size") or 1) > 1:
            groups[p["group_id"]].append(p)
    out = []
    blur_below = ctx.settings.analysis.blur_below
    for members in groups.values():
        if len(members) < 2:
            continue
        best = next((p for p in members if p["is_best"]), max(members, key=lambda p: p.get("tech") or 0))
        rest = [p for p in members if p["id"] != best["id"]]
        worst = min(rest, key=lambda p: p.get("tech") or 0)
        c = compare(best, worst, blur_below)
        if c:
            out.append(c)
    out.sort(key=lambda c: -abs(c["gap"]))
    stored = {r["photo_id"]: json.loads(r["pair"]) for r in ctx.db.all("SELECT photo_id, pair FROM coach WHERE pair IS NOT NULL")}
    for c in out[:limit]:
        note = stored.get(c["worse"])
        if note and note.get("with") == c["better"]:
            c["explained"] = note
    return out[:limit]


PAIR_SCHEMA = {
    "type": "object",
    "properties": {"difference": {"type": "string"}, "lesson": {"type": "string"}},
    "required": ["difference", "lesson"],
    "additionalProperties": False,
}


def explain_pair(ctx: Ctx, better_id: int, worse_id: int) -> dict:
    """The on-device model looks at both frames side by side and says what the eye sees. No tokens."""
    rows = {p["id"]: p for p in enrich(ctx, load(ctx, "p.id IN (?,?)", (better_id, worse_id)))}
    if len(rows) != 2:
        raise KeyError(worse_id)
    c = compare(rows[better_id], rows[worse_id], ctx.settings.analysis.blur_below) or {"diffs": []}
    px = ctx.settings.local.image_px
    left, _ = imaging.load_image(ctx.preview(better_id, px))
    right, _ = imaging.load_image(ctx.preview(worse_id, px))
    sheet = editing.side_by_side(left, right)
    vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge")
    user = (
        "這是同一個場景的兩張照片，左邊是比較好的那張，右邊是比較差的那張。\n"
        + ("程式量到的差別：" + "；".join(c["diffs"]) + "\n" if c["diffs"] else "")
        + "請用老師指導學生的口吻說明：\n"
        "- difference：從畫面上看，左邊好在哪裡（構圖、主體、時機、光線、清晰度），一兩句，要指出畫面裡的具體東西\n"
        "- lesson：下次拍的時候要記得的一件事，一句話\n"
        "不要重複程式已經量到的數字。"
    )
    d = vlm.chat_json("你是一位有經驗的攝影老師。只輸出 JSON，用繁體中文。", user, [imaging.jpeg_bytes(sheet, 86)], PAIR_SCHEMA, max_tokens=500)
    note = {"with": better_id, "difference": str(d.get("difference", "")).strip()[:300], "lesson": str(d.get("lesson", "")).strip()[:200]}
    if not note["difference"]:
        raise local_vlm.LocalModelError("地端模型沒有說明差別，請再試一次。")
    ctx.db.run(
        "INSERT INTO coach(photo_id, pair, model, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(photo_id) DO UPDATE SET pair=excluded.pair, updated_at=excluded.updated_at",
        (worse_id, json.dumps(note, ensure_ascii=False), vlm.model, time.time()),
    )
    return note


# ---------------------------------------------------------------- one photo


ADVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "works": {"type": "string"},
        "composition": {"type": "array", "items": {"type": "string"}},
        "position": {"type": "string"},
        "light": {"type": "string"},
        "one_thing": {"type": "string"},
    },
    "required": ["intent", "works", "composition", "position", "light", "one_thing"],
    "additionalProperties": False,
}
ADVICE_SYSTEM = (
    "你是一位有三十年經驗的攝影老師，正在看學生的一張照片，告訴他下次再遇到這個場景要怎麼拍得更好。\n"
    "- 具體：指出畫面裡實際的東西（「右邊那根電線桿」「前景的欄杆」），說清楚要往哪裡移動、把什麼放進或移出畫面。\n"
    "- 相機設定的數值已經由程式算好，你不要提任何快門、光圈、ISO 的數字，專心講構圖、站位、時機和光線。\n"
    "- 先肯定真正做得好的地方，再講要改的。不說客套話，不說空泛的原則。\n"
    "只輸出 JSON，用繁體中文。"
)


def _light_text(p: dict) -> str:
    bits = []
    if p.get("light"):
        bits.append(sun.LABELS[p["light"]])
    if p.get("sun_elev") is not None:
        bits.append(f"太陽高度 {p['sun_elev']:.0f}°")
    if p.get("heading") is not None and p.get("sun_az") is not None and (p.get("sun_elev") or 0) > 3:
        bits.append(sun.relative_light(p["sun_az"], p["heading"])[0])
    elif p["vlm"].get("light_type"):
        bits.append(p["vlm"]["light_type"])
    return "、".join(bits)


def _timetable(day: dict | None) -> dict | None:
    if not day:
        return None
    out = {k: day[k] for k in ("day", "tz_exact", "sunrise_az", "sunset_az", "noon_elev")}
    for k in ("sunrise", "sunset", "noon"):
        out[k] = sun.hhmm(day[k])
    for k in ("morning_blue", "morning_golden", "harsh", "evening_golden", "evening_blue"):
        out[k] = [sun.hhmm(x) for x in day[k]] if day[k] else None
    for k in ("sunrise_az", "sunset_az"):
        out[k + "_zh"] = sun.compass(day[k]) if day[k] is not None else None
    return out


def photo(ctx: Ctx, pid: int) -> dict:
    """Everything the mentor panel shows for one photo. No model is called."""
    ingest.refresh_meta(ctx, only=pid)  # a library analysed before mentor mode existed lacks the shooting fields
    rows = load(ctx, "p.id=?", (pid,))
    if not rows:
        raise KeyError(pid)
    ensure_tilt(ctx, rows)
    p = enrich(ctx, rows)[0]
    coach = ctx.db.one("SELECT advice, pair, model FROM coach WHERE photo_id=?", (pid,))
    pair = None
    if (p.get("group_size") or 1) > 1:
        members = load(ctx, "p.group_id=?", (p["group_id"],))
        ensure_tilt(ctx, members)
        enrich(ctx, members)
        best = next((m for m in members if m["is_best"]), None)
        if best is not None:
            other = min((m for m in members if m["id"] != best["id"]), key=lambda m: m.get("tech") or 0) if p["id"] == best["id"] else p
            pair = compare(best, other, ctx.settings.analysis.blur_below)
            if pair and coach and coach["pair"] and pair["worse"] == pid:
                pair["explained"] = json.loads(coach["pair"])
    light = None
    if p.get("sun_elev") is not None:
        light = {
            "elev": p["sun_elev"],
            "phase": p.get("light"),
            "az": p.get("sun_az"),
            "compass": sun.compass(p["sun_az"]) if p.get("sun_az") is not None else None,
            "heading": p.get("heading"),
            "exact": p.get("tz_offset") is not None,
            "time": p["taken_at"][11:16],
            "day": _timetable(p["_day"]),
        }
    return {
        "id": pid,
        "summary": shooting.summary(p),
        "now": p["_d"]["now"],
        "next": p["_d"]["next"],
        "shortfall": p["_d"]["shortfall"],
        "findings": p["_d"]["findings"],
        "light": light,
        "advice": json.loads(coach["advice"]) if coach and coach["advice"] else None,
        "advice_model": coach["model"] if coach else None,
        "pair": pair,
        "annotated": bool(p["vlm"]),
    }


def advise(ctx: Ctx, pid: int) -> dict:
    """The on-device model looks at the photo and says what to do in front of the same scene next time. No tokens."""
    rows = load(ctx, "p.id=?", (pid,))
    if not rows:
        raise KeyError(pid)
    p = enrich(ctx, rows)[0]
    s = shooting.summary(p)
    known = []
    if s["settings"]:
        known.append(f"拍攝設定：{s['settings']}" + (f"，等效 {s['focal35']:.0f}mm" if s["focal35"] else "") + (f"，{s['mode']}" if s["mode"] else ""))
    if _light_text(p):
        known.append("光線：" + _light_text(p))
    if p["vlm"].get("caption"):
        known.append("畫面：" + local_vlm.card(p))
    measured = [f["title"] for f in p["_d"]["findings"] if f["level"] in ("fix", "tip")]
    if measured:
        known.append("程式已經診斷出的問題（不需要重複）：" + "；".join(measured))
    user = (
        "請看這張照片，告訴學生下次怎麼拍得更好。\n"
        + "\n".join(known)
        + "\n\n- intent：這張照片想表達什麼，一句話\n"
        "- works：真正做得好的一個地方，一句話\n"
        "- composition：1～2 條構圖建議，每條都要說清楚畫面裡的哪個東西要怎麼處理\n"
        "- position：站位、高度或角度要怎麼改，一句話；現在的位置已經很好就留空字串\n"
        "- light：這個場景適合什麼樣的光線或時間，一句話\n"
        "- one_thing：下次再拍這個場景，最該做的一件事"
    )
    vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge")
    d = vlm.chat_json(ADVICE_SYSTEM, user, [ctx.preview_bytes(pid, ctx.settings.local.image_px)], ADVICE_SCHEMA, max_tokens=900)
    advice = {
        "intent": str(d.get("intent", "")).strip()[:200],
        "works": str(d.get("works", "")).strip()[:200],
        "composition": [str(x).strip()[:240] for x in (d.get("composition") or []) if str(x).strip()][:3],
        "position": str(d.get("position", "")).strip()[:240],
        "light": str(d.get("light", "")).strip()[:240],
        "one_thing": str(d.get("one_thing", "")).strip()[:240],
    }
    if not advice["one_thing"] and not advice["composition"]:
        raise local_vlm.LocalModelError("地端模型沒有給出建議，請再試一次。")
    ctx.db.run(
        "INSERT INTO coach(photo_id, advice, model, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(photo_id) DO UPDATE SET advice=excluded.advice, model=excluded.model, updated_at=excluded.updated_at",
        (pid, json.dumps(advice, ensure_ascii=False), vlm.model, time.time()),
    )
    return advice


# ---------------------------------------------------------------- statistics


def _dist(rows, value, table=None, top: int = 6) -> list[dict]:
    c: Counter = Counter()
    for p in rows:
        v = value(p)
        v = _bucket(v, table) if table else v
        if v:
            c[v] += 1
    total = sum(c.values())
    order = [label for *_, label in table] if table else [k for k, _ in c.most_common(top)]
    return [{"name": k, "n": c[k], "share": round(c[k] / total, 3)} for k in order if c[k]]


def _insights(rows: list[dict]) -> list[str]:
    """Comparisons inside the user's own pictures. Each one is only stated when both sides have enough photos."""
    out = []
    gold = [p["aesthetic"] for p in rows if p.get("light") in ("golden", "blue") and p.get("aesthetic") is not None and _outdoor(p)]
    day = [p["aesthetic"] for p in rows if p.get("light") == "day" and p.get("aesthetic") is not None and _outdoor(p)]
    if len(gold) >= MIN_GROUP and len(day) >= MIN_GROUP and abs(mean(gold) - mean(day)) >= 5:
        out.append(f"在黃金或藍調時刻拍的 {len(gold)} 張，美感初評平均 {mean(gold):.0f} 分；白天拍的 {len(day)} 張平均 {mean(day):.0f} 分。")
    cam = [p for p in rows if p["_slow"] is not None and p["shutter_s"] < 0.5]
    slow, fast = [p for p in cam if p["_slow"] >= 0.5], [p for p in cam if p["_slow"] < 0.5]
    if len(slow) >= MIN_GROUP and len(fast) >= MIN_GROUP:
        bs, bf = sum(p["_blurry"] for p in slow) / len(slow), sum(p["_blurry"] for p in fast) / len(fast)
        if bs - bf >= 0.1:
            out.append(f"快門慢於安全快門的 {len(slow)} 張裡有 {bs:.0%} 模糊；快門夠快的 {len(fast)} 張只有 {bf:.0%}。")
    ann = [p for p in rows if p["vlm"] and p.get("vlm_score")]
    by_tech: dict = defaultdict(list)
    for p in ann:
        for t in p["vlm"].get("techniques") or []:
            by_tech[t].append(p["vlm_score"])
    solid = {t: v for t, v in by_tech.items() if len(v) >= MIN_GROUP}
    if len(solid) >= 2:
        most = max(solid, key=lambda t: len(solid[t]))
        best = max(solid, key=lambda t: mean(solid[t]))
        if best != most and mean(solid[best]) - mean(solid[most]) >= 3:
            out.append(f"你最常用的構圖是「{most}」（{len(solid[most])} 張，平均 {mean(solid[most]):.0f} 分），"
                       f"但分數最高的是用了「{best}」的照片（{len(solid[best])} 張，平均 {mean(solid[best]):.0f} 分）。")  # fmt: skip
    by_focal: dict = defaultdict(list)
    for p in rows:
        b = _bucket(p.get("focal35") or p.get("focal"), FOCAL)
        score = p.get("vlm_score") or p.get("aesthetic")
        if b and score is not None:
            by_focal[b].append(score)
    solid = {b: v for b, v in by_focal.items() if len(v) >= MIN_GROUP}
    if len(solid) >= 2:
        most = max(solid, key=lambda b: len(solid[b]))
        best = max(solid, key=lambda b: mean(solid[b]))
        if best != most and mean(solid[best]) - mean(solid[most]) >= 4:
            out.append(f"你最常用{most.split('（')[0]}焦段（{len(solid[most])} 張），但{best.split('（')[0]}焦段的照片平均分數比較高"
                       f"（{mean(solid[best]):.0f} 對 {mean(solid[most]):.0f}）。")  # fmt: skip
    cams = [p for p in rows if _settings(p) and p.get("exp_program")]
    if len(cams) >= MIN_GROUP:
        auto = sum(p["exp_program"] in ("auto", "program") for p in cams) / len(cams)
        if auto >= 0.5:
            out.append(f"相機拍的 {len(cams)} 張裡有 {auto:.0%} 是用全自動或程式模式，光圈和快門是相機替你決定的。")
    edited = [p["recipe"] for p in rows if p["recipe"]]
    if len(edited) >= MIN_GROUP:
        ev = [float(r.get("exposure") or 0) for r in edited]
        up, down = sum(e >= 0.25 for e in ev), sum(e <= -0.25 for e in ev)
        if up >= 0.6 * len(edited):
            out.append(f"修過的 {len(edited)} 張裡有 {up} 張是往上提亮，平均 {mean(e for e in ev if e >= 0.25):+.1f} 級：你拍的時候習慣性偏暗。")
        elif down >= 0.6 * len(edited):
            out.append(f"修過的 {len(edited)} 張裡有 {down} 張是往下壓暗，平均 {mean(e for e in ev if e <= -0.25):+.1f} 級：你拍的時候習慣性偏亮。")
        turned = sum(abs(float(r.get("rotate") or 0)) >= 0.7 for r in edited)
        if turned >= 0.4 * len(edited):
            out.append(f"修過的 {len(edited)} 張裡有 {turned} 張轉正過水平。")
        cropped = [r for r in edited if r.get("crop")]
        if len(cropped) >= 0.5 * len(edited):
            out.append(f"修過的 {len(edited)} 張裡有 {len(cropped)} 張裁切過，代表拍的時候離主體不夠近，或畫面邊緣沒有留意。")
    if len(ann) >= MIN_GROUP:
        avg = {k: mean(p["vlm"][k] for p in ann if p["vlm"].get(k)) for k in SCORE_ZH if any(p["vlm"].get(k) for p in ann)}
        if len(avg) >= 3:
            lo, hi = min(avg, key=avg.get), max(avg, key=avg.get)
            if avg[hi] - avg[lo] >= 0.8:
                out.append(f"地端評審的六個面向裡，你最強的是「{SCORE_ZH[hi]}」（平均 {avg[hi]:.1f}），最弱的是「{SCORE_ZH[lo]}」（平均 {avg[lo]:.1f}）。")
    return out


def stats(ctx: Ctx, rows: list[dict]) -> dict:
    """What can be said about a set of photos without any model. `rows` must already be enriched."""
    n = len(rows)
    cam = [p for p in rows if _settings(p)]
    hours = [0] * 24
    for p in rows:
        if p.get("time_source") == "exif":
            hours[int(p["taken_at"][11:13])] += 1
    issues: Counter = Counter()
    examples: dict = defaultdict(list)
    levels: dict = {}
    for p in rows:
        for f in p["_d"]["findings"]:
            if f["level"] == "info":
                continue
            issues[f["key"]] += 1
            levels[f["key"]] = f["level"]
            if len(examples[f["key"]]) < 6:
                examples[f["key"]].append(p["id"])
    found = [{"key": k, "title": KEY_ZH.get(k, k), "level": levels[k], "n": c, "share": round(c / n, 3), "examples": examples[k]}
             for k, c in issues.most_common()]  # fmt: skip
    ann = [p for p in rows if p["vlm"]]
    scores = {k: round(mean(p["vlm"][k] for p in ann if p["vlm"].get(k)), 2) for k in SCORE_ZH if any(p["vlm"].get(k) for p in ann)}
    edited = [p["recipe"] for p in rows if p["recipe"]]
    dated = [p["taken_at"][:10] for p in rows if p.get("time_source") == "exif"]
    light: Counter = Counter(p.get("light") or "unknown" for p in rows)
    return {
        "count": n,
        "span": [min(dated), max(dated)] if dated else None,
        "keepers": sum(keeper(p) for p in rows),
        "cameras": _dist(rows, lambda p: p.get("camera")),
        "lenses": _dist(rows, lambda p: p.get("lens")),
        "phone_share": round(sum(p["_phone"] for p in rows) / n, 3) if n else 0,
        "focal": _dist(rows, lambda p: p.get("focal35") or p.get("focal"), FOCAL),
        "aperture": _dist(cam, lambda p: p["aperture"], APERTURE),
        "iso": _dist(cam, lambda p: p["iso"], ISO),
        "program": _dist(cam, lambda p: shooting.PROGRAM_ZH.get(p.get("exp_program") or "")),
        "categories": _dist(rows, lambda p: taxonomy.LABELS.get(p.get("category") or "")),
        "hours": hours,
        "light": {k: light.get(k, 0) for k in ("golden", "blue", "day", "night", "unknown")},
        "flaws": {"blurry": sum(p["_blurry"] for p in rows), **{k: sum(k in p["flags"] for p in rows) for k in ("overexposed", "underexposed")}},
        "metrics": measure(rows),
        "problems": [f for f in found if f["level"] in ("fix", "tip")],
        "good": [f for f in found if f["level"] == "good"],
        "edits": {
            "n": len(edited),
            "exposure": round(mean(float(r.get("exposure") or 0) for r in edited), 2) if edited else 0,
            "straightened": sum(abs(float(r.get("rotate") or 0)) >= 0.7 for r in edited),
            "cropped": sum(bool(r.get("crop")) for r in edited),
        },
        "annotated": len(ann),
        "techniques": [{"name": t, "n": c, "share": round(c / len(ann), 3)}
                       for t, c in Counter(t for p in ann for t in (p["vlm"].get("techniques") or [])).most_common(6)],
        "scores": scores,
        "insights": _insights(rows),
        "pairs": pairs(ctx, rows),
    }  # fmt: skip


def stats_text(s: dict) -> str:
    """The statistics as a few lines of text for a model to read."""

    def line(name, items):
        return f"{name}：" + "、".join(f"{x['name']} {x['share']:.0%}" for x in items) if items else ""

    span = f"{s['span'][0]} 到 {s['span'][1]}" if s["span"] and s["span"][0] != s["span"][1] else (s["span"][0] if s["span"] else "")
    lines = [
        f"共 {s['count']} 張" + (f"，拍攝於 {span}" if span else "") + f"；值得留下的 {s['keepers']} 張；模糊 {s['flaws']['blurry']} 張、"
        f"過曝 {s['flaws']['overexposed']} 張、曝光不足 {s['flaws']['underexposed']} 張。",
        line("器材", s["cameras"]),
        line("焦段（等效）", s["focal"]),
        line("光圈", s["aperture"]),
        line("ISO", s["iso"]),
        line("曝光模式", s["program"]),
        line("題材", s["categories"]),
        line("構圖手法（地端模型標註）", s["techniques"]),
    ]
    lt = s["light"]
    known = sum(lt[k] for k in ("golden", "blue", "day", "night"))
    if known:
        lines.append(f"光線時段：黃金時刻 {lt['golden']} 張、藍調時刻 {lt['blue']} 張、白天 {lt['day']} 張、夜晚 {lt['night']} 張。")
    busy = sorted(range(24), key=lambda h: -s["hours"][h])[:3]
    if sum(s["hours"]):
        lines.append("最常拍照的時間：" + "、".join(f"{h} 點（{s['hours'][h]} 張）" for h in busy if s["hours"][h]))
    if s["scores"]:
        lines.append("地端評審各面向平均（1–10）：" + "、".join(f"{SCORE_ZH[k]} {v:.1f}" for k, v in s["scores"].items()))
    if s["problems"]:
        lines.append("程式診斷出的問題（次數）：" + "、".join(f"{f['title']} {f['n']}" for f in s["problems"][:8]))
    if s["good"]:
        lines.append("做得好的地方（次數）：" + "、".join(f"{f['title']} {f['n']}" for f in s["good"][:4]))
    if s["edits"]["n"]:
        e = s["edits"]
        lines.append(f"後製：修過 {e['n']} 張，平均曝光調整 {e['exposure']:+.1f} 級，轉正 {e['straightened']} 張，裁切 {e['cropped']} 張。")
    if s["insights"]:
        lines.append("從學生自己的照片比較出來的事實：\n" + "\n".join("・" + x for x in s["insights"]))
    return "\n".join(x for x in lines if x)


# ------------------------------------------------------------------- reviews

MENTOR_SYSTEM = (
    "你是一位有三十年經驗的攝影老師，正在一對一指導學生。你會看到學生照片的縮圖總表、程式量出來的統計數字，"
    "以及每張照片的拍攝設定和診斷。\n"
    "- 具體：每個觀察都要指出是哪幾張（用編號）或哪個數字，不說放諸四海皆準的原則。\n"
    "- 聚焦：只講最重要的兩三件事。學生一次改不了十件事。\n"
    "- 快門、光圈、ISO 的數值一律引用程式診斷裡已經算好的，不要自己發明數字。\n"
    "- 你的價值在程式看不到的地方：構圖、主體、時機、怎麼用光、照片想說什麼。\n"
    "- 語氣像老師對學生：直接、溫暖、不說客套話。先肯定真正做得好的，再講要改的。\n"
    "- 縮圖看不出清晰度，清晰度以程式的診斷為準。\n"
    "全部用繁體中文。"
)


def review_schema() -> dict:
    text = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "headline": text,
            "strengths": {"type": "array", "items": {
                "type": "object", "properties": {"title": text, "evidence": text},
                "required": ["title", "evidence"], "additionalProperties": False}},
            "problems": {"type": "array", "items": {
                "type": "object",
                "properties": {"title": text, "evidence": text, "fix": text, "photos": {"type": "array", "items": {"type": "integer"}}},
                "required": ["title", "evidence", "fix", "photos"], "additionalProperties": False}},
            "next_time": {"type": "array", "items": text},
            "photo_notes": {"type": "array", "items": {
                "type": "object", "properties": {"no": {"type": "integer"}, "note": text},
                "required": ["no", "note"], "additionalProperties": False}},
            "exercises": {"type": "array", "items": {
                "type": "object",
                "properties": {"title": text, "how": text, "why": text, "metric": {"type": "string", "enum": [*METRICS, "manual"]}},
                "required": ["title", "how", "why", "metric"], "additionalProperties": False}},
        },
        "required": ["headline", "strengths", "problems", "next_time", "photo_notes", "exercises"],
        "additionalProperties": False,
    }  # fmt: skip


def scope_rows(ctx: Ctx, scope: str, scope_id: int | None) -> tuple[list[dict], str]:
    if scope == "event":
        ev = ctx.db.one("SELECT title, place FROM events WHERE id=?", (scope_id,))
        if ev is None:
            raise KeyError(scope_id)
        return load(ctx, "p.event_id=?", (scope_id,)), ev["title"]
    if scope == "library":
        return load(ctx), "整個相片庫"
    raise ValueError("不支援的檢討範圍")


def pick_sheet(rows: list[dict], n: int = SHEET_PHOTOS) -> list[dict]:
    """Which photos the mentor gets to see: the strongest, and one clear example of each recurring problem."""
    if len(rows) <= n:
        return list(rows)

    def score(p):
        return p.get("vlm_score") or p.get("aesthetic") or 0

    strong, seen_groups = [], set()
    for p in sorted(rows, key=score, reverse=True):
        if keeper(p) and p.get("group_id") not in seen_groups:
            strong.append(p)
            seen_groups.add(p.get("group_id"))
        if len(strong) >= n // 2:
            break
    chosen = {p["id"] for p in strong}
    weak = []
    counts = Counter(k for p in rows for f in p["_d"]["findings"] if f["level"] in ("fix", "tip") for k in [f["key"]])
    for key, _ in counts.most_common():
        cands = [p for p in rows if key in p["_keys"] and p["id"] not in chosen and p.get("group_id") not in seen_groups]
        if cands:
            p = min(cands, key=lambda p: p.get("tech") or 0)
            weak.append(p)
            chosen.add(p["id"])
            seen_groups.add(p.get("group_id"))
        if len(weak) >= n - len(strong):
            break
    rest = [p for p in sorted(rows, key=score) if p["id"] not in chosen]
    return (strong + weak + rest)[:n]


def _photo_line(no: int, p: dict) -> str:
    s = shooting.summary(p)
    bits = [f"{no}. {local_vlm.card(p)}"]
    if s["settings"]:
        bits.append(s["settings"] + (f"，等效 {s['focal35']:.0f}mm" if s["focal35"] else ""))
    elif s["device"] == "phone":
        bits.append("手機")
    if _light_text(p):
        bits.append(_light_text(p))
    found = [f["title"] for f in p["_d"]["findings"] if f["level"] in ("fix", "tip")]
    if found:
        bits.append("診斷：" + "；".join(found[:3]))
    if p["_d"]["next"]:
        bits.append("建議設定：" + p["_d"]["next"])
    return "｜".join(bits)


def _jury_notes(ctx: Ctx) -> list[str]:
    rows = ctx.db.all("SELECT final FROM contest_scores WHERE final IS NOT NULL ORDER BY rowid DESC LIMIT 12")
    return [w for w in (json.loads(r["final"]).get("weaknesses", "") for r in rows) if w][:10]


def _request(ctx: Ctx, scope: str, name: str, s: dict, sheet: list[dict]) -> tuple[list, dict]:
    menu = "\n".join(f"- {k}：{v['label']}，目前 {show_metric(v)}（{v['n']} 張）" for k, v in s["metrics"].items() if v["value"] is not None)
    what = f"學生一次外出拍攝的全部照片（{name}）" if scope == "event" else "學生的整個相片庫"
    text = (
        f"下面是{what}。\n\n【程式量測的統計】\n{stats_text(s)}\n"
        + ("\n【過去比賽評審寫過的扣分點】\n" + "\n".join("・" + w for w in _jury_notes(ctx)) + "\n" if scope == "library" and _jury_notes(ctx) else "")
        + f"\n【縮圖總表】共 {len(sheet)} 張，左上角有編號。前半是分數最高的，後半是各種問題的代表。總表下方是每張的說明、拍攝設定和程式診斷。\n\n"
        "請寫出：\n"
        "- headline：一句話總評，說出這位學生目前最大的特點\n"
        "- strengths：真正做得好的 1～2 件事，evidence 要引用編號或數字\n"
        "- problems：最該改的 2～3 件事，依重要性排列。evidence 說明你從哪裡看出來；fix 是下次具體要怎麼做；photos 是相關照片的編號\n"
        + ("- next_time：下次再來同一個地方，3～4 件要做的事（時間、站位、要拍什麼）\n" if scope == "event"
           else "- next_time：接下來一個月，3～4 個能讓照片明顯進步的做法\n")
        + "- photo_notes：挑 3～5 張最值得講的照片，各寫一句點評（好在哪或怎麼改）\n"
        "- exercises：1～2 個具體的練習。how 要說清楚怎麼做；metric 從下面的清單選一個能看出進步的量測，"
        "之後學生拍的新照片會用它自動檢查；沒有適合的就選 manual\n\n"
        f"【可用的量測】\n{menu}"
    )  # fmt: skip
    tiles = []
    for p in sheet:
        with Image.open(ctx.thumb_path(p["id"])) as im:
            tiles.append(im.convert("RGB"))
    image = imaging.jpeg_bytes(editing.contact_sheet(tiles, list(range(1, len(sheet) + 1))), 85)
    lines = "\n".join(_photo_line(i + 1, p) for i, p in enumerate(sheet))
    return [text, "縮圖總表：", Img(image), lines], review_schema()


def _clean_review(d: dict, sheet: list[dict]) -> dict:
    ids = {i + 1: p["id"] for i, p in enumerate(sheet)}

    def s(v, n=400):
        return str(v or "").strip()[:n]

    return {
        "headline": s(d.get("headline"), 200),
        "strengths": [{"title": s(x.get("title"), 80), "evidence": s(x.get("evidence"))} for x in (d.get("strengths") or [])[:3] if s(x.get("title"))],
        "problems": [{"title": s(x.get("title"), 80), "evidence": s(x.get("evidence")), "fix": s(x.get("fix")),
                      "photos": [ids[n] for n in (x.get("photos") or []) if n in ids][:6]}
                     for x in (d.get("problems") or [])[:3] if s(x.get("title"))],
        "next_time": [s(x, 300) for x in (d.get("next_time") or [])[:5] if s(x)],
        "photo_notes": [{"photo_id": ids[x["no"]], "note": s(x.get("note"), 300)} for x in (d.get("photo_notes") or [])[:6]
                        if x.get("no") in ids and s(x.get("note"))],
        "exercises": [{"title": s(x.get("title"), 80), "how": s(x.get("how")), "why": s(x.get("why"), 300),
                       "metric": x.get("metric") if x.get("metric") in METRICS else "manual"}
                      for x in (d.get("exercises") or [])[:2] if s(x.get("title"))],
    }  # fmt: skip


def outgoing(ctx: Ctx, scope: str, scope_id: int | None) -> dict:
    """What a review would send to the external model, for the user to see first."""
    rows, name = scope_rows(ctx, scope, scope_id)
    sheet = pick_sheet(enrich(ctx, rows))
    prov = ctx.settings.cloud.provider
    sheet_px = 4 * 384  # the contact sheet is four tiles wide
    return {
        "name": name,
        "count": len(rows),
        "photo_ids": [p["id"] for p in sheet],
        "provider": prov,
        "model": ctx.settings.cloud.models.get(prov, ""),
        "est_tokens": estimate_image_tokens(sheet_px) + 110 * len(sheet) + 3200 if sheet else 0,  # sheet, per-photo lines, statistics and the reply
    }


def review(ctx: Ctx, scope: str, scope_id: int | None, mode: str = "cloud", progress=lambda done, total, msg: None) -> dict:
    """Measures, then asks a model to read the result like a teacher would. mode: cloud | local | stats."""
    ingest.refresh_meta(ctx, progress)
    rows, name = scope_rows(ctx, scope, scope_id)
    if not rows:
        raise ValueError("這個範圍裡沒有照片。")
    ensure_tilt(ctx, rows, progress)
    progress(0, 0, "統計拍攝習慣")
    enrich(ctx, rows)
    s = stats(ctx, rows)
    sheet = pick_sheet(rows)
    result, provider, model = None, None, None
    if mode in ("cloud", "local"):
        parts, schema = _request(ctx, scope, name, s, sheet)
        if mode == "cloud":
            prov = get_provider(ctx.settings)
            progress(0, 0, f"請 {prov.name} 寫講評")
            res = prov.generate(system=MENTOR_SYSTEM, parts=parts, schema=schema, max_tokens=8000)
            contest.log_call(ctx, res, prov.name, f"mentor_{scope}", None, photos=len(sheet))
            data, provider, model = res.data, prov.name, res.model
        else:
            vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge")
            progress(0, 0, "地端模型寫講評")
            text = "\n\n".join(x for x in parts if isinstance(x, str))
            image = next(x.data for x in parts if isinstance(x, Img))
            data, provider, model = vlm.chat_json(MENTOR_SYSTEM + "\n只輸出 JSON。", text, [image], schema, max_tokens=2600), "local", vlm.model
        result = _clean_review(data, sheet)
        if not result["headline"] and not result["problems"]:
            raise CloudError("模型沒有寫出可用的講評，請再試一次。")
    cur = ctx.db.run(
        "INSERT INTO reviews(scope, scope_id, stats, review, photo_ids, provider, model, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (scope, scope_id, json.dumps(s, ensure_ascii=False), json.dumps(result, ensure_ascii=False) if result else None,
         json.dumps([p["id"] for p in sheet]), provider, model, time.time()),
    )  # fmt: skip
    rid = cur.lastrowid
    # Older reviews of the same thing are superseded; exercises they set stay, with their baselines.
    ctx.db.run("DELETE FROM reviews WHERE scope=? AND scope_id IS ? AND id<>?", (scope, scope_id, rid))
    added = 0
    if result:
        open_metrics = {r["metric"] for r in ctx.db.all("SELECT metric FROM exercises WHERE status='open' AND metric<>'manual'")}
        for x in result["exercises"]:
            if x["metric"] in open_metrics:
                continue  # already working on this; the earlier baseline is the one that matters
            m = s["metrics"].get(x["metric"])
            ctx.db.run(
                "INSERT INTO exercises(review_id, title, how, why, metric, baseline, baseline_n, since_ts, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, x["title"], x["how"], x["why"], x["metric"], m["value"] if m else None, m["n"] if m else None, now_local(), time.time()),
            )
            added += 1
    return {"id": rid, "scope": scope, "scope_id": scope_id, "photos": len(rows), "mode": mode, "exercises": added,
            "provider": provider, "headline": result["headline"] if result else None}  # fmt: skip


def get_review(ctx: Ctx, scope: str, scope_id: int | None) -> dict | None:
    r = ctx.db.one("SELECT * FROM reviews WHERE scope=? AND scope_id IS ? ORDER BY id DESC LIMIT 1", (scope, scope_id))
    if r is None:
        return None
    return {
        "id": r["id"], "scope": scope, "scope_id": scope_id, "created_at": r["created_at"], "provider": r["provider"], "model": r["model"],
        "stats": json.loads(r["stats"]), "review": json.loads(r["review"]) if r["review"] else None, "photo_ids": json.loads(r["photo_ids"] or "[]"),
    }  # fmt: skip


# ----------------------------------------------------------------- exercises


def exercises(ctx: Ctx) -> list[dict]:
    """Every exercise with how the photos taken since it was set measure up."""
    rows = ctx.db.all("SELECT * FROM exercises ORDER BY status='open' DESC, created_at DESC")
    todo = [r for r in rows if r["status"] == "open" and r["metric"] in METRICS]
    recent: list[dict] = []
    if todo:
        recent = load(ctx, "p.taken_ts > ? AND p.time_source='exif'", (min(r["since_ts"] for r in todo),))
        ensure_tilt(ctx, recent, cap=400)
        enrich(ctx, recent)
    out = []
    for r in rows:
        d = dict(r)
        d.update(label=None, unit=None, better=None, current=None, n=0, state="manual")
        if r["metric"] in METRICS:
            label, better, unit, fn = METRICS[r["metric"]]
            d.update(label=label, unit=unit, better=better, state="waiting")
            if r["status"] == "open":
                value, n = fn([p for p in recent if p["taken_ts"] > r["since_ts"]])
                d.update(current=None if value is None else round(value, 3), n=n)
                if value is not None and n >= MIN_PROGRESS and r["baseline"] is not None:
                    gain = (r["baseline"] - value) if better == "lower" else (value - r["baseline"])
                    tol = TOLERANCE[unit]
                    d["state"] = "better" if gain >= tol else "worse" if gain <= -tol else "same"
                elif value is not None and n >= MIN_PROGRESS:
                    d["state"] = "measured"
        out.append(d)
    return out


def set_exercise(ctx: Ctx, eid: int, status: str) -> None:
    if status not in ("open", "done", "dropped"):
        raise ValueError("不支援的狀態")
    ctx.db.run("UPDATE exercises SET status=? WHERE id=?", (status, eid))
