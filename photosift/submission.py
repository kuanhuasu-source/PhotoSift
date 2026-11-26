"""From finalists to something you can hand in.

Contests are usually entered in two rounds, and originals of 40-50 MB are not
what the first round wants. So there are two packages:

    preliminary   small JPEGs at the size the contest asks for, sRGB, each under
                  the file-size limit, plus the written entry; a few MB in all
    full          only for entries that passed: the untouched originals (and RAW),
                  the full-size edit, and the same written entry

The written entry has two parts. Facts (date, place, camera, exposure) come
straight from the photo's own metadata. The description is drafted by a model
that is told to use only what it is given: what the picture shows, what the
jury said, and what the photographer answered when asked why they took it.
"""

from __future__ import annotations

import base64
import html
import io
import json
import re
import shutil
import time
import zipfile
from pathlib import Path

from PIL import Image, ImageCms

from . import contest, editing, imaging, local_vlm, retouch, sun
from .cloud import CloudError, get_provider
from .context import Ctx

DEFAULT_SPEC = {
    "long_edge": 3000,   # pixels on the long side of a preliminary file
    "max_mb": 5.0,       # upper bound for each preliminary file
    "exif": "all",       # all | no_gps | none
    "name": "{no}_{title}",
    "author": "",
    "desc_max": 150,     # characters allowed for a description
    "tone": "樸實",
}  # fmt: skip
NAME_PATTERNS = ["{no}_{title}", "{no}_{title}_{author}", "{author}_{title}", "{author}_{no}", "{original}"]
EXIF_POLICIES = ("all", "no_gps", "none")
STATUS = ("draft", "sent", "passed", "failed")
TONES = {"樸實": "樸實、口語，像在跟朋友說這張照片", "詩意": "含蓄、有畫面感，但不堆砌詞藻", "專業": "簡潔、精確，著重拍攝意圖與手法"}


# ----------------------------------------------------------------------- state


def spec(ctx: Ctx, cid: int) -> dict:
    """The contest's packaging settings: what the announcement said, then the defaults, then the user's edits."""
    row = ctx.db.one("SELECT brief, submission FROM contests WHERE id=?", (cid,))
    if row is None:
        raise KeyError(cid)
    from_brief = (json.loads(row["brief"]).get("spec") or {})
    saved = (json.loads(row["submission"]) if row["submission"] else {}).get("spec") or {}
    out = {**DEFAULT_SPEC, **{k: v for k, v in from_brief.items() if v}, **saved}
    return _clean_spec(out)


def _clean_spec(s: dict) -> dict:
    def num(v, lo, hi, default):
        try:
            return min(max(float(v), lo), hi)
        except (TypeError, ValueError):
            return default

    return {
        "long_edge": int(num(s.get("long_edge"), 600, 12000, DEFAULT_SPEC["long_edge"])),
        "max_mb": round(num(s.get("max_mb"), 0.2, 200, DEFAULT_SPEC["max_mb"]), 1),
        "exif": s.get("exif") if s.get("exif") in EXIF_POLICIES else "all",
        "name": s.get("name") if s.get("name") in NAME_PATTERNS else DEFAULT_SPEC["name"],
        "author": str(s.get("author") or "").strip()[:40],
        "desc_max": int(num(s.get("desc_max"), 20, 2000, DEFAULT_SPEC["desc_max"])),
        "tone": s.get("tone") if s.get("tone") in TONES else "樸實",
    }


def _saved(ctx: Ctx, cid: int) -> dict:
    v = ctx.db.val("SELECT submission FROM contests WHERE id=?", (cid,))
    return json.loads(v) if v else {}


def set_spec(ctx: Ctx, cid: int, patch: dict) -> dict:
    cur = _saved(ctx, cid)
    cur["spec"] = _clean_spec({**spec(ctx, cid), **patch})
    ctx.db.run("UPDATE contests SET submission=? WHERE id=?", (json.dumps(cur, ensure_ascii=False), cid))
    return cur["spec"]


def _log_package(ctx: Ctx, cid: int, entry: dict) -> None:
    cur = _saved(ctx, cid)
    cur.setdefault("packages", []).append(entry)
    cur["packages"] = cur["packages"][-10:]
    ctx.db.run("UPDATE contests SET submission=? WHERE id=?", (json.dumps(cur, ensure_ascii=False), cid))


def seed(ctx: Ctx, cid: int) -> int:
    """First visit: start from the jury's recommended picks, or the top of the local ranking."""
    if ctx.db.val("SELECT COUNT(*) FROM submissions WHERE contest_id=?", (cid,)):
        return 0
    c = contest.get(ctx, cid)
    picks = (c.get("summary") or {}).get("picks") or []
    if not picks:
        picks = [r["photo_id"] for r in contest.results(ctx, cid)[: int(c["brief"]["sizes"]["picks"])]]
    set_entries(ctx, cid, picks)
    return len(picks)


def set_entries(ctx: Ctx, cid: int, photo_ids: list[int]) -> None:
    """Which photos are being entered, in order. Text already written for a photo is kept."""
    ids = list(dict.fromkeys(int(p) for p in photo_ids))
    have = {r["photo_id"] for r in ctx.db.all("SELECT photo_id FROM submissions WHERE contest_id=?", (cid,))}
    titles = {}
    for r in ctx.db.all("SELECT photo_id, final FROM contest_scores WHERE contest_id=? AND final IS NOT NULL", (cid,)):
        titles[r["photo_id"]] = json.loads(r["final"]).get("title") or ""
    edited = {r["photo_id"] for r in ctx.db.all("SELECT photo_id FROM edits WHERE recipe IS NOT NULL")}
    with ctx.db.tx() as c:
        for pid in have - set(ids):
            c.execute("DELETE FROM submissions WHERE contest_id=? AND photo_id=?", (cid, pid))
        for pos, pid in enumerate(ids):
            if pid in have:
                c.execute("UPDATE submissions SET position=? WHERE contest_id=? AND photo_id=?", (pos, cid, pid))
            else:
                c.execute(
                    "INSERT INTO submissions(contest_id, photo_id, position, title, use_edit, updated_at) VALUES (?,?,?,?,?,?)",
                    (cid, pid, pos, titles.get(pid, ""), 1 if pid in edited else 0, time.time()),
                )


def update_entry(ctx: Ctx, cid: int, pid: int, patch: dict) -> None:
    cols, vals = [], []
    for k in ("title", "description"):
        if k in patch:
            cols.append(f"{k}=?")
            vals.append(str(patch[k] or "").strip()[: 60 if k == "title" else 3000])
    if "use_edit" in patch:
        cols.append("use_edit=?")
        vals.append(1 if patch["use_edit"] else 0)
    if patch.get("status") in STATUS:
        cols.append("status=?")
        vals.append(patch["status"])
    if "questions" in patch and isinstance(patch["questions"], list):
        cols.append("questions=?")
        vals.append(json.dumps(patch["questions"], ensure_ascii=False))
    if not cols:
        return
    ctx.db.run(f"UPDATE submissions SET {', '.join(cols)}, updated_at=? WHERE contest_id=? AND photo_id=?",
               (*vals, time.time(), cid, pid))  # fmt: skip


def facts(ctx: Ctx, p: dict) -> list[tuple[str, str]]:
    """What the file itself records. Nothing here is written by a model."""
    out = []
    if p.get("time_source") == "exif" and p.get("taken_at"):
        when = p["taken_at"].replace("T", " ")[:16]
        out.append(("拍攝時間", when + (f"（{sun.LABELS[p['light']]}）" if p.get("light") in ("golden", "blue") else "")))
    place = ctx.db.val("SELECT place FROM events WHERE id=?", (p["event_id"],)) if p.get("event_id") else None
    if place:
        out.append(("拍攝地點", place))
    gear = " ".join(x for x in (p.get("camera"), p.get("lens")) if x)
    if gear:
        out.append(("器材", gear))
    exposure = [f"{round(p['focal'])}mm" if p.get("focal") else "", f"f/{p['aperture']:g}" if p.get("aperture") else "",
                p.get("shutter") or "", f"ISO {p['iso']}" if p.get("iso") else ""]  # fmt: skip
    if any(exposure):
        out.append(("拍攝參數", "　".join(x for x in exposure if x)))
    if p.get("width"):
        out.append(("原始尺寸", f"{p['width']} × {p['height']} 像素"))
    return out


def entries(ctx: Ctx, cid: int) -> list[dict]:
    rows = ctx.db.all(
        "SELECT s.*, p.path, p.raw_path, p.filename, p.size, p.width, p.height, p.taken_at, p.time_source, p.light, p.event_id, "
        "p.camera, p.lens, p.focal, p.aperture, p.shutter, p.iso, p.caption, p.vlm, p.group_id, "
        "(SELECT e.recipe IS NOT NULL FROM edits e WHERE e.photo_id=p.id) edited, "
        "(SELECT c.final FROM contest_scores c WHERE c.contest_id=s.contest_id AND c.photo_id=s.photo_id) final, "
        "(SELECT c.final_rank FROM contest_scores c WHERE c.contest_id=s.contest_id AND c.photo_id=s.photo_id) final_rank "
        "FROM submissions s JOIN photos p ON p.id=s.photo_id WHERE s.contest_id=? ORDER BY s.position, s.photo_id",
        (cid,),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["questions"] = json.loads(d["questions"]) if d["questions"] else []
        d["final"] = json.loads(d["final"]) if d["final"] else None
        d["edited"] = bool(d["edited"])
        d["use_edit"] = bool(d["use_edit"]) and d["edited"]
        raw = Path(d["raw_path"]) if d["raw_path"] else None
        d["raw_size"] = raw.stat().st_size if raw and raw.exists() else 0
        d["missing"] = not Path(d["path"]).exists()
        d["facts"] = facts(ctx, d)
        out.append(d)
    return out


# ------------------------------------------------- asking the photographer why


QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}},
                "required": ["question", "options"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def ask_background(ctx: Ctx, cid: int, pid: int) -> list[dict]:
    """The on-device model looks at the photo and asks what only the photographer knows."""
    c = contest.get(ctx, cid)
    vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge")
    user = (
        f"這張照片要參加攝影比賽，主題是「{c['brief']['theme'] or '不限'}」。接下來要替它寫作品說明，"
        "但有些事只有拍攝的人知道。請看著照片，問攝影師 2 個簡短的問題，幫助寫出有他個人觀點的說明。\n"
        "例如：當時是什麼讓他停下來拍、這個畫面對他的意義、他想讓觀者注意到什麼。問題要針對這張照片的內容，不要問泛泛的問題。\n"
        "- question：一句話\n- options：3 個可能的回答，每個 20 字以內，要是從畫面看來合理的不同答案"
    )
    d = vlm.chat_json("你是協助攝影師整理創作想法的編輯。只輸出 JSON，用繁體中文。", user,
                      [ctx.preview_bytes(pid, ctx.settings.local.image_px)], QUESTION_SCHEMA, max_tokens=600)  # fmt: skip
    before = ctx.db.val("SELECT questions FROM submissions WHERE contest_id=? AND photo_id=?", (cid, pid))
    old = {q["q"]: q.get("a", "") for q in (json.loads(before) if before else [])}  # keep answers to repeated questions
    out = []
    for q in (d.get("questions") or [])[:2]:
        text = str(q.get("question", "")).strip()[:120]
        opts = list(dict.fromkeys(str(o).strip()[:40] for o in (q.get("options") or []) if str(o).strip()))[:4]
        if text:
            out.append({"q": text, "options": opts, "a": old.get(text, "")})
    if not out:
        raise local_vlm.LocalModelError("地端模型沒有提出問題，請再試一次。")
    update_entry(ctx, cid, pid, {"questions": out})
    return out


# ------------------------------------------------------------ writing the entry


COPY_SYSTEM = (
    "你是協助攝影師撰寫比賽投稿文案的編輯，用攝影師本人的口吻、以第一人稱書寫。\n"
    "最重要的規則：只能使用提供給你的資訊。不可以編造拍攝過程、等待了多久、當時的天氣、人物、對話或任何沒有被提到的經歷。"
    "攝影師沒有說明動機時，就寫畫面本身和它與主題的關係，不要替他發明感受。\n"
    "文字要真誠、具體、不浮誇，避免「時間彷彿靜止」「按下快門的瞬間」這類陳腔濫調，也不要條列拍攝參數。全部用繁體中文。"
)


def _copy_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "title": {"type": "string"}, "description": {"type": "string"}},
                    "required": ["id", "title", "description"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["entries"],
        "additionalProperties": False,
    }


def _material(e: dict) -> str:
    """Everything known about one entry, as text. This is all the writer gets: no image is sent."""
    lines = [f"作品編號 {e['photo_id']}", f"目前的作品名稱：{e['title'] or '（尚未命名）'}"]
    lines.append("畫面內容：" + local_vlm.card(e))
    for label, value in e["facts"]:
        if label in ("拍攝時間", "拍攝地點"):
            lines.append(f"{label}：{value}")
    f = e.get("final") or {}
    if f.get("strengths"):
        lines.append(f"評審認為的優點：{f['strengths']}")
    answered = [q for q in e["questions"] if q.get("a")]
    if answered:
        lines.append("攝影師自己的說法：")
        lines += [f"　問：{q['q']}　答：{q['a']}" for q in answered]
    else:
        lines.append("攝影師沒有補充拍攝動機。")
    return "\n".join(lines)


def write_copy(ctx: Ctx, cid: int, only_empty: bool = False, use_cloud: bool = True) -> dict:
    """Drafts a description (and a title where one is missing) for each entry, from text alone."""
    c = contest.get(ctx, cid)
    sp = spec(ctx, cid)
    es = [e for e in entries(ctx, cid) if not (only_empty and e["description"])]
    if not es:
        return {"written": 0, "tokens": 0, "model": "", "over_limit": 0}
    user = (
        f"比賽名稱：{c['name']}\n主題：{c['brief']['theme'] or '不限'}\n"
        + (f"{contest.brief_text(c)}\n" if contest.brief_text(c) else "")
        + f"\n請為下面 {len(es)} 件投稿作品各寫一段作品說明。\n"
        f"- description：{sp['desc_max']} 個字以內（含標點），語氣{TONES[sp['tone']]}。"
        "說明這張照片是什麼、為什麼它符合主題；攝影師有說明動機時，把他的說法自然地寫進去。\n"
        "- title：已經有作品名稱的請原樣回傳；尚未命名的才取一個 8 字以內的名稱。\n"
        "- id 必須是作品編號。每件作品的說明要有各自的重點，不要套用同一個句型。\n\n"
        + "\n\n".join(_material(e) for e in es)
    )
    schema = _copy_schema()
    tokens, model = 0, ""
    if use_cloud:
        prov = get_provider(ctx.settings)
        res = prov.generate(system=COPY_SYSTEM, parts=[user], schema=schema, max_tokens=8000)
        contest.log_call(ctx, res, prov.name, "copy", cid)
        data, tokens, model = res.data, res.input_tokens + res.output_tokens, res.model
    else:
        vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge")
        data, model = vlm.chat_json(COPY_SYSTEM + "\n只輸出 JSON。", user, None, schema, max_tokens=400 + 300 * len(es)), vlm.model
    valid = {e["photo_id"]: e for e in es}
    drafts = {}
    for it in data.get("entries") or []:
        text = str(it.get("description", "")).strip()
        if it.get("id") in valid and text:
            drafts[it["id"]] = {"description": text, "title": str(it.get("title", "")).strip()}
    if not drafts:
        raise CloudError("模型沒有回傳可用的作品說明，請再試一次。")
    # Models count characters loosely. Anything over the limit goes back once to be tightened, rather than cut mid-sentence.
    over = {pid: d for pid, d in drafts.items() if len(d["description"]) > sp["desc_max"]}
    if over:
        ask = (
            f"下面 {len(over)} 段作品說明超過了字數上限。請各自改寫到 {sp['desc_max']} 個字以內（含標點），"
            f"以 {int(sp['desc_max'] * 0.85)} 個字左右為目標，保留最重要的一兩個重點，不可以加入新的內容。title 原樣回傳。\n\n"
            + "\n\n".join(f"作品編號 {pid}\ntitle：{d['title']}\n目前有 {len(d['description'])} 個字：{d['description']}" for pid, d in over.items())
        )
        try:
            if use_cloud:
                res = prov.generate(system=COPY_SYSTEM, parts=[ask], schema=schema, max_tokens=8000)
                contest.log_call(ctx, res, prov.name, "copy", cid)
                again, tokens = res.data, tokens + res.input_tokens + res.output_tokens
            else:
                again = vlm.chat_json(COPY_SYSTEM + "\n只輸出 JSON。", ask, None, schema, max_tokens=400 + 300 * len(over))
            for it in again.get("entries") or []:
                text = str(it.get("description", "")).strip()
                if it.get("id") in over and text and len(text) < len(over[it["id"]]["description"]):
                    drafts[it["id"]]["description"] = text
        except (CloudError, local_vlm.LocalModelError):
            pass  # the longer draft is kept; the checklist will point out that it is over the limit
    for pid, d in drafts.items():
        patch = {"description": d["description"]}
        if not valid[pid]["title"] and d["title"]:
            patch["title"] = d["title"]
        update_entry(ctx, cid, pid, patch)
    still = sum(len(d["description"]) > sp["desc_max"] for d in drafts.values())
    return {"written": len(drafts), "tokens": tokens, "model": model, "over_limit": still}


# --------------------------------------------------------------------- checks


def preflight(ctx: Ctx, cid: int) -> list[dict]:
    """What would get an entry bounced, checked before anything is packaged."""
    c = contest.get(ctx, cid)
    b, sp, es = c["brief"], spec(ctx, cid), entries(ctx, cid)
    cons = b.get("constraints") or {}
    out: list[dict] = []

    def add(level, text):
        out.append({"level": level, "text": text})

    if not es:
        add("error", "還沒有選擇要投稿的作品。")
        return out
    limit = int(b["sizes"]["picks"])
    if len(es) > limit:
        add("error", f"選了 {len(es)} 件作品，超過每人 {limit} 件的上限。")
    seen_groups: dict[int, str] = {}
    for i, e in enumerate(es, 1):
        name = f"第 {i} 件「{e['title'] or e['filename']}」"
        if e["missing"]:
            add("error", f"{name}的原始檔已經不在原本的位置，無法打包。")
            continue
        if not e["title"]:
            add("warn", f"{name}還沒有作品名稱。")
        if not e["description"]:
            add("warn", f"{name}還沒有作品說明。")
        elif len(e["description"]) > sp["desc_max"]:
            add("error", f"{name}的說明有 {len(e['description'])} 個字，超過 {sp['desc_max']} 字的上限。")
        day = (e["taken_at"] or "")[:10]
        if (cons.get("date_from") or cons.get("date_to")) and e["time_source"] != "exif":
            add("error", f"{name}沒有拍攝時間的紀錄，無法證明在規定期間內拍攝。")
        elif (cons.get("date_from") and day < cons["date_from"]) or (cons.get("date_to") and day > cons["date_to"]):
            add("error", f"{name}拍攝於 {day}，不在規定的拍攝期間內。")
        long_edge = max(e["width"] or 0, e["height"] or 0)
        if cons.get("min_long_edge") and long_edge < int(cons["min_long_edge"]):
            add("error", f"{name}的長邊只有 {long_edge} 像素，低於規定的 {cons['min_long_edge']} 像素。")
        if long_edge and long_edge < sp["long_edge"]:
            add("warn", f"{name}的長邊是 {long_edge} 像素，比初審檔設定的 {sp['long_edge']} 像素小，會以原始大小輸出，不會放大。")
        if e["group_id"] in seen_groups:
            add("warn", f"{name}和{seen_groups[e['group_id']]}來自同一組連拍，畫面很接近。")
        else:
            seen_groups[e["group_id"]] = name
        if cons.get("people") == "none" and (json.loads(e["vlm"]) if e["vlm"] else {}).get("people"):
            add("warn", f"{name}畫面中有人物，比賽規定不可有清楚可見的人。")
    if sp["name"].find("{author}") >= 0 and not sp["author"]:
        add("warn", "檔名格式包含作者姓名，但還沒有填寫。")
    if not any(x["level"] == "error" for x in out) and not any(x["level"] == "warn" for x in out):
        add("ok", f"{len(es)} 件作品都符合規定，可以打包。")
    return out


# ------------------------------------------------------------------ rendering

_SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def to_srgb(im: Image.Image) -> Image.Image:
    """Phone photos are often Display P3. A jury's browser may ignore that and show them washed out, so convert."""
    icc = im.info.get("icc_profile")
    if icc:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            if "srgb" not in (ImageCms.getProfileDescription(src) or "").lower():
                im = ImageCms.profileToProfile(im, src, ImageCms.createProfile("sRGB"), outputMode="RGB")
        except (ImageCms.PyCMSError, OSError, ValueError):
            pass  # unreadable profile: the pixels are left as they are
    im.info["icc_profile"] = _SRGB
    return im


def _exif_for(path: Path, policy: str) -> bytes | None:
    if policy == "none" or imaging.is_raw(path):
        return None
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            if not len(ex):
                return None
            ex[0x0112] = 1  # pixels are saved upright
            if policy == "no_gps" and 0x8825 in ex:
                del ex[0x8825]
            return ex.tobytes()
    except Exception:
        return None


def _fit_jpeg(im: Image.Image, max_bytes: int, extra: dict) -> tuple[bytes, int, tuple[int, int]]:
    """Highest quality that fits. If even a low quality is too big, the picture is made a little smaller."""
    def enc(img, q):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True, progressive=True, **extra)
        return buf.getvalue()

    while True:
        data = enc(im, 92)
        if len(data) <= max_bytes:
            return data, 92, im.size
        lo, hi, best = 60, 91, None
        while lo <= hi:
            mid = (lo + hi) // 2
            d = enc(im, mid)
            if len(d) <= max_bytes:
                best, lo = (d, mid), mid + 1
            else:
                hi = mid - 1
        if best:
            return best[0], best[1], im.size
        im = im.resize((max(int(im.width * 0.88), 1), max(int(im.height * 0.88), 1)), Image.Resampling.LANCZOS)


def _source(ctx: Ctx, e: dict) -> Image.Image:
    """Full-resolution pixels of what is being entered: the original, or the saved edit applied to it."""
    im, _ = imaging.load_image(Path(e["path"]))
    if e["use_edit"]:
        im = editing.render(im, retouch.load(ctx, e["photo_id"])["recipe"])
    return im


def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_.")[:50]


def file_stem(sp: dict, e: dict, no: int) -> str:
    stem = sp["name"].format(no=f"{no:02d}", title=_safe(e["title"]) or "未命名", author=_safe(sp["author"]) or "作者",
                             original=Path(e["filename"]).stem)  # fmt: skip
    return _safe(stem) or f"{no:02d}"


def _free(dest: Path, name: str) -> Path:
    p = dest / name
    stem, suf, i = p.stem, p.suffix, 2
    while p.exists():
        p = dest / f"{stem}_{i}{suf}"
        i += 1
    return p


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n < 1e9 else f"{n / 1e9:.2f} GB"


def entry_document(ctx: Ctx, cid: int, es: list[dict], previews: dict[int, bytes], files: dict[int, list[str]], kind: str) -> str:
    """The written entry as one self-contained page. Opens in any browser; print it to get a PDF."""
    c = contest.get(ctx, cid)
    sp = spec(ctx, cid)
    x = html.escape
    parts = [
        "<!doctype html><html lang='zh-Hant'><meta charset='utf-8'>",
        f"<title>{x(c['name'])} 投稿作品</title>",
        "<style>body{font:15px/1.8 -apple-system,'PingFang TC','Noto Sans TC','Microsoft JhengHei',sans-serif;max-width:820px;"
        "margin:36px auto;padding:0 24px;color:#1c1c1c}h1{font-size:24px;margin:0 0 4px}h2{font-size:19px;margin:0 0 10px}"
        ".m{color:#6a6a6a;font-size:13px}.e{margin-top:34px;padding-top:26px;border-top:1px solid #ddd;page-break-inside:avoid}"
        ".e img{max-width:100%;max-height:520px;display:block;margin:0 0 14px;border-radius:3px}"
        "p{margin:0 0 12px;white-space:pre-wrap}table{border-collapse:collapse;font-size:13px;color:#444}"
        "td{padding:2px 18px 2px 0;vertical-align:top}td:first-child{color:#888;white-space:nowrap}"
        "@media print{body{margin:0;max-width:none}.e{border-top:0;page-break-before:always}.e:first-of-type{page-break-before:auto}}</style>",
        f"<h1>{x(c['name'])}</h1>",
        f"<p class='m'>主題：{x(c['brief']['theme'] or '不限')}"
        + (f"　作者：{x(sp['author'])}" if sp["author"] else "")
        + f"　共 {len(es)} 件作品（{'初審' if kind == 'prelim' else '完整檔'}）</p>",
    ]
    for i, e in enumerate(es, 1):
        img = "data:image/jpeg;base64," + base64.b64encode(previews[e["photo_id"]]).decode()
        parts.append(f"<section class='e'><h2>{i}. {x(e['title'] or '未命名')}</h2><img src='{img}' alt=''>")
        if e["description"]:
            parts.append(f"<p>{x(e['description'])}</p>")
        rows = [*e["facts"], ("檔案", "、".join(files.get(e["photo_id"], [])))]
        if e["use_edit"]:
            rows.append(("後製", "經過整體調整與裁切，未合成、未增刪畫面內容"))
        parts.append("<table>" + "".join(f"<tr><td>{x(k)}</td><td>{x(v)}</td></tr>" for k, v in rows if v) + "</table></section>")
    return "\n".join(parts)


# ------------------------------------------------------------------- packages


def build_prelim(ctx: Ctx, cid: int, dest: str, progress=lambda done, total, msg: None) -> dict:
    """Small files for the first round: resized, sRGB, each within the size limit, plus the written entry and a zip."""
    c = contest.get(ctx, cid)
    sp, es = spec(ctx, cid), [e for e in entries(ctx, cid) if not e["missing"]]
    if not es:
        raise ValueError("沒有可以打包的作品。")
    folder = _free(Path(dest).expanduser(), f"{_safe(c['name'])}_初審")
    folder.mkdir(parents=True)
    max_bytes = int(sp["max_mb"] * 1e6)
    files, previews, listing = {}, {}, []
    for i, e in enumerate(es, 1):
        progress(i - 1, len(es), f"製作初審檔：{e['title'] or e['filename']}")
        im = to_srgb(_source(ctx, e))
        previews[e["photo_id"]] = imaging.jpeg_bytes(imaging.resized(im, 1000), 80)
        small = imaging.resized(im, sp["long_edge"])
        extra = {"icc_profile": _SRGB}
        exif = _exif_for(Path(e["path"]), sp["exif"])
        if exif:
            extra["exif"] = exif
        data, quality, size = _fit_jpeg(small, max_bytes, extra)
        target = _free(folder, file_stem(sp, e, i) + ".jpg")
        target.write_bytes(data)
        files[e["photo_id"]] = [target.name]
        listing.append({"name": target.name, "bytes": len(data), "width": size[0], "height": size[1], "quality": quality,
                        "original_bytes": e["size"]})  # fmt: skip
    (folder / "作品說明.html").write_text(entry_document(ctx, cid, es, previews, files, "prelim"), "utf-8")
    archive = folder.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(folder.iterdir()):
            z.write(f, f"{folder.name}/{f.name}")
    ctx.db.run("UPDATE submissions SET status='sent' WHERE contest_id=? AND status='draft'", (cid,))
    out = {"kind": "prelim", "folder": str(folder), "zip": str(archive), "files": listing, "count": len(es),
           "bytes": archive.stat().st_size, "original_bytes": sum(e["size"] + e["raw_size"] for e in es), "at": time.time()}  # fmt: skip
    _log_package(ctx, cid, {k: out[k] for k in ("kind", "folder", "zip", "count", "bytes", "at")})
    progress(len(es), len(es), "完成")
    return out


def estimate_full(ctx: Ctx, cid: int) -> dict:
    """How big the full package will be, before committing the disk space."""
    es = [e for e in entries(ctx, cid) if e["status"] == "passed" and not e["missing"]]
    originals = sum(e["size"] for e in es)
    raws = sum(e["raw_size"] for e in es)
    edits = sum(int(e["size"] * 1.1) for e in es if e["use_edit"])  # a full-size JPEG at high quality, roughly
    return {"count": len(es), "bytes": originals + raws + edits, "originals": originals, "raws": raws, "edits": edits}


def build_full(ctx: Ctx, cid: int, dest: str, make_zip: bool = False, progress=lambda done, total, msg: None) -> dict:
    """For entries that passed the first round: untouched originals, RAW files, full-size edits and the written entry."""
    c = contest.get(ctx, cid)
    sp = spec(ctx, cid)
    es = [e for e in entries(ctx, cid) if e["status"] == "passed" and not e["missing"]]
    if not es:
        raise ValueError("請先標記通過初審的作品。")
    folder = _free(Path(dest).expanduser(), f"{_safe(c['name'])}_完整檔")
    folder.mkdir(parents=True)
    files, previews, total = {}, {}, 0
    for i, e in enumerate(es, 1):
        progress(i - 1, len(es), f"打包完整檔：{e['title'] or e['filename']}")
        stem = file_stem(sp, e, i)
        src = Path(e["path"])
        names = []
        target = _free(folder, f"{stem}_原始檔{src.suffix.lower()}")
        shutil.copy2(src, target)  # byte for byte: this is the file a contest may ask for as proof
        names.append(target.name)
        if e["raw_path"] and Path(e["raw_path"]).exists():
            raw = Path(e["raw_path"])
            rt = _free(folder, f"{stem}_RAW{raw.suffix.lower()}")
            shutil.copy2(raw, rt)
            names.append(rt.name)
        im = _source(ctx, e)
        previews[e["photo_id"]] = imaging.jpeg_bytes(imaging.resized(to_srgb(im.copy()), 1000), 80)
        if e["use_edit"]:
            full = im  # the edit at full resolution, in the original's own colour space
            extra = {}
            if full.info.get("icc_profile"):
                extra["icc_profile"] = full.info["icc_profile"]
            exif = imaging.original_exif(src)
            if exif:
                extra["exif"] = exif
            et = _free(folder, f"{stem}_修圖.jpg")
            full.save(et, "JPEG", quality=95, subsampling=0, **extra)
            names.append(et.name)
        files[e["photo_id"]] = names
        total += sum((folder / n).stat().st_size for n in names)
    (folder / "作品說明.html").write_text(entry_document(ctx, cid, es, previews, files, "full"), "utf-8")
    out = {"kind": "full", "folder": str(folder), "zip": None, "count": len(es), "bytes": total, "at": time.time(),
           "files": [n for names in files.values() for n in names]}  # fmt: skip
    if make_zip:
        progress(len(es), len(es), "壓縮中")
        archive = folder.with_suffix(".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED, allowZip64=True) as z:  # photos do not compress; just bundle
            for f in sorted(folder.iterdir()):
                z.write(f, f"{folder.name}/{f.name}")
        out["zip"] = str(archive)
    _log_package(ctx, cid, {k: out[k] for k in ("kind", "folder", "zip", "count", "bytes", "at")})
    progress(len(es), len(es), "完成")
    return out


def package_folder(ctx: Ctx, cid: int, at: float) -> Path | None:
    """Where a package built earlier was put, if it is still there."""
    for p in _saved(ctx, cid).get("packages", []):
        if abs(p["at"] - at) < 0.01 and Path(p["folder"]).is_dir():
            return Path(p["folder"])
    return None


def state(ctx: Ctx, cid: int, seed_entries: bool = True) -> dict:
    """Everything the submission panel shows."""
    c = contest.get(ctx, cid)
    if seed_entries:
        seed(ctx, cid)
    es = entries(ctx, cid)
    saved = _saved(ctx, cid)
    for e in es:  # the panel does not need the heavy columns
        for k in ("vlm", "path", "raw_path"):
            e.pop(k, None)
    return {
        "spec": spec(ctx, cid),
        "entries": es,
        "checks": preflight(ctx, cid),
        "full": estimate_full(ctx, cid),
        "packages": [{**p, "exists": Path(p["folder"]).is_dir()} for p in saved.get("packages", [])],
        "suggested": (c.get("summary") or {}).get("picks") or [],  # what the jury would enter
        "limit": int(c["brief"]["sizes"]["picks"]),
        "patterns": NAME_PATTERNS,
        "tones": list(TONES),
        "original_bytes": sum(e["size"] + e["raw_size"] for e in es),
    }
