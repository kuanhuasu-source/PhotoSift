"""A solo exhibition: a fixed number of pictures that belong together.

A contest asks which single photos are best. An exhibition asks a different
question: which twenty hold together as a series, in what order, and whether
each will survive being printed large. So the work is split differently:

    themes      on-device: the library is grouped several ways and each group
                is offered as a possible series, named by the local model
    printable   arithmetic: does the photo have the pixels for the frame
    curate      the external model sees the candidates on contact sheets and
                acts as curator: chooses, groups into chapters, orders, and
                keeps a couple of reserves per chapter
    words       titles, captions, a biography and a statement, from text only
    hand-in     print files cut to the frame, caption cards as a slide deck
"""

from __future__ import annotations

import json
import time
import zipfile
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

from . import contest, editing, imaging, local_vlm, mentor, retouch, submission, taxonomy
from .cloud import CloudError, Img, estimate_image_tokens, get_provider
from .context import Ctx

DEFAULT_SPEC = {
    "count": 20,             # works on the wall
    "frame_w": 75.0,         # centimetres, long side first
    "frame_h": 50.0,
    "orientation": "both",   # both | landscape | portrait: whether the frame may be hung either way
    "pool": 48,              # candidates the curator is shown
    "reserves": 2,           # stand-ins kept per chapter
    "min_dpi": 100,          # below this a print looks soft even from across the room
    "desc_max": 80,          # characters on a caption card
    "tone": "詩意",
    "author": "",
    "exif": "all",
}
GOOD_DPI = 150
SHEET = 16
# Ways a set of photos can hang together that have nothing to do with where they were taken.
CONCEPTS = [
    ("quiet", "寧靜與留白", "a quiet, empty, serene scene with calm atmosphere and no people"),
    ("reflection", "倒影", "reflections on water or glass, mirrored scenery"),
    ("geometry", "線條與幾何", "geometric architecture with strong lines, patterns and symmetry"),
    ("light", "光與影", "dramatic light and shadow, strong contrast, rays of light"),
    ("people", "城市裡的人", "candid street photography of people in a city"),
    ("colour", "色彩", "bold vivid saturated colours as the subject of the photo"),
    ("time", "時間的痕跡", "old traditional streets, temples and weathered buildings, a sense of history"),
    ("land", "大地", "majestic natural landscape, mountains, waterfalls, vast scenery"),
    ("water", "水", "the sea, a river, a lake or a waterfall"),
    ("night", "入夜之後", "a night scene with city lights or a dark sky"),
    ("detail", "細節", "a close-up of small details and textures"),
]


# ----------------------------------------------------------------------- spec


def clean_spec(s: dict) -> dict:
    def num(key, lo, hi, cast=float):
        try:
            return cast(min(max(float(s.get(key)), lo), hi))
        except (TypeError, ValueError):
            return DEFAULT_SPEC[key]

    w, h = num("frame_w", 5, 500), num("frame_h", 5, 500)
    return {
        "count": num("count", 1, 200, int),
        "frame_w": max(w, h),
        "frame_h": min(w, h),
        "orientation": s.get("orientation") if s.get("orientation") in ("both", "landscape", "portrait") else "both",
        "pool": num("pool", 4, 96, int),
        "reserves": num("reserves", 0, 6, int),
        "min_dpi": num("min_dpi", 40, 400, int),
        "desc_max": num("desc_max", 20, 2000, int),
        "tone": s.get("tone") if s.get("tone") in submission.TONES else DEFAULT_SPEC["tone"],
        "author": str(s.get("author") or "").strip()[:40],
        "exif": s.get("exif") if s.get("exif") in submission.EXIF_POLICIES else "all",
    }


def create(ctx: Ctx, name: str, spec: dict | None = None) -> int:
    cur = ctx.db.run("INSERT INTO exhibitions(name, created_at, spec) VALUES (?,?,?)",
                     (name.strip()[:80] or "攝影展", time.time(), json.dumps(clean_spec({**DEFAULT_SPEC, **(spec or {})}), ensure_ascii=False)))  # fmt: skip
    return cur.lastrowid


def get(ctx: Ctx, xid: int) -> dict:
    r = ctx.db.one("SELECT * FROM exhibitions WHERE id=?", (xid,))
    if r is None:
        raise KeyError(xid)
    d = dict(r)
    for k in ("spec", "themes", "theme", "curation", "profile", "packages"):
        d[k] = json.loads(d[k]) if d[k] else None
    d["spec"] = clean_spec({**DEFAULT_SPEC, **d["spec"]})
    return d


def _put(ctx: Ctx, xid: int, **cols) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    ctx.db.run(f"UPDATE exhibitions SET {sets} WHERE id=?",
               (*(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in cols.values()), xid))  # fmt: skip


def update(ctx: Ctx, xid: int, name: str | None = None, spec: dict | None = None) -> None:
    x = get(ctx, xid)
    if name is not None and name.strip():
        _put(ctx, xid, name=name.strip()[:80])
    if spec is not None:
        _put(ctx, xid, spec=clean_spec({**x["spec"], **spec}))


def delete(ctx: Ctx, xid: int) -> None:
    ctx.db.run("DELETE FROM exhibitions WHERE id=?", (xid,))


# ---------------------------------------------------------------- printability


def print_info(width: int | None, height: int | None, recipe: dict | None, spec: dict) -> dict:
    """What a photo becomes in the frame: how much is trimmed to fit its shape, and how many dots per inch are left.

    The frame may hang either way, so an upright photo goes in an upright frame. A saved crop counts:
    those are the pixels that will be printed.
    """
    w, h = float(width or 0), float(height or 0)
    crop = (recipe or {}).get("crop") or {}
    if crop:
        w *= max(crop.get("right", 1) - crop.get("left", 0), 0.01)
        h *= max(crop.get("bottom", 1) - crop.get("top", 0), 0.01)
    if w <= 0 or h <= 0:
        return {"orientation": "landscape", "pixels": [0, 0], "dpi": 0, "trim": 0.0, "level": "low", "allowed": False}
    portrait = h > w * 1.02
    long_px, short_px = max(w, h), min(w, h)
    ratio = spec["frame_w"] / spec["frame_h"]
    if long_px / short_px > ratio:
        kept_long, kept_short = short_px * ratio, short_px
    else:
        kept_long, kept_short = long_px, long_px / ratio
    dpi = kept_long / (spec["frame_w"] / 2.54)
    trim = 1.0 - (kept_long * kept_short) / (long_px * short_px)
    allowed = spec["orientation"] == "both" or (spec["orientation"] == "portrait") == portrait
    return {
        "orientation": "portrait" if portrait else "landscape",
        "pixels": [round(kept_short), round(kept_long)] if portrait else [round(kept_long), round(kept_short)],
        "dpi": round(dpi),
        "trim": round(trim, 3),
        "level": "ok" if dpi >= GOOD_DPI else "soft" if dpi >= spec["min_dpi"] else "low",
        "allowed": allowed,
    }


def to_frame(im: Image.Image, spec: dict) -> Image.Image:
    """Cuts a picture to the frame's shape, taking the same amount from both sides."""
    w, h = im.size
    ratio = spec["frame_w"] / spec["frame_h"]
    want = (1 / ratio) if h > w * 1.02 else ratio  # width over height of the frame as this picture hangs
    if abs(w / h - want) / want < 0.005:
        return im
    if w / h > want:
        nw = round(h * want)
        box = ((w - nw) // 2, 0, (w - nw) // 2 + nw, h)
    else:
        nh = round(w / want)
        box = (0, (h - nh) // 2, w, (h - nh) // 2 + nh)
    out = im.crop(box)
    out.info.update(im.info)
    return out


def _quality(p: dict) -> float:
    return float(p.get("vlm_score") or p.get("aesthetic") or 50.0)


def eligible(ctx: Ctx, spec: dict) -> list[dict]:
    """Photos that could hang in this show: the best frame of each burst, technically sound, with pixels enough for the frame."""
    blur = ctx.settings.analysis.blur_below
    out = []
    for p in mentor.load(ctx, "p.is_best=1 AND COALESCE(p.category,'')<>'document'"):
        if (p.get("sharp") is not None and p["sharp"] < blur) or ({"overexposed", "underexposed"} & set(p["flags"])):
            continue
        p["print"] = print_info(p["width"], p["height"], p["recipe"], spec)
        if p["print"]["level"] == "low" or not p["print"]["allowed"]:
            continue
        p["q"] = _quality(p)
        out.append(p)
    return out


# --------------------------------------------------------------------- themes


def _spread(mat: np.ndarray, k: int) -> np.ndarray:
    """Spherical k-means with a fixed start, so the same library always gives the same groups."""
    first = int((mat @ mat.mean(0)).argmax())
    centers = [mat[first]]
    for _ in range(1, k):
        far = int((mat @ np.stack(centers).T).max(1).argmin())  # the photo least like any centre so far
        centers.append(mat[far])
    c = np.stack(centers)
    lab = np.zeros(len(mat), dtype=int)
    for _ in range(25):
        lab = (mat @ c.T).argmax(1)
        new = np.stack([mat[lab == j].mean(0) if (lab == j).any() else c[j] for j in range(k)])
        new /= np.linalg.norm(new, axis=1, keepdims=True) + 1e-9
        if np.allclose(new, c, atol=1e-4):
            break
        c = new
    return lab


def _rank(rows: list[dict], fit: dict[int, float] | None, pool: int) -> list[int]:
    """Best candidates first: how well each fits the idea, and how good it is on its own."""
    if fit:
        lo, hi = min(fit.values()), max(fit.values())
        span = (hi - lo) or 1.0
        key = lambda p: 0.55 * (fit[p["id"]] - lo) / span + 0.45 * p["q"] / 100.0  # noqa: E731
    else:
        key = lambda p: p["q"]  # noqa: E731
    return [p["id"] for p in sorted(rows, key=key, reverse=True)[:pool]]


def _candidates(ctx: Ctx, rows: list[dict], spec: dict) -> list[dict]:
    """Every way the library might be cut into a series, before anything is named."""
    by_id = {p["id"]: p for p in rows}
    _, ids, mat = ctx.vectors()
    idx = [i for i, pid in enumerate(ids) if int(pid) in by_id]
    if not idx:
        return []
    vids = [int(ids[i]) for i in idx]
    vm = mat[idx]
    need = max(4, spec["count"] // 2)
    out = []

    # 1. pictures that simply look alike
    k = int(min(max(round(len(vids) / max(spec["count"] * 0.8, 6)), 2), 8))
    if len(vids) >= need * 2:
        lab = _spread(vm, k)
        for j in range(k):
            members = [by_id[vids[i]] for i in np.where(lab == j)[0]]
            if len(members) >= need:
                center = vm[lab == j].mean(0)
                center /= np.linalg.norm(center) + 1e-9
                fit = {vids[i]: float(vm[i] @ center) for i in np.where(lab == j)[0]}
                cats = Counter(p["category"] for p in members if p.get("category")).most_common(1)
                hint = taxonomy.LABELS.get(cats[0][0], "影像") if cats else "影像"
                out.append({"key": f"look-{j}", "kind": "look", "hint": hint, "ids": _rank(members, fit, spec["pool"])})

    # 2. one place
    places = {r["id"]: r["place"] for r in ctx.db.all("SELECT id, place FROM events WHERE place IS NOT NULL")}
    by_place: dict = {}
    for p in rows:
        if places.get(p.get("event_id")):
            by_place.setdefault(places[p["event_id"]], []).append(p)
    for place, members in by_place.items():
        if len(members) >= need:
            out.append({"key": f"place-{place}", "kind": "place", "hint": place, "ids": _rank(members, None, spec["pool"])})

    # 3. one idea, wherever it was photographed
    for key, zh, en in CONCEPTS:
        got = contest.theme_scores(ctx, {"probes": [{"zh": zh, "en": en}]}, vm)
        if got is None:
            continue
        score = got[0]
        cut = max(float(np.percentile(score, 60)), 35.0)  # only photos that match the idea clearly
        members = [by_id[vids[i]] for i in np.where(score >= cut)[0]]
        if len(members) >= need:
            fit = {vids[i]: float(score[i]) for i in np.where(score >= cut)[0]}
            out.append({"key": f"idea-{key}", "kind": "idea", "hint": zh, "ids": _rank(members, fit, spec["pool"])})

    # 4. one kind of subject
    by_cat: dict = {}
    for p in rows:
        if p.get("category"):
            by_cat.setdefault(p["category"], []).append(p)
    for cat, members in by_cat.items():
        if len(members) >= need:
            out.append({"key": f"subject-{cat}", "kind": "subject", "hint": taxonomy.LABELS.get(cat, cat),
                        "ids": _rank(members, None, spec["pool"])})  # fmt: skip

    pos = {pid: i for i, pid in enumerate(vids)}
    for c in out:
        top = c["ids"][: spec["count"]]
        sub = vm[[pos[i] for i in top if i in pos]]
        center = sub.mean(0)
        c["coherence"] = float((sub @ (center / (np.linalg.norm(center) + 1e-9))).mean()) if len(sub) else 0.0
        c["quality"] = float(np.mean([by_id[i]["q"] for i in top]))
        c["score"] = min(len(c["ids"]) / spec["count"], 1.0) * c["quality"] * (0.6 + 0.4 * max(c["coherence"], 0.0))
    out.sort(key=lambda c: -c["score"])
    kept: list[dict] = []
    for c in out:  # two ideas that pick nearly the same photos are one idea
        top = set(c["ids"][: spec["count"]])
        if all(len(top & set(o["ids"][: spec["count"]])) / max(len(top | set(o["ids"][: spec["count"]])), 1) < 0.6 for o in kept):
            kept.append(c)
    return kept[:6]


NAME_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "pitch": {"type": "string"}, "thread": {"type": "string"}},
    "required": ["title", "pitch", "thread"],
    "additionalProperties": False,
}
KIND_ZH = {"look": "畫面風格相近", "place": "同一個地方", "idea": "同一個概念", "subject": "同一種題材", "custom": "你提出的主題"}


def _sheet(ctx: Ctx, ids: list[int], numbers: list[int], cols: int = 4, cell: int = 384) -> bytes:
    tiles = []
    for pid in ids:
        with Image.open(ctx.thumb_path(pid)) as im:
            tiles.append(im.convert("RGB"))
    return imaging.jpeg_bytes(editing.contact_sheet(tiles, numbers, cols=cols, cell=cell), 85)


def _name(ctx: Ctx, vlm, c: dict, by_id: dict, spec: dict) -> dict:
    """The on-device model looks at nine of the candidates and says what the series would be."""
    show = c["ids"][:9]
    cards = "\n".join(f"{i + 1}. {local_vlm.card(by_id[pid])}" for i, pid in enumerate(show))
    user = (
        f"一位攝影者要辦個展，展出 {spec['count']} 張作品，需要一個有主軸的系列。"
        f"下面這 9 張是其中一個可能的方向（{KIND_ZH[c['kind']]}：{c['hint']}），總表上有編號，下面是每張的說明。\n{cards}\n\n"
        "- title：這個系列的名稱，8 個字以內，要有畫面感，不要用「之美」「印象」這類套語\n"
        "- pitch：一句話說這個系列想讓觀眾看到什麼\n"
        "- thread：這些照片在視覺上是靠什麼串在一起的（色調、光線、構圖或題材），一句話"
    )
    d = vlm.chat_json("你是一位攝影策展人。只輸出 JSON，用繁體中文。", user, [_sheet(ctx, show, list(range(1, len(show) + 1)), cols=3, cell=320)],
                      NAME_SCHEMA, max_tokens=400)  # fmt: skip
    return {k: str(d.get(k, "")).strip()[:n] for k, n in (("title", 20), ("pitch", 160), ("thread", 160))}


def propose(ctx: Ctx, xid: int, use_model: bool = True, progress=lambda done, total, msg: None) -> list[dict]:
    """Series ideas found in the library. Grouping is arithmetic; only the naming uses the on-device model."""
    x = get(ctx, xid)
    spec = x["spec"]
    rows = eligible(ctx, spec)
    by_id = {p["id"]: p for p in rows}
    cands = _candidates(ctx, rows, spec)
    vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge") if use_model else None
    out = []
    for i, c in enumerate(cands):
        progress(i, len(cands), f"為候選主題命名：{c['hint']}")
        named = {"title": c["hint"], "pitch": "", "thread": ""}
        by = None
        if vlm is not None:
            try:
                got = _name(ctx, vlm, c, by_id, spec)
                if got["title"]:
                    named, by = got, vlm.model
            except local_vlm.LocalModelError:
                vlm = None  # not running: the remaining ideas keep their plain names
        out.append({"key": c["key"], "kind": c["kind"], "kind_zh": KIND_ZH[c["kind"]], "hint": c["hint"], **named, "named_by": by,
                    "count": len(c["ids"]), "enough": len(c["ids"]) >= spec["count"], "quality": round(c["quality"]),
                    "cover": c["ids"][:8], "photo_ids": c["ids"]})  # fmt: skip
    _put(ctx, xid, themes={"at": time.time(), "eligible": len(rows), "ideas": out})
    return out


def choose(ctx: Ctx, xid: int, key: str | None = None, text: str | None = None) -> dict:
    """Settles the series: one of the proposals, or the user's own words matched against the library."""
    x = get(ctx, xid)
    spec = x["spec"]
    if key:
        idea = next((t for t in (x["themes"] or {}).get("ideas", []) if t["key"] == key), None)
        if idea is None:
            raise ValueError("找不到這個候選主題，請重新提案。")
        theme = {k: idea[k] for k in ("key", "kind", "title", "pitch", "thread")}
        theme["photo_ids"] = idea["photo_ids"]
    else:
        words = (text or "").strip()
        if not words:
            raise ValueError("請輸入主題。")
        rows = eligible(ctx, spec)
        by_id = {p["id"]: p for p in rows}
        _, ids, mat = ctx.vectors()
        idx = [i for i, pid in enumerate(ids) if int(pid) in by_id]
        got = contest.theme_scores(ctx, {"probes": [{"zh": words, "en": words}]}, mat[idx]) if idx else None
        if got is None:
            raise ValueError("相片庫裡沒有可以比對的照片，請先分析。")
        fit = {int(ids[i]): float(s) for i, s in zip(idx, got[0])}
        theme = {"key": "custom", "kind": "custom", "title": words[:20], "pitch": words[:200], "thread": "",
                 "photo_ids": _rank(rows, fit, spec["pool"])}  # fmt: skip
    theme.update(excluded=[], pinned=[])
    _put(ctx, xid, theme=theme, curation=None)
    ctx.db.run("DELETE FROM exhibition_works WHERE exhibition_id=?", (xid,))
    return theme


def set_pool(ctx: Ctx, xid: int, excluded: list[int] | None = None, pinned: list[int] | None = None) -> None:
    x = get(ctx, xid)
    if not x["theme"]:
        raise ValueError("還沒有選定主題。")
    t = x["theme"]
    if excluded is not None:
        t["excluded"] = [int(i) for i in excluded]
    if pinned is not None:
        t["pinned"] = [int(i) for i in pinned]
    _put(ctx, xid, theme=t)


def pool(ctx: Ctx, x: dict) -> list[dict]:
    """The candidates the curator will see: pinned ones first, excluded ones gone."""
    t = x["theme"] or {}
    order = list(dict.fromkeys([*t.get("pinned", []), *t.get("photo_ids", [])]))
    order = [i for i in order if i not in set(t.get("excluded", []))][: x["spec"]["pool"]]
    if not order:
        return []
    rows = {p["id"]: p for p in mentor.load(ctx, f"p.id IN ({','.join('?' * len(order))})", tuple(order))}
    out = []
    for pid in order:
        p = rows.get(pid)
        if p:
            p["print"] = print_info(p["width"], p["height"], p["recipe"], x["spec"])
            p["q"] = _quality(p)
            out.append(p)
    return out


# -------------------------------------------------------------------- curation

CURATOR_SYSTEM = (
    "你是一位策展人，正在為一位攝影者籌備個展。你的工作不是挑出最好的單張，而是選出放在一起成立的一組作品。\n"
    "- 系列要有主軸：觀眾走完一圈，要能說出這個展在講什麼。\n"
    "- 寧可放棄一張很強但格格不入的照片，也不要讓它打斷系列。\n"
    "- 不要放兩張幾乎一樣的照片。同一個場景只留最好的一張。\n"
    "- 順序就是觀眾看到的順序：第一張要能把人帶進來，最後一張要有收尾的感覺；中間要有節奏，遠景、中景、細節交錯，不要連續幾張都是同一種畫面。\n"
    "- 作品會印得很大。程式已經檢查過解析度，標示「近看略軟」的照片除非不可取代，否則少用。\n"
    "- 說理由時要指出畫面裡的東西，不說空話。\n"
    "全部用繁體中文。"
)


def curator_schema() -> dict:
    text = {"type": "string"}
    pick = {"type": "object", "properties": {"no": {"type": "integer"}, "chapter": {"type": "integer"}, "why": text},
            "required": ["no", "chapter", "why"], "additionalProperties": False}  # fmt: skip
    return {
        "type": "object",
        "properties": {
            "title": text,
            "statement": text,
            "chapters": {"type": "array", "items": {"type": "object", "properties": {"title": text, "idea": text},
                                                    "required": ["title", "idea"], "additionalProperties": False}},
            "works": {"type": "array", "items": pick},
            "reserves": {"type": "array", "items": pick},
            "sequence_note": text,
            "weak": {"type": "array", "items": {"type": "object", "properties": {"no": {"type": "integer"}, "why": text},
                                                "required": ["no", "why"], "additionalProperties": False}},
            "gaps": {"type": "array", "items": text},
        },
        "required": ["title", "statement", "chapters", "works", "reserves", "sequence_note", "weak", "gaps"],
        "additionalProperties": False,
    }  # fmt: skip


def _request(ctx: Ctx, x: dict, cands: list[dict]) -> list:
    spec, t = x["spec"], x["theme"]
    n = min(spec["count"], len(cands))
    parts: list = [
        f"展覽名稱：{x['name']}\n攝影者選定的主軸：{t['title']}" + (f"（{t['pitch']}）" if t.get("pitch") and t["pitch"] != t["title"] else "")
        + f"\n展出 {n} 張作品，每張印成 {spec['frame_w']:g}×{spec['frame_h']:g} 公分。\n\n"
        f"下面是 {len(cands)} 張候選照片的縮圖總表，左上角有編號；總表下方是每張的說明、方向和列印解析度。\n\n"
        "請你策展：\n"
        f"- works：選出剛好 {n} 張，依展出順序排列。chapter 是它屬於第幾個章節（從 1 開始）；why 用一句話說它在系列裡的作用\n"
        "- chapters：把作品分成 2～4 個章節，同一章節的作品在順序上要排在一起。title 是章節名稱（6 個字以內），idea 是這一章在說什麼\n"
        f"- reserves：每個章節另外選最多 {spec['reserves']} 張候補，萬一有作品要更換時可以替上；why 說它能替代什麼\n"
        "- title：這個系列的名稱（8 個字以內）；statement：兩三句話的策展論述，說這個展在講什麼\n"
        "- sequence_note：一兩句話說明排序的邏輯（從哪裡開始、在哪裡轉折、怎麼收尾）\n"
        "- weak：選出的作品裡最弱的 1～3 張，說明弱在哪裡，讓攝影者知道哪幾張最該考慮更換\n"
        "- gaps：這個系列還缺什麼樣的照片（沒有就給空陣列）"
    ]
    for k in range(0, len(cands), SHEET):
        chunk = cands[k : k + SHEET]
        nums = list(range(k + 1, k + len(chunk) + 1))
        parts += [f"總表 {k // SHEET + 1}：", Img(_sheet(ctx, [p["id"] for p in chunk], nums))]
        lines = []
        for no, p in zip(nums, chunk):
            pr = p["print"]
            soft = "，近看略軟" if pr["level"] == "soft" else ""
            lines.append(f"{no}. {local_vlm.card(p)}｜{'直式' if pr['orientation'] == 'portrait' else '橫式'}｜{pr['dpi']} dpi{soft}｜地端評分 {p['q']:.0f}")
        parts.append("\n".join(lines))
    return parts


def outgoing(ctx: Ctx, xid: int) -> dict:
    """What curation would send to the external model, for the user to see first."""
    x = get(ctx, xid)
    cands = pool(ctx, x)
    prov = ctx.settings.cloud.provider
    sheets = -(-len(cands) // SHEET) if cands else 0
    return {
        "photo_ids": [p["id"] for p in cands],
        "sheets": sheets,
        "count": min(x["spec"]["count"], len(cands)),
        "provider": prov,
        "model": ctx.settings.cloud.models.get(prov, ""),
        # sheets, a line per candidate, the brief, and a reply that names every work with a reason
        "est_tokens": sheets * estimate_image_tokens(4 * 384) + 110 * len(cands) + 900 + 150 * x["spec"]["count"] + 2800 if cands else 0,
    }


def _clean_curation(d: dict, cands: list[dict], spec: dict) -> tuple[dict, list[dict], list[dict]]:
    """Turns sheet numbers back into photos and repairs what a model gets wrong: repeats, bad numbers, the wrong count."""
    ids = {i + 1: p["id"] for i, p in enumerate(cands)}
    n = min(spec["count"], len(cands))

    def s(v, limit=300):
        return str(v or "").strip()[:limit]

    chapters = [{"title": s(c.get("title"), 20), "idea": s(c.get("idea"))} for c in (d.get("chapters") or [])[:4] if s(c.get("title"))]
    if not chapters:
        chapters = [{"title": "全部作品", "idea": ""}]

    def chapter(v) -> int:
        try:
            return min(max(int(v), 1), len(chapters)) - 1
        except (TypeError, ValueError):
            return 0

    works, seen = [], set()
    for it in d.get("works") or []:
        pid = ids.get(it.get("no"))
        if pid is not None and pid not in seen and len(works) < n:
            seen.add(pid)
            works.append({"photo_id": pid, "chapter": chapter(it.get("chapter")), "why": s(it.get("why"))})
    for p in cands:  # the model chose too few: the best of the rest fill the wall, and say so
        if len(works) >= n:
            break
        if p["id"] not in seen:
            seen.add(p["id"])
            works.append({"photo_id": p["id"], "chapter": len(chapters) - 1, "why": "策展人選的張數不足，依地端排名補上。"})
    order = {w["photo_id"]: i for i, w in enumerate(works)}
    works.sort(key=lambda w: (w["chapter"], order[w["photo_id"]]))  # a chapter hangs together
    reserves, per = [], Counter()
    for it in d.get("reserves") or []:
        pid, ch = ids.get(it.get("no")), chapter(it.get("chapter"))
        if pid is not None and pid not in seen and per[ch] < spec["reserves"]:
            seen.add(pid)
            per[ch] += 1
            reserves.append({"photo_id": pid, "chapter": ch, "why": s(it.get("why"))})
    chosen = {w["photo_id"] for w in works}
    text = {
        "title": s(d.get("title"), 20),
        "statement": s(d.get("statement"), 500),
        "chapters": chapters,
        "sequence_note": s(d.get("sequence_note"), 400),
        "weak": [{"photo_id": ids[w["no"]], "why": s(w.get("why"))} for w in (d.get("weak") or [])[:3] if ids.get(w.get("no")) in chosen],
        "gaps": [s(g) for g in (d.get("gaps") or [])[:4] if s(g)],
    }
    return text, works, reserves


def curate(ctx: Ctx, xid: int, mode: str = "cloud", progress=lambda done, total, msg: None) -> dict:
    """The curator chooses the works, groups and orders them, and keeps reserves. mode: cloud | local."""
    x = get(ctx, xid)
    if not x["theme"]:
        raise ValueError("請先選定主題。")
    cands = pool(ctx, x)
    if len(cands) < 2:
        raise ValueError("候選照片太少，無法策展。")
    parts = _request(ctx, x, cands)
    schema = curator_schema()
    tokens = 0
    if mode == "cloud":
        prov = get_provider(ctx.settings)
        progress(0, 0, f"請 {prov.name} 策展")
        res = prov.generate(system=CURATOR_SYSTEM, parts=parts, schema=schema, max_tokens=12000)
        contest.log_call(ctx, res, prov.name, "exhibit_curate", None, photos=len(cands))
        data, provider, model, tokens = res.data, prov.name, res.model, res.input_tokens + res.output_tokens
    else:
        vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge")
        progress(0, 0, "地端模型策展")
        data = vlm.chat_json(CURATOR_SYSTEM + "\n只輸出 JSON。", "\n\n".join(p for p in parts if isinstance(p, str)),
                             [p.data for p in parts if isinstance(p, Img)], schema, max_tokens=4000)  # fmt: skip
        provider, model = "local", vlm.model
    text, works, reserves = _clean_curation(data, cands, x["spec"])
    if not text["title"]:
        text["title"] = x["theme"]["title"]
    text.update(provider=provider, model=model, tokens=tokens, at=time.time(), shown=[p["id"] for p in cands])
    old = {r["photo_id"]: dict(r) for r in ctx.db.all("SELECT * FROM exhibition_works WHERE exhibition_id=?", (xid,))}
    with ctx.db.tx() as c:
        c.execute("DELETE FROM exhibition_works WHERE exhibition_id=?", (xid,))
        for role, items in (("work", works), ("reserve", reserves)):
            for pos, w in enumerate(items):
                kept = old.get(w["photo_id"], {})  # words already written for a photo survive a new curation
                c.execute(
                    "INSERT INTO exhibition_works(exhibition_id, photo_id, role, chapter, position, title, description, questions, why, use_edit) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (xid, w["photo_id"], role, w["chapter"], pos, kept.get("title"), kept.get("description"), kept.get("questions"),
                     w["why"], kept.get("use_edit", 1)),
                )  # fmt: skip
    _put(ctx, xid, curation=text)
    return {"title": text["title"], "works": len(works), "reserves": len(reserves), "chapters": len(text["chapters"]), "tokens": tokens,
            "provider": provider}  # fmt: skip


# ----------------------------------------------------------------------- works


def works(ctx: Ctx, x: dict) -> list[dict]:
    """Works in hanging order, then reserves, each with what its caption card and its print need."""
    rows = ctx.db.all(
        "SELECT w.*, p.path, p.filename, p.size, p.width, p.height, p.taken_at, p.time_source, p.light, p.event_id, p.camera, p.lens, "
        "p.focal, p.aperture, p.shutter, p.iso, p.caption, p.vlm, p.group_id, "
        "(SELECT e.recipe FROM edits e WHERE e.photo_id=p.id) recipe "
        "FROM exhibition_works w JOIN photos p ON p.id=w.photo_id WHERE w.exhibition_id=? ORDER BY w.role='reserve', w.chapter, w.position, w.photo_id",
        (x["id"],),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["questions"] = json.loads(d["questions"]) if d["questions"] else []
        recipe = json.loads(d["recipe"]) if d["recipe"] else {}
        d["edited"] = bool(recipe)
        d["use_edit"] = bool(d["use_edit"]) and d["edited"]
        d["recipe"] = recipe if d["use_edit"] else {}
        d["print"] = print_info(d["width"], d["height"], d["recipe"], x["spec"])
        d["missing"] = not Path(d["path"]).exists()
        d["facts"] = submission.facts(ctx, d)
        d["gear"] = " ".join(v for v in (d["camera"], d["lens"]) if v) or ""
        out.append(d)
    n = 0
    for d in out:
        d["no"] = None
        if d["role"] == "work":
            n += 1
            d["no"] = n
    return out


def _resequence(ctx: Ctx, xid: int, ordered: list[tuple[int, int]]) -> None:
    """Writes hanging order back: [(photo_id, chapter)] for the works, already sorted."""
    with ctx.db.tx() as c:
        for pos, (pid, ch) in enumerate(ordered):
            c.execute("UPDATE exhibition_works SET position=?, chapter=?, role='work' WHERE exhibition_id=? AND photo_id=?", (pos, ch, xid, pid))


def _sequence(ctx: Ctx, xid: int) -> list[tuple[int, int]]:
    return [(r["photo_id"], r["chapter"]) for r in ctx.db.all(
        "SELECT photo_id, chapter FROM exhibition_works WHERE exhibition_id=? AND role='work' ORDER BY chapter, position, photo_id", (xid,))]  # fmt: skip


def update_work(ctx: Ctx, xid: int, pid: int, patch: dict) -> None:
    cols, vals = [], []
    for k, limit in (("title", 60), ("description", 3000)):
        if k in patch:
            cols.append(f"{k}=?")
            vals.append(str(patch[k] or "").strip()[:limit])
    if "use_edit" in patch:
        cols.append("use_edit=?")
        vals.append(1 if patch["use_edit"] else 0)
    if isinstance(patch.get("questions"), list):
        cols.append("questions=?")
        vals.append(json.dumps(patch["questions"], ensure_ascii=False))
    if cols:
        ctx.db.run(f"UPDATE exhibition_works SET {', '.join(cols)} WHERE exhibition_id=? AND photo_id=?", (*vals, xid, pid))


def move(ctx: Ctx, xid: int, pid: int, by: int) -> None:
    """One place earlier or later on the wall. At the edge of a chapter the work crosses into the next one instead of jumping over it."""
    seq = _sequence(ctx, xid)
    i = next((k for k, (p, _) in enumerate(seq) if p == pid), None)
    j = None if i is None else i + (1 if by > 0 else -1)
    if j is None or not (0 <= j < len(seq)):
        return
    if seq[i][1] != seq[j][1]:
        seq[i] = (pid, seq[j][1])
    else:
        seq[i], seq[j] = seq[j], seq[i]
    _resequence(ctx, xid, seq)


def swap(ctx: Ctx, xid: int, work_id: int, reserve_id: int) -> None:
    """A reserve takes a work's place on the wall; the work becomes a reserve. Both keep their words."""
    rows = {r["photo_id"]: r for r in ctx.db.all(
        "SELECT photo_id, role, chapter, position FROM exhibition_works WHERE exhibition_id=? AND photo_id IN (?,?)", (xid, work_id, reserve_id))}  # fmt: skip
    w, r = rows.get(work_id), rows.get(reserve_id)
    if w is None or r is None or w["role"] != "work" or r["role"] != "reserve":
        raise ValueError("只能用候補替換展出的作品。")
    with ctx.db.tx() as c:
        c.execute("UPDATE exhibition_works SET role='work', chapter=?, position=? WHERE exhibition_id=? AND photo_id=?",
                  (w["chapter"], w["position"], xid, reserve_id))  # fmt: skip
        c.execute("UPDATE exhibition_works SET role='reserve', position=? WHERE exhibition_id=? AND photo_id=?", (r["position"], xid, work_id))


def set_role(ctx: Ctx, xid: int, pid: int, role: str) -> None:
    """Takes a work off the wall into the reserves, puts a reserve on at the end of its chapter, or adds or drops a photo."""
    if role not in ("work", "reserve", "none"):
        raise ValueError("不支援的狀態")
    row = ctx.db.one("SELECT role, chapter FROM exhibition_works WHERE exhibition_id=? AND photo_id=?", (xid, pid))
    if role == "none":
        ctx.db.run("DELETE FROM exhibition_works WHERE exhibition_id=? AND photo_id=?", (xid, pid))
    elif row is None:
        if ctx.db.one("SELECT 1 FROM photos WHERE id=?", (pid,)) is None:
            raise KeyError(pid)
        last = ctx.db.val("SELECT COALESCE(MAX(chapter), 0) FROM exhibition_works WHERE exhibition_id=?", (xid,))
        ctx.db.run("INSERT INTO exhibition_works(exhibition_id, photo_id, role, chapter, position) VALUES (?,?,?,?,9999)", (xid, pid, role, last))
    else:
        ctx.db.run("UPDATE exhibition_works SET role=?, position=9999 WHERE exhibition_id=? AND photo_id=?", (role, xid, pid))
    _resequence(ctx, xid, _sequence(ctx, xid))


# ----------------------------------------------------------------------- words


def ask_background(ctx: Ctx, xid: int, pid: int) -> list[dict]:
    """The on-device model looks at one work and asks what only the photographer knows."""
    x = get(ctx, xid)
    title = (x["curation"] or {}).get("title") or (x["theme"] or {}).get("title") or x["name"]
    vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge")
    user = (
        f"這張照片要在攝影個展「{title}」展出，旁邊會有一張說明牌。有些事只有拍攝的人知道。"
        "請看著照片，問攝影師 2 個簡短的問題，幫助寫出有他個人觀點的作品說明。\n"
        "例如：當時是什麼讓他停下來拍、這個畫面對他的意義、他想讓觀眾注意到什麼。問題要針對這張照片的內容。\n"
        "- question：一句話\n- options：3 個可能的回答，每個 20 字以內，要是從畫面看來合理的不同答案"
    )
    d = vlm.chat_json("你是協助攝影師整理創作想法的編輯。只輸出 JSON，用繁體中文。", user,
                      [ctx.preview_bytes(pid, ctx.settings.local.image_px)], submission.QUESTION_SCHEMA, max_tokens=600)  # fmt: skip
    before = ctx.db.val("SELECT questions FROM exhibition_works WHERE exhibition_id=? AND photo_id=?", (xid, pid))
    old = {q["q"]: q.get("a", "") for q in (json.loads(before) if before else [])}
    out = []
    for q in (d.get("questions") or [])[:2]:
        text = str(q.get("question", "")).strip()[:120]
        opts = list(dict.fromkeys(str(o).strip()[:40] for o in (q.get("options") or []) if str(o).strip()))[:4]
        if text:
            out.append({"q": text, "options": opts, "a": old.get(text, "")})
    if not out:
        raise local_vlm.LocalModelError("地端模型沒有提出問題，請再試一次。")
    update_work(ctx, xid, pid, {"questions": out})
    return out


def write_copy(ctx: Ctx, xid: int, only_empty: bool = False, use_cloud: bool = True) -> dict:
    """A title and a caption for every work, from text alone: no photo is uploaded again."""
    x = get(ctx, xid)
    spec, cur = x["spec"], x["curation"] or {}
    ws = [w for w in works(ctx, x) if not (only_empty and w["description"] and w["title"])]
    if not ws:
        return {"written": 0, "tokens": 0, "model": "", "over_limit": 0}
    chapters = cur.get("chapters") or []

    def material(w: dict) -> str:
        lines = [f"作品編號 {w['photo_id']}", f"目前的作品名稱：{w['title'] or '（尚未命名）'}"]
        if chapters and w["chapter"] < len(chapters):
            lines.append(f"所屬章節：{chapters[w['chapter']]['title']}")
        lines.append("畫面內容：" + local_vlm.card(w))
        lines += [f"{k}：{v}" for k, v in w["facts"] if k in ("拍攝時間", "拍攝地點")]
        if w["why"]:
            lines.append(f"策展人選它的理由：{w['why']}")
        said = [q for q in w["questions"] if q.get("a")]
        lines += ["攝影師自己的說法："] + [f"　問：{q['q']}　答：{q['a']}" for q in said] if said else ["攝影師沒有補充拍攝動機。"]
        return "\n".join(lines)

    user = (
        f"展覽名稱：{x['name']}\n系列名稱：{cur.get('title') or (x['theme'] or {}).get('title', '')}\n"
        + (f"策展論述：{cur['statement']}\n" if cur.get("statement") else "")
        + f"\n請為下面 {len(ws)} 件展出作品各取一個作品名稱並寫一段作品說明，會印在作品旁的說明牌上。\n"
        f"- description：{spec['desc_max']} 個字以內（含標點），語氣{submission.TONES[spec['tone']]}。"
        "寫這張照片讓人看到什麼、感受到什麼；攝影師有說明動機時，把他的說法自然地寫進去。不要寫拍攝參數。\n"
        "- title：已經有作品名稱的請原樣回傳；尚未命名的取一個 8 字以內的名稱，同一個系列裡的名稱不要重複用同一個詞。\n"
        "- id 必須是作品編號。每件作品的說明要有各自的重點，不要套用同一個句型。\n\n"
        + "\n\n".join(material(w) for w in ws)
    )
    drafts, tokens, model = submission.draft_copy(ctx, user, {w["photo_id"] for w in ws}, spec["desc_max"], use_cloud, "exhibit_copy")
    by_id = {w["photo_id"]: w for w in ws}
    for pid, d in drafts.items():
        patch = {}
        if not (only_empty and by_id[pid]["description"]):
            patch["description"] = d["description"]
        if not by_id[pid]["title"] and d["title"]:
            patch["title"] = d["title"]
        update_work(ctx, xid, pid, patch)
    still = sum(len(d["description"]) > spec["desc_max"] for d in drafts.values())
    return {"written": len(drafts), "tokens": tokens, "model": model, "over_limit": still}


PROFILE_Q_SCHEMA = submission.QUESTION_SCHEMA
PROFILE_SCHEMA = {
    "type": "object",
    "properties": {"bio": {"type": "string"}, "statement": {"type": "string"}},
    "required": ["bio", "statement"],
    "additionalProperties": False,
}
PROFILE_SYSTEM = (
    "你是協助攝影者準備個展的編輯，要替他寫展覽海報上的個人簡介和創作自述。\n"
    "最重要的規則：只能使用提供給你的資訊。不可以編造他的職業、年資、得獎紀錄、學經歷、旅行經過或任何沒有被提到的事。"
    "資訊不多就寫短一點，寧可樸素也不要捏造。\n"
    "文字要真誠、具體，避免「用鏡頭記錄生活」「光影的詩人」這類套語。全部用繁體中文。"
)


def _facts_text(ctx: Ctx, x: dict) -> str:
    """What the library itself says about how the series was made."""
    ws = [w for w in works(ctx, x) if w["role"] == "work"]
    days = sorted(w["taken_at"][:10] for w in ws if w["time_source"] == "exif")
    places = Counter(v for w in ws for k, v in w["facts"] if k == "拍攝地點")
    gear = Counter(w["camera"] for w in ws if w["camera"])
    bits = [f"展出 {len(ws)} 張作品"]
    if days:
        bits.append(f"拍攝於 {days[0][:7]} 到 {days[-1][:7]}")
    if places:
        bits.append("地點：" + "、".join(p for p, _ in places.most_common(4)))
    if gear:
        bits.append("器材：" + "、".join(g for g, _ in gear.most_common(3)))
    return "；".join(bits)


def ask_profile(ctx: Ctx, xid: int) -> list[dict]:
    """The on-device model interviews the photographer for the poster. Text only."""
    x = get(ctx, xid)
    cur = x["curation"] or {}
    user = (
        f"一位攝影者要辦個展，系列名稱是「{cur.get('title') or (x['theme'] or {}).get('title', x['name'])}」。"
        + (f"策展論述：{cur['statement']}\n" if cur.get("statement") else "\n")
        + f"{_facts_text(ctx, x)}。\n\n"
        "展覽海報需要他的個人簡介和創作自述。請問他 4 個簡短的問題，蒐集寫這兩段文字需要、而且只有他自己知道的事："
        "他是怎麼開始拍照的、平常拍什麼、這個系列對他的意義、希望觀眾帶走什麼。\n"
        "- question：一句話\n- options：3 個常見的回答，每個 20 字以內，讓他可以直接點選，也可以自己寫"
    )
    vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge")
    d = vlm.chat_json("你是協助攝影師整理創作想法的編輯。只輸出 JSON，用繁體中文。", user, None, PROFILE_Q_SCHEMA, max_tokens=900)
    profile = x["profile"] or {}
    old = {q["q"]: q.get("a", "") for q in profile.get("questions", [])}
    qs = []
    for q in (d.get("questions") or [])[:5]:
        text = str(q.get("question", "")).strip()[:120]
        opts = list(dict.fromkeys(str(o).strip()[:40] for o in (q.get("options") or []) if str(o).strip()))[:4]
        if text:
            qs.append({"q": text, "options": opts, "a": old.get(text, "")})
    if not qs:
        raise local_vlm.LocalModelError("地端模型沒有提出問題，請再試一次。")
    profile["questions"] = qs
    _put(ctx, xid, profile=profile)
    return qs


def set_profile(ctx: Ctx, xid: int, patch: dict) -> dict:
    profile = get(ctx, xid)["profile"] or {}
    if isinstance(patch.get("questions"), list):
        profile["questions"] = patch["questions"]
    for k, limit in (("bio", 2000), ("statement", 3000)):
        if k in patch:
            profile[k] = str(patch[k] or "").strip()[:limit]
    _put(ctx, xid, profile=profile)
    return profile


def write_profile(ctx: Ctx, xid: int, use_cloud: bool = True) -> dict:
    """Drafts the biography and the statement from the interview answers. Text only."""
    x = get(ctx, xid)
    cur, profile = x["curation"] or {}, x["profile"] or {}
    said = [q for q in profile.get("questions", []) if q.get("a")]
    user = (
        f"攝影者姓名：{x['spec']['author'] or '（未提供，簡介裡用「攝影者」稱呼）'}\n展覽名稱：{x['name']}\n"
        f"系列名稱：{cur.get('title') or (x['theme'] or {}).get('title', '')}\n"
        + (f"策展論述：{cur['statement']}\n" if cur.get("statement") else "")
        + ("章節：" + "、".join(c["title"] for c in cur.get("chapters", [])) + "\n" if cur.get("chapters") else "")
        + f"從照片檔得知的事實：{_facts_text(ctx, x)}\n"
        + ("攝影者自己的回答：\n" + "\n".join(f"　問：{q['q']}　答：{q['a']}" for q in said) if said else "攝影者沒有回答任何問題。")
        + "\n\n請寫：\n"
        "- bio：個人簡介，第三人稱，120 字以內。只寫上面有根據的事\n"
        "- statement：創作自述，第一人稱，200 字以內。說這個系列在拍什麼、為什麼拍、希望觀眾看到什麼"
    )
    tokens = 0
    if use_cloud:
        prov = get_provider(ctx.settings)
        res = prov.generate(system=PROFILE_SYSTEM, parts=[user], schema=PROFILE_SCHEMA, max_tokens=4000)
        contest.log_call(ctx, res, prov.name, "exhibit_profile", None)
        data, tokens, model = res.data, res.input_tokens + res.output_tokens, res.model
    else:
        vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge")
        data, model = vlm.chat_json(PROFILE_SYSTEM + "\n只輸出 JSON。", user, None, PROFILE_SCHEMA, max_tokens=900), vlm.model
    bio, statement = str(data.get("bio", "")).strip(), str(data.get("statement", "")).strip()
    if not bio and not statement:
        raise CloudError("模型沒有寫出簡介，請再試一次。")
    profile.update(bio=bio[:2000], statement=statement[:3000], written_by=model)
    _put(ctx, xid, profile=profile)
    return {"bio": bio, "statement": statement, "tokens": tokens, "model": model, "answered": len(said)}


# ---------------------------------------------------------------------- checks


def preflight(ctx: Ctx, x: dict, ws: list[dict] | None = None) -> list[dict]:
    """What would hold up the hand-in, checked before anything is packaged."""
    spec = x["spec"]
    ws = [w for w in (ws if ws is not None else works(ctx, x)) if w["role"] == "work"]
    out: list[dict] = []

    def add(level, text):
        out.append({"level": level, "text": text})

    if not ws:
        return [{"level": "error", "text": "還沒有展出的作品。請先選定主題並策展。"}]
    if len(ws) != spec["count"]:
        add("error", f"目前有 {len(ws)} 張作品，展覽需要剛好 {spec['count']} 張。")
    if not spec["author"]:
        add("error", "還沒有填創作者姓名，說明牌上需要。")
    groups: dict = {}
    for w in ws:
        name = f"第 {w['no']} 張「{w['title'] or w['filename']}」"
        if w["missing"]:
            add("error", f"{name}的原始檔已經不在原本的位置，無法輸出。")
            continue
        pr = w["print"]
        if not pr["allowed"]:
            add("error", f"{name}是{'直式' if pr['orientation'] == 'portrait' else '橫式'}，和畫框的方向不合。")
        if pr["level"] == "low":
            add("error", f"{name}印成 {spec['frame_w']:g}×{spec['frame_h']:g} 公分只有 {pr['dpi']} dpi，會明顯模糊。")
        elif pr["level"] == "soft":
            add("warn", f"{name}印成 {spec['frame_w']:g}×{spec['frame_h']:g} 公分是 {pr['dpi']} dpi，站近看會略軟。")
        if pr["trim"] > 0.01:
            add("warn", f"{name}的比例和畫框不同，輸出時會從兩側置中裁掉 {round(pr['trim'] * 100)}%。想自己決定裁哪裡，請到後製裡裁成畫框的比例。")
        if not w["title"]:
            add("warn", f"{name}還沒有作品名稱。")
        if not w["description"]:
            add("warn", f"{name}還沒有作品說明。")
        elif len(w["description"]) > spec["desc_max"]:
            add("error", f"{name}的說明有 {len(w['description'])} 個字，超過 {spec['desc_max']} 字的上限。")
        if w["group_id"] in groups:
            add("warn", f"{name}和{groups[w['group_id']]}來自同一組連拍，畫面很接近。")
        else:
            groups[w["group_id"]] = name
    titles = Counter(w["title"] for w in ws if w["title"])
    for t, n in titles.items():
        if n > 1:
            add("warn", f"有 {n} 張作品都叫「{t}」。")
    profile = x["profile"] or {}
    if not profile.get("bio"):
        add("warn", "還沒有個人簡介，展覽海報需要。")
    if not any(o["level"] in ("error", "warn") for o in out):
        add("ok", f"{len(ws)} 張作品和文件都準備好了，可以打包。")
    return out


# --------------------------------------------------------------------- hand-in


def printable(ctx: Ctx, w: dict, spec: dict) -> Image.Image:
    """The pixels that go to the printer: the original or its saved edit, cut to the frame's shape, in sRGB."""
    im, _ = imaging.load_image(Path(w["path"]))
    if w["use_edit"]:
        im = editing.render(im, retouch.load(ctx, w["photo_id"])["recipe"])
    return submission.to_srgb(to_frame(im, spec))


def build(ctx: Ctx, xid: int, dest: str, make_zip: bool = False, progress=lambda done, total, msg: None) -> dict:
    """Everything the organiser asked for: the print files, the caption cards with the biography, as one folder."""
    from . import deck

    x = get(ctx, xid)
    spec = x["spec"]
    ws = [w for w in works(ctx, x) if w["role"] == "work" and not w["missing"]]
    if not ws:
        raise ValueError("沒有可以輸出的作品。")
    folder = submission._free(Path(dest).expanduser(), f"{submission._safe(x['name'])}_展覽交件")
    (folder / "作品").mkdir(parents=True)
    slides, total = [], 0
    for i, w in enumerate(ws, 1):
        progress(i - 1, len(ws) + 1, f"輸出作品：{w['title'] or w['filename']}")
        im = printable(ctx, w, spec)
        extra = {"icc_profile": submission._SRGB}
        exif = submission._exif_for(Path(w["path"]), spec["exif"])
        if exif:
            extra["exif"] = exif
        name = f"{i:02d}_{submission._safe(w['title']) or '未命名'}.jpg"
        target = submission._free(folder / "作品", name)
        im.save(target, "JPEG", quality=95, subsampling=0, **extra)
        total += target.stat().st_size
        portrait = im.height > im.width
        size = f"{spec['frame_h']:g}×{spec['frame_w']:g} 公分" if portrait else f"{spec['frame_w']:g}×{spec['frame_h']:g} 公分"
        slides.append({"no": i, "file": target.name, "title": w["title"] or "未命名", "gear": w["gear"], "author": spec["author"],
                       "size": f"{size}（{im.width}×{im.height} 像素）", "description": w["description"] or "",
                       "chapter": w["chapter"],
                       "image": imaging.jpeg_bytes(imaging.resized(im, 1800), 86)})  # fmt: skip
    progress(len(ws), len(ws) + 1, "製作說明牌與個人簡介")
    cur, profile = x["curation"] or {}, x["profile"] or {}
    pptx = folder / "作品說明牌與個人簡介.pptx"
    deck.build(pptx, exhibition=x["name"], series=cur.get("title") or (x["theme"] or {}).get("title", ""), author=spec["author"],
               statement=profile.get("statement") or cur.get("statement", ""), bio=profile.get("bio", ""),
               chapters=cur.get("chapters", []), slides=slides)  # fmt: skip
    text = [f"{x['name']}", f"系列：{cur.get('title', '')}", f"創作者：{spec['author']}", "", "【個人簡介】", profile.get("bio", ""), "",
            "【創作自述】", profile.get("statement") or cur.get("statement", ""), "", "【作品清單】"]  # fmt: skip
    text += [f"{s['no']:02d}　{s['title']}　{s['gear']}　{s['size']}\n　　{s['description']}" for s in slides]
    (folder / "個人簡介與作品清單.txt").write_text("\n".join(text), "utf-8")
    out = {"folder": str(folder), "zip": None, "count": len(ws), "bytes": total, "pptx": pptx.name, "pptx_bytes": pptx.stat().st_size,
           "at": time.time()}  # fmt: skip
    if make_zip:
        progress(len(ws), len(ws) + 1, "壓縮中")
        archive = str(folder) + ".zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for f in sorted(folder.rglob("*")):
                if f.is_file():
                    z.write(f, f"{folder.name}/{f.relative_to(folder)}")
        out["zip"] = archive
    log = (x["packages"] or [])[-9:] + [out]
    _put(ctx, xid, packages=log)
    progress(len(ws) + 1, len(ws) + 1, "完成")
    return out


def package_folder(ctx: Ctx, xid: int, at: float) -> Path | None:
    for p in get(ctx, xid)["packages"] or []:
        if abs(p["at"] - at) < 0.01 and Path(p["folder"]).is_dir():
            return Path(p["folder"])
    return None


# ----------------------------------------------------------------------- state


def listing(ctx: Ctx) -> list[dict]:
    out = []
    for r in ctx.db.all("SELECT id FROM exhibitions ORDER BY created_at DESC"):
        x = get(ctx, r["id"])
        n = ctx.db.val("SELECT COUNT(*) FROM exhibition_works WHERE exhibition_id=? AND role='work'", (x["id"],))
        cover = ctx.db.val("SELECT photo_id FROM exhibition_works WHERE exhibition_id=? AND role='work' ORDER BY chapter, position LIMIT 1", (x["id"],))
        out.append({"id": x["id"], "name": x["name"], "count": x["spec"]["count"], "works": n, "cover_id": cover,
                    "series": (x["curation"] or {}).get("title") or (x["theme"] or {}).get("title")})  # fmt: skip
    return out


def state(ctx: Ctx, xid: int) -> dict:
    """Everything the exhibition page shows."""
    x = get(ctx, xid)
    ws = works(ctx, x)
    cands = pool(ctx, x)
    slim = [{k: w[k] for k in ("photo_id", "role", "chapter", "no", "title", "description", "questions", "why", "use_edit", "edited",
                               "filename", "print", "missing", "facts", "gear")} for w in ws]  # fmt: skip
    theme = None
    if x["theme"]:
        theme = {k: x["theme"].get(k) for k in ("key", "kind", "title", "pitch", "thread", "excluded", "pinned")}
        theme["available"] = len(x["theme"].get("photo_ids", []))
    ideas = None
    if x["themes"]:
        ideas = {"at": x["themes"]["at"], "eligible": x["themes"]["eligible"],
                 "ideas": [{k: v for k, v in t.items() if k != "photo_ids"} for t in x["themes"]["ideas"]]}  # fmt: skip
    return {
        "id": x["id"], "name": x["name"], "spec": x["spec"], "tones": list(submission.TONES),
        "ideas": ideas,
        "theme": theme,
        "pool": [{"photo_id": p["id"], "print": p["print"], "q": round(p["q"]), "caption": p.get("caption")} for p in cands],
        "curation": x["curation"],
        "works": [w for w in slim if w["role"] == "work"],
        "reserves": [w for w in slim if w["role"] == "reserve"],
        "checks": preflight(ctx, x, ws),
        "profile": x["profile"] or {},
        "packages": [{**p, "exists": Path(p["folder"]).is_dir()} for p in (x["packages"] or [])],
    }  # fmt: skip
