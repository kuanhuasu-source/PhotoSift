"""Contest selection: where the on-device model and the external model work together.

    coach      external model writes the rubric for this contest      (text only)
    local      on-device model scores every candidate with it         (zero tokens)
    calibrate  external model scores a small sample; the local scores
               are then mapped onto its scale and it tells the local
               judge what it keeps getting wrong                      (a handful of images)
    finals     external model critiques and ranks only the finalists  (a few dozen images)

Each step is resumable and every external call is logged with its token cost.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import date, timedelta
from typing import Callable

import numpy as np

from . import local_vlm, taxonomy
from .cloud import CloudError, CloudResult, Img, estimate_image_tokens, get_provider
from .context import Ctx

Progress = Callable[[int, int, str], None]
Cancelled = Callable[[], bool]

DEFAULT_SIZES = {"pool": 150, "calibration": 16, "triage": 48, "finals": 12, "picks": 5}
TEACHER_BATCH = 8
FINAL_BATCH = 6
JURY_SIZE = 8
SHEET_TILES = 16
PHONE_MAKERS = ("apple", "iphone", "samsung", "google", "pixel", "xiaomi", "huawei", "oppo", "vivo", "oneplus", "realme", "zenfone")

# Used when no external model is configured, so the app still works fully offline.
DEFAULT_RUBRIC = {
    "criteria": [
        {"key": "c1", "name": "主題契合", "weight": 25, "description": "照片內容與比賽主題的關聯是否一看就懂。",
         "high": "主題明確，不需要說明就能看出與比賽主題的關係。", "low": "看不出與主題有關，或關聯很牽強。"},
        {"key": "c2", "name": "意境與故事", "weight": 25, "description": "畫面是否傳達情緒、氛圍或讓人想像背後的故事。",
         "high": "有明確的情緒或瞬間，讓人想多看幾秒。", "low": "只是記錄，沒有情緒或想像空間。"},
        {"key": "c3", "name": "構圖", "weight": 20, "description": "主體位置、線條、留白與畫面平衡。",
         "high": "主體突出，畫面乾淨，視線有引導。", "low": "主體不明、雜亂、地平線歪斜或裁切不當。"},
        {"key": "c4", "name": "光線與色彩", "weight": 15, "description": "光線的方向與質感，色彩是否協調。",
         "high": "光線有層次，色彩協調或有特色。", "low": "光線平淡、過曝或死黑，色彩混亂。"},
        {"key": "c5", "name": "技術品質", "weight": 15, "description": "對焦、清晰度、曝光與雜訊。",
         "high": "主體清晰，曝光準確。", "low": "模糊、晃動或嚴重曝光錯誤。"},
    ],
    "judge_brief": "你是攝影比賽的初審評審。先看照片是否符合主題，再看有沒有情緒與故事，最後看構圖、光線和清晰度。"
                   "一般的生活照給 4～5 分，只有明顯出色的才給 8 分以上。",
    "pitfalls": ["主體不明確", "地平線歪斜", "畫面雜亂", "主題不符"],
    "ideal": "一眼就能看出主題，並且有讓人停下來多看的情緒或瞬間。",
    "source": "builtin",
}  # fmt: skip


# ------------------------------------------------------------------ contests


def create(ctx: Ctx, name: str, theme: str, rules: str = "", notes: str = "", scope: dict | None = None,
           sizes: dict | None = None, exclude_flawed: bool = True, max_per_event: int = 0, extra: dict | None = None) -> int:  # fmt: skip
    """`extra` carries what the definition wizard learned: constraints, profile, qa, examples, insight."""
    brief = {
        "theme": theme.strip(),
        "rules": rules.strip(),
        "notes": notes.strip(),
        "scope": scope or {},
        "sizes": {**DEFAULT_SIZES, **(sizes or {})},
        "exclude_flawed": exclude_flawed,
        "max_per_event": max_per_event,
        **{k: v for k, v in (extra or {}).items() if k in BRIEF_EXTRA and v},
    }
    cur = ctx.db.run(
        "INSERT INTO contests(name, created_at, brief) VALUES (?,?,?)",
        (name.strip() or "未命名比賽", time.time(), json.dumps(brief, ensure_ascii=False)),
    )
    return cur.lastrowid


BRIEF_EXTRA = ("constraints", "profile", "qa", "examples", "insight", "spec")  # spec: what a preliminary file must be


def get(ctx: Ctx, cid: int) -> dict:
    r = ctx.db.one("SELECT * FROM contests WHERE id=?", (cid,))
    if r is None:
        raise KeyError(cid)
    d = dict(r)
    for k in ("brief", "rubric", "calibration", "summary"):
        d[k] = json.loads(d[k]) if d[k] else None
    d["brief"]["sizes"] = {**DEFAULT_SIZES, **d["brief"].get("sizes", {})}  # contests made before a size existed
    return d


def _save(ctx: Ctx, cid: int, **cols) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in cols.values()]
    ctx.db.run(f"UPDATE contests SET {sets} WHERE id=?", (*vals, cid))


def scope_sql(brief: dict) -> tuple[str, list]:
    """Which photos may enter at all: the chosen albums plus the contest's hard rules.

    Rules such as "taken this year" or "landscape orientation" are checked here from
    EXIF, at no cost, before any model looks at anything.
    """
    scope = brief.get("scope", brief) or {}
    cons = brief.get("constraints") or {}
    where, params = ["p.error IS NULL"], []
    for key, col in (("folders", "p.folder_id"), ("events", "p.event_id"), ("categories", "p.category")):
        vals = scope.get(key) or []
        if vals:
            where.append(f"{col} IN ({','.join('?' * len(vals))})")
            params += list(vals)
    if scope.get("min_rating"):
        where.append("p.rating >= ?")
        params.append(int(scope["min_rating"]))
    if cons.get("date_from") or cons.get("date_to"):
        where.append("p.time_source='exif'")  # a date rule cannot be checked without a real capture time
        if cons.get("date_from"):
            where.append("p.taken_at >= ?")
            params.append(str(cons["date_from"])[:10])
        if cons.get("date_to"):
            where.append("p.taken_at < ?")
            params.append((date.fromisoformat(str(cons["date_to"])[:10]) + timedelta(days=1)).isoformat())
    if cons.get("min_long_edge"):
        where.append("MAX(p.width, p.height) >= ?")
        params.append(int(cons["min_long_edge"]))
    shape = {"landscape": "p.width > p.height * 1.05", "portrait": "p.height > p.width * 1.05",
             "square": "ABS(p.width - p.height) <= 0.05 * p.width"}.get(cons.get("orientation"))  # fmt: skip
    if shape:
        where.append(shape)
    if cons.get("device") in ("phone", "camera"):
        phone = "(" + " OR ".join("LOWER(p.camera) LIKE ?" for _ in PHONE_MAKERS) + ")"
        where.append(phone if cons["device"] == "phone" else f"(p.camera IS NOT NULL AND NOT {phone})")
        params += [f"%{m}%" for m in PHONE_MAKERS]
    if cons.get("people") == "none":
        # Known to show nobody. Photos the local model has not described yet are kept and checked later.
        where.append("COALESCE(json_extract(p.vlm, '$.people'), 0) = 0")
    return " AND ".join(where), params


def scope_count(ctx: Ctx, brief: dict) -> int:
    w, p = scope_sql(brief)
    return ctx.db.val(f"SELECT COUNT(*) FROM photos p WHERE {w}", p) or 0


# ------------------------------------------------- recall: who gets a local look


def _probe_vectors(ctx: Ctx, texts: list[str]):
    """Text probes as vectors, cached for the life of the process."""
    if not texts:
        return None
    emb, _, _ = ctx.vectors()
    cache = ctx.__dict__.setdefault("_probe_cache", {})
    key = (emb.name, tuple(texts))
    if key not in cache:
        cache[key] = emb.texts(texts)
    return cache[key]


def _probe_text(p) -> str:
    return (p.get("en") or p.get("zh") or "").strip() if isinstance(p, dict) else str(p).strip()


def theme_scores(ctx: Ctx, rubric: dict, vecs: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[str]] | None:
    """How well each photo matches any of the rubric's picture descriptions.

    Returns (score 0-100, index of the best probe, per-probe match matrix). The probes are
    different readings of the theme, so a photo only has to fit one of them well.
    """
    probes = [p for p in (rubric.get("probes") or []) if _probe_text(p)]
    if not probes or not len(vecs):
        return None
    emb, _, _ = ctx.vectors()
    pos = vecs @ _probe_vectors(ctx, [_probe_text(p) for p in probes]).T
    prob = emb.match_prob(pos)
    if prob is not None:
        best = prob.max(axis=1)
        score = np.clip((np.log10(np.maximum(best, 1e-12)) + 4.5) / 3.5, 0.0, 1.0) * 100.0
        avoid = [p for p in (rubric.get("avoid") or []) if _probe_text(p)]
        if avoid:
            bad = emb.match_prob(vecs @ _probe_vectors(ctx, [_probe_text(p) for p in avoid]).T).max(axis=1)
            score = np.where(bad > best * 3.0, score * 0.6, score)  # reads more like what the rubric warns against
        strength = prob
    else:  # an embedding model without calibrated probabilities: rank instead
        best = pos.max(axis=1)
        score = taxonomy.to_percentile(best)
        strength = pos
    return score.astype(np.float32), strength, [p.get("zh") or _probe_text(p) if isinstance(p, dict) else str(p) for p in probes]


def rank_candidates(ctx: Ctx, c: dict) -> list[dict]:
    """Every eligible photo, best bet first, with the reasons it is there.

    Four signals, all free: how well it fits a reading of the theme, the generic
    quality pre-score, the user's own taste, and this contest's liked/disliked examples.
    """
    from . import taste

    b = c["brief"]
    rubric = c.get("rubric") or {}
    ex = b.get("examples") or {}
    liked, disliked = set(ex.get("liked") or []), set(ex.get("disliked") or [])
    taste.update_library(ctx)
    w, p = scope_sql(b)
    w += " AND p.is_best=1 AND COALESCE(p.flag,'') != 'reject' AND p.prefilter IS NOT NULL"
    rows = ctx.db.all(f"SELECT p.id, p.flag, p.flags, p.prefilter, p.vlm_score, p.taste FROM photos p WHERE {w}", p)
    cand = []
    for r in rows:
        pinned = r["flag"] == "pick" or r["id"] in liked
        if r["id"] in disliked and r["flag"] != "pick":
            continue  # the user already said this one does not fit
        if b.get("exclude_flawed", True) and not pinned and json.loads(r["flags"] or "[]"):
            continue
        base = r["prefilter"] if r["vlm_score"] is None else 0.5 * r["prefilter"] + 0.5 * r["vlm_score"]
        cand.append({"id": r["id"], "pinned": pinned, "base": float(base), "taste": r["taste"], "theme": None,
                     "probe": None, "pref": None, "quota": False})  # fmt: skip
    if not cand:
        return []

    if ctx.db.val("SELECT COUNT(*) FROM embeddings"):
        _, ids, vecs = ctx.vectors()
        where = {int(pid): i for i, pid in enumerate(ids)}
        have = [d for d in cand if d["id"] in where]
        sub = vecs[[where[d["id"]] for d in have]] if have else vecs[:0]
        ts = theme_scores(ctx, rubric, sub)
        if ts is not None:
            score, strength, names = ts
            for d, sc, row in zip(have, score, strength):
                j = int(row.argmax())
                d["theme"], d["probe"] = round(float(sc), 1), names[j]
            # Each reading of the theme gets a few seats, so one popular reading cannot fill the pool.
            seats = max(2, int(b["sizes"]["pool"]) // (3 * len(names)))
            floor = 1e-4 if ctx.vectors()[0].match_prob(np.zeros(1)) is not None else -1.0
            for j in range(len(names)):
                for k in np.argsort(-strength[:, j])[:seats]:
                    if strength[k, j] >= floor:
                        have[int(k)]["quota"] = True
        pos = vecs[[where[i] for i in liked if i in where]]
        neg = vecs[[where[i] for i in disliked if i in where]]
        proto = taste.prototype_score(pos, neg, sub) if len(pos) else None
        if proto is not None:
            for d, v in zip(have, taste.to_percentile(proto)):
                d["pref"] = round(float(v), 1)

    weights = {"theme": 0.40, "base": 0.30, "taste": 0.15, "pref": 0.15}
    for d in cand:
        parts = [(d[k], wt) for k, wt in weights.items() if d[k] is not None]
        d["score"] = round(sum(v * wt for v, wt in parts) / sum(wt for _, wt in parts), 1)
    cand.sort(key=lambda d: (d["pinned"], d["quota"], d["score"], d["id"]), reverse=True)
    return cand


def candidate_pool(ctx: Ctx, c: dict) -> list[int]:
    """The photos the on-device judge will score: pinned ones, each theme reading's best, then the rest by score."""
    return [d["id"] for d in rank_candidates(ctx, c)[: int(c["brief"]["sizes"]["pool"])]]


def log_call(ctx: Ctx, res: CloudResult, provider: str, purpose: str, cid: int | None, photos: int | None = None) -> None:
    """`photos` is how many photos the call showed; a contact sheet is one image but many photos."""
    ctx.db.run(
        "INSERT INTO cloud_calls(ts, provider, model, purpose, contest_id, images, photos, input_tokens, output_tokens, seconds) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (time.time(), provider, res.model, purpose, cid, res.images, res.images if photos is None else photos,
         res.input_tokens, res.output_tokens, res.seconds),
    )  # fmt: skip


def usage(ctx: Ctx, cid: int) -> dict:
    c = get(ctx, cid)
    rows = ctx.db.all(
        "SELECT purpose, COUNT(*) n, SUM(images) images, SUM(photos) photos, SUM(input_tokens) i, SUM(output_tokens) o "
        "FROM cloud_calls WHERE contest_id=? GROUP BY purpose",
        (cid,),
    )
    by = {r["purpose"]: {"calls": r["n"], "images": r["images"], "photos": r["photos"], "input": r["i"], "output": r["o"]}
          for r in rows}  # fmt: skip
    total = sum(v["input"] + v["output"] for v in by.values())
    n_scope = scope_count(ctx, c["brief"])
    # What it would have cost to show every photo in scope to the external model once.
    baseline = n_scope * (estimate_image_tokens(ctx.settings.cloud.image_px) + 150)
    seen = ctx.db.val(
        "SELECT COUNT(*) FROM contest_scores WHERE contest_id=? AND (teacher IS NOT NULL OR triage IS NOT NULL OR final IS NOT NULL)",
        (cid,),
    )
    return {
        "by_purpose": by,
        "total_tokens": total,
        "images_sent": sum(v["images"] for v in by.values()),
        "photos_seen": seen or 0,
        "scope_photos": n_scope,
        "baseline_tokens": baseline,
        "saved_pct": round((1 - total / baseline) * 100, 1) if baseline and total else None,
    }


# --------------------------------------------------------------- step 1: coach

_PROBE = {
    "type": "object",
    "properties": {"zh": {"type": "string"}, "en": {"type": "string"}},
    "required": ["zh", "en"],
    "additionalProperties": False,
}
RUBRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "weight": {"type": "integer"},
                    "description": {"type": "string"},
                    "high": {"type": "string"},
                    "low": {"type": "string"},
                },
                "required": ["name", "weight", "description", "high", "low"],
                "additionalProperties": False,
            },
        },
        "judge_brief": {"type": "string"},
        "pitfalls": {"type": "array", "items": {"type": "string"}},
        "ideal": {"type": "string"},
        "probes": {"type": "array", "items": _PROBE},
        "avoid": {"type": "array", "items": _PROBE},
        "plan": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["criteria", "judge_brief", "pitfalls", "ideal", "probes", "avoid", "plan"],
    "additionalProperties": False,
}

COACH_SYSTEM = (
    "你是資深的攝影比賽評審，也是攝影老師。你要為一場攝影比賽設計評分標準。"
    "這份標準不是給人看的：它會交給一個在個人電腦上執行的小型視覺模型，由它逐張替數百張照片打分數做初選。"
    "小模型的理解力有限，所以每一項標準都要具體、看得見、容易判斷，避免抽象的形容。全部用繁體中文。"
)

PROFILE_ZH = {"occasion": "比賽性質", "judges": "評審", "strictness": "主題的鬆緊", "goal": "參賽目標", "styles": "偏好的風格"}


def brief_text(c: dict, ctx: Ctx | None = None) -> str:
    """Everything the user told the wizard, as plain lines. Shared by the coach and the local judge."""
    b = c["brief"]
    lines = []
    for k, label in PROFILE_ZH.items():
        v = (b.get("profile") or {}).get(k)
        if v:
            lines.append(f"{label}：{'、'.join(v) if isinstance(v, list) else v}")
    for qa in (b.get("qa") or [])[:12]:
        if qa.get("q") and qa.get("a"):
            lines.append(f"問：{qa['q']}　答：{qa['a']}")
    if b.get("insight"):
        lines.append(f"從使用者挑的範例照片歸納出的偏好：{b['insight']}")
    ex = b.get("examples") or {}
    if ctx is not None and (ex.get("liked") or ex.get("disliked")):
        def caps(ids):
            if not ids:
                return []
            q = ",".join("?" * len(ids))
            return [r[0] for r in ctx.db.all(f"SELECT caption FROM photos WHERE id IN ({q}) AND caption IS NOT NULL", list(ids))]

        good, bad = caps(ex.get("liked") or [])[:6], caps(ex.get("disliked") or [])[:6]
        if good:
            lines.append("使用者認為符合的照片：" + "；".join(good))
        if bad:
            lines.append("使用者認為不符合的照片：" + "；".join(bad))
    return "\n".join(lines)


def _library_summary(ctx: Ctx, brief: dict) -> str:
    w, p = scope_sql(brief)
    cats = ctx.db.all(f"SELECT p.category c, COUNT(*) n FROM photos p WHERE {w} GROUP BY p.category ORDER BY n DESC", p)
    lines = ["相片庫概況（依內容分類的張數）：" + "、".join(f"{taxonomy.LABELS.get(r['c'], r['c'] or '未分類')} {r['n']}" for r in cats[:10])]
    caps = ctx.db.all(
        f"SELECT p.caption FROM photos p WHERE {w} AND p.caption IS NOT NULL ORDER BY p.prefilter DESC LIMIT 15", p
    )
    if caps:
        lines.append("其中幾張照片的內容：\n" + "\n".join(f"- {r['caption']}" for r in caps))
    return "\n".join(lines)


def _probes(items, limit: int) -> list[dict]:
    out = []
    for it in (items or [])[:limit]:
        zh = str(it.get("zh", "") if isinstance(it, dict) else it).strip()[:80]
        en = str(it.get("en", "") if isinstance(it, dict) else "").strip()[:160]
        if zh or en:
            out.append({"zh": zh or en, "en": en or zh})
    return out


def normalise_rubric(d: dict, source: str) -> dict:
    crit = []
    for i, c in enumerate((d.get("criteria") or [])[:6]):
        try:
            w = max(int(c.get("weight", 0)), 1)
        except Exception:
            w = 1
        crit.append({
            "key": f"c{i + 1}", "name": str(c.get("name", f"標準 {i + 1}"))[:20], "weight": w,
            "description": str(c.get("description", ""))[:200], "high": str(c.get("high", ""))[:200],
            "low": str(c.get("low", ""))[:200],
        })  # fmt: skip
    if len(crit) < 2:
        raise CloudError("外部模型回傳的評分標準不完整，請再試一次。")
    total = sum(c["weight"] for c in crit)
    for c in crit:
        c["weight"] = round(c["weight"] * 100 / total)
    crit[0]["weight"] += 100 - sum(c["weight"] for c in crit)  # rounding remainder
    return {
        "criteria": crit,
        "judge_brief": str(d.get("judge_brief", ""))[:600],
        "pitfalls": [str(x)[:60] for x in (d.get("pitfalls") or [])][:8],
        "ideal": str(d.get("ideal", ""))[:300],
        "probes": _probes(d.get("probes"), 12),
        "avoid": _probes(d.get("avoid"), 5),
        "plan": [str(x)[:200] for x in (d.get("plan") or [])][:6],
        "source": source,
    }


def _local_probes(ctx: Ctx, c: dict) -> list[dict]:
    """Offline: the on-device model writes the picture descriptions. Falls back to the theme itself."""
    theme = c["brief"]["theme"]
    if not theme:
        return []
    schema = {"type": "object", "properties": {"probes": {"type": "array", "items": _PROBE}}, "required": ["probes"],
              "additionalProperties": False}  # fmt: skip
    user = (
        f"攝影比賽的主題是「{theme}」。\n{brief_text(c, ctx)}\n\n"
        "請寫出 6～8 個符合這個主題的照片可能呈現的畫面，要涵蓋不同的詮釋方式。"
        "每個用一句具體的畫面描述：zh 用繁體中文，en 用英文（像圖片說明那樣，例如 an empty street at dawn with soft light）。"
    )
    try:
        d = local_vlm.LocalVLM(ctx.settings.local, role="judge").chat_json("你是攝影比賽的策展人，只輸出 JSON。", user, None, schema, max_tokens=700)
        got = _probes(d.get("probes"), 10)
        if got:
            return got
    except local_vlm.LocalModelError:
        pass
    return [{"zh": theme, "en": theme}]


def coach(ctx: Ctx, cid: int, use_cloud: bool = True) -> dict:
    """Ask the external model for a rubric. Falls back to the built-in one when told to stay offline."""
    c = get(ctx, cid)
    b = c["brief"]
    if not use_cloud:
        rubric = {**DEFAULT_RUBRIC, "probes": _local_probes(ctx, c), "avoid": [], "plan": []}
    else:
        known = brief_text(c, ctx)
        user = (
            f"比賽名稱：{c['name']}\n比賽主題：{b['theme'] or '（未指定，一般攝影比賽）'}\n"
            f"比賽規則：{b['rules'] or '（無）'}\n主辦單位或評審重視的地方：{b['notes'] or '（未說明）'}\n"
            + (f"\n參賽者在問答中提供的資訊：\n{known}\n" if known else "")
            + f"\n{_library_summary(ctx, b)}\n\n"
            "請設計 4～6 項評分標準：\n"
            "- weight 是比重，全部加起來 100；要反映上面的評審類型和參賽目標。沒有特別說明時，"
            "意境、情感、故事性這類決定作品能不能打動評審的面向，比重不要低於技術面。\n"
            "- description 用一句話說明這項在看什麼。\n"
            "- high 描述 9～10 分的照片長什麼樣子，low 描述 1～3 分的樣子，都要是畫面上看得出來的特徵。\n"
            "- judge_brief：給小模型的評審須知，200 字以內，直接告訴它要怎麼看、常見的誤判要怎麼避免、分數要怎麼拉開差距。\n"
            "- pitfalls：這個主題常見的扣分點。\n"
            "- ideal：一句話描述理想的得獎作品。\n"
            "- probes：8～12 個「可能得獎的照片會呈現的畫面」，用來從幾千張照片裡用文字比對找出候選。"
            "要涵蓋這個主題的不同詮釋方式，彼此不要重複。它們是用來找出相片庫裡「其他」同類的照片，"
            "所以要寫一類畫面的共通特徵，不要照抄上面列出的個別照片。每個都是一句具體的畫面描述："
            "zh 用繁體中文，en 用英文（像圖片說明，例如 an empty street at dawn with soft light）。\n"
            "- avoid：3～5 個明顯離題或會被扣分的畫面，格式同上。\n"
            "- plan：用 3～5 句白話，告訴參賽者你會怎麼評他的照片（最重視什麼、什麼會扣分），讓他確認你理解得對不對。"
        )
        prov = get_provider(ctx.settings)
        res = prov.generate(system=COACH_SYSTEM, parts=[user], schema=RUBRIC_SCHEMA, max_tokens=8000)
        log_call(ctx, res, prov.name, "coach", cid)
        rubric = normalise_rubric(res.data, f"{prov.name}:{res.model}")
        if not rubric["probes"] and b["theme"]:
            rubric["probes"] = [{"zh": b["theme"], "en": b["theme"]}]
    rubric["id"] = hashlib.sha1(json.dumps(rubric, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]
    # A new rubric invalidates every score made with the old one.
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM contest_scores WHERE contest_id=?", (cid,))
        conn.execute(
            "UPDATE contests SET rubric=?, calibration=NULL, summary=NULL WHERE id=?",
            (json.dumps(rubric, ensure_ascii=False), cid),
        )
    return rubric


# --------------------------------------------------------- step 2: local judge


def _score_schema(rubric: dict, extra: dict[str, dict]) -> dict:
    props = {c["key"]: {"type": "integer"} for c in rubric["criteria"]}
    props.update(extra)
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _criteria_text(rubric: dict) -> str:
    return "\n".join(
        f"- {c['key']}「{c['name']}」（比重 {c['weight']}%）：{c['description']} 高分：{c['high']} 低分：{c['low']}"
        for c in rubric["criteria"]
    )


def weighted_total(rubric: dict, scores: dict) -> float:
    return round(sum(c["weight"] * local_vlm.clamp_score(scores.get(c["key"])) for c in rubric["criteria"]) / 10.0, 1)


def _anchors(ctx: Ctx, cid: int) -> str:
    """Three photos the senior judge has scored (high, middle, low), described in words, as reference points."""
    rows = ctx.db.all(
        "SELECT p.caption, s.teacher, s.teacher_total FROM contest_scores s JOIN photos p ON p.id=s.photo_id "
        "WHERE s.contest_id=? AND s.teacher IS NOT NULL AND p.caption IS NOT NULL ORDER BY s.teacher_total DESC",
        (cid,),
    )
    if len(rows) < 3:
        return ""
    picks = [rows[0], rows[len(rows) // 2], rows[-1]]
    return "\n".join(
        f"- 「{r['caption']}」→ {r['teacher_total']:.0f} 分。{json.loads(r['teacher']).get('comment', '')}" for r in picks
    )


def local_prompt(c: dict, ctx: Ctx | None = None) -> tuple[str, str]:
    rubric, b = c["rubric"], c["brief"]
    system = rubric["judge_brief"] + "\n只輸出 JSON，用繁體中文。"
    notes = (c.get("calibration") or {}).get("notes")
    if notes:
        system += f"\n\n資深評審看過你之前的評分後給的提醒，請照著修正：\n{notes}"
        anchors = _anchors(ctx, c["id"]) if ctx is not None else ""
        if anchors:
            system += f"\n\n資深評審實際給過的分數，當作你的參考基準：\n{anchors}"
    known = brief_text(c, ctx)
    user = (
        f"比賽主題：{b['theme'] or '一般攝影比賽'}\n"
        + (f"關於這場比賽：\n{known}\n" if known else "")
        + f"\n評分標準（每項 1～10 分）：\n{_criteria_text(rubric)}\n\n"
        "請為這張照片的每一項打分數，並在 reason 用一句話說出它最主要的優點或缺點。"
    )
    return system, user


def score_local(ctx: Ctx, cid: int, progress: Progress = lambda *a: None, cancelled: Cancelled = lambda: False,
                rescore: bool = False) -> dict:  # fmt: skip
    c = get(ctx, cid)
    if not c["rubric"]:
        raise ValueError("請先產生評分標準。")
    rubric = c["rubric"]
    ranked = rank_candidates(ctx, c)[: int(c["brief"]["sizes"]["pool"])]
    why = {d["id"]: d for d in ranked}
    pool = [d["id"] for d in ranked]
    done_ids = set()
    if not rescore:
        done_ids = {r[0] for r in ctx.db.all(
            "SELECT photo_id FROM contest_scores WHERE contest_id=? AND local IS NOT NULL", (cid,))}  # fmt: skip
    todo = [pid for pid in pool if pid not in done_ids]
    vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge")
    system, user = local_prompt(c, ctx)
    schema = _score_schema(rubric, {"reason": {"type": "string"}})
    px = ctx.settings.local.image_px
    n, failed, t0 = 0, 0, time.time()
    progress(0, len(todo), f"地端評審（{vlm.model}）")
    for pid in todo:
        if cancelled():
            break
        try:
            d = vlm.chat_json(system, user, [ctx.preview_bytes(pid, px)], schema, max_tokens=400)
        except local_vlm.LocalModelError:
            failed += 1
            if failed >= 5 and n == 0:
                raise  # nothing works: surface the reason instead of grinding through the pool
            continue
        except OSError:
            failed += 1
            continue
        scores = {cr["key"]: local_vlm.clamp_score(d.get(cr["key"])) for cr in rubric["criteria"]}
        rec = {"scores": scores, "reason": str(d.get("reason", "")).strip()[:200], "model": vlm.model,
               # why this photo was a candidate at all: which reading of the theme it matched, and how well
               "theme": why[pid]["theme"], "probe": why[pid]["probe"], "pref": why[pid]["pref"]}  # fmt: skip
        ctx.db.run(
            "INSERT INTO contest_scores(contest_id, photo_id, local, local_total) VALUES (?,?,?,?) "
            "ON CONFLICT(contest_id, photo_id) DO UPDATE SET local=excluded.local, local_total=excluded.local_total",
            (cid, pid, json.dumps(rec, ensure_ascii=False), weighted_total(rubric, scores)),
        )
        n += 1
        per = (time.time() - t0) / n
        progress(n + failed, len(todo), f"地端評審（{vlm.model}，每張約 {per:.1f} 秒）")
    out = {"scored": n, "failed": failed, "pool": len(pool), "seconds": round(time.time() - t0, 1)}
    if c.get("calibration") and n:
        # The teacher's scores are already paid for: refit against them at no token cost.
        cal = refit(ctx, cid, "依提醒重評" if rescore else "補評")
        out["agreement"] = cal["rounds"][-1]["agreement"]
    return out


# -------------------------------------------------------- step 3: calibration


def spearman(a, b) -> float | None:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) < 3 or a.std() == 0 or b.std() == 0:
        return None

    def rank(x):
        order = x.argsort()
        r = np.empty(len(x))
        r[order] = np.arange(len(x), dtype=float)
        for v in np.unique(x):  # average the ranks of ties
            m = x == v
            r[m] = r[m].mean()
        return r

    return round(float(np.corrcoef(rank(a), rank(b))[0, 1]), 3)


def calibration_sample(ctx: Ctx, cid: int, n: int) -> list[int]:
    """Which photos the teacher should see: the likely finalists plus an even spread of the rest."""
    rows = ctx.db.all(
        "SELECT photo_id FROM contest_scores WHERE contest_id=? AND local_total IS NOT NULL ORDER BY local_total DESC, photo_id",
        (cid,),
    )
    ids = [r[0] for r in rows]
    if len(ids) <= n:
        return ids
    top = ids[: n // 3]
    rest = ids[n // 3 :]
    idx = np.linspace(0, len(rest) - 1, n - len(top)).round().astype(int)
    return top + [rest[i] for i in sorted(set(idx.tolist()))]


TEACHER_SYSTEM = (
    "你是資深的攝影比賽評審。請依照給定的評分標準，嚴格而一致地為每張照片評分。"
    "分數要有鑑別度：一般的生活照 4～5 分，只有真正出色的作品才給 8 分以上。全部用繁體中文。"
)


def _teacher_schema(rubric: dict, extra: dict[str, dict]) -> dict:
    item = _score_schema(rubric, {"id": {"type": "integer"}, **extra})
    return {
        "type": "object",
        "properties": {"photos": {"type": "array", "items": item}},
        "required": ["photos"],
        "additionalProperties": False,
    }


def _photo_parts(ctx: Ctx, ids: list[int]) -> list:
    parts = []
    px = ctx.settings.cloud.image_px
    for pid in ids:
        parts += [f"照片編號 {pid}：", Img(ctx.preview_bytes(pid, px))]
    return parts


def _features(ctx: Ctx, cid: int, rubric: dict) -> tuple[list[int], np.ndarray]:
    rows = ctx.db.all(
        "SELECT s.photo_id, s.local, p.aesthetic, p.tech FROM contest_scores s JOIN photos p ON p.id=s.photo_id "
        "WHERE s.contest_id=? AND s.local IS NOT NULL ORDER BY s.photo_id",
        (cid,),
    )
    keys = [c["key"] for c in rubric["criteria"]]
    ids, X = [], []
    for r in rows:
        sc = json.loads(r["local"])["scores"]
        ids.append(r["photo_id"])
        X.append([float(sc.get(k, 5)) for k in keys] + [(r["aesthetic"] or 50.0) / 10.0, (r["tech"] or 50.0) / 10.0])
    return ids, np.array(X, dtype=float).reshape(len(ids), len(keys) + 2)


def fit_calibration(X: np.ndarray, local_total: np.ndarray, y: np.ndarray) -> dict:
    """Map local scores onto the teacher's scale. Picks the simplest model that predicts held-out photos best."""
    n = len(y)

    def ridge(Xa, ya, lam=3.0):
        mu, sd = Xa.mean(axis=0), Xa.std(axis=0) + 1e-6
        Z = (Xa - mu) / sd
        w = np.linalg.solve(Z.T @ Z + lam * np.eye(Z.shape[1]), Z.T @ (ya - ya.mean()))
        return {"kind": "ridge", "mu": mu.tolist(), "sd": sd.tolist(), "w": w.tolist(), "b": float(ya.mean())}

    def linear(ta, ya):
        if ta.std() < 1e-6:
            return {"kind": "linear", "a": 0.0, "b": float(ya.mean())}
        a, b = np.polyfit(ta, ya, 1)
        return {"kind": "linear", "a": float(max(a, 0.0)), "b": float(b)}

    cands = {"identity": None, "linear": None, "ridge": None}
    loo = {k: np.zeros(n) for k in cands}
    for i in range(n):
        m = np.arange(n) != i
        loo["identity"][i] = local_total[i]
        loo["linear"][i] = predict(linear(local_total[m], y[m]), X[i : i + 1], local_total[i : i + 1])[0]
        loo["ridge"][i] = predict(ridge(X[m], y[m]), X[i : i + 1], local_total[i : i + 1])[0]
    mae = {k: float(np.abs(v - y).mean()) for k, v in loo.items()}
    # Prefer the simpler model unless the richer one is clearly better on held-out photos.
    best = "identity"
    if mae["linear"] < mae[best] - 0.5:
        best = "linear"
    if n >= 10 and mae["ridge"] < mae[best] - 1.0:
        best = "ridge"
    model = {"identity": {"kind": "identity"}, "linear": linear(local_total, y), "ridge": ridge(X, y)}[best]
    return {
        "model": model,
        "mae": {k: round(v, 1) for k, v in mae.items()},
        "agreement_before": spearman(local_total, y),
        "agreement_after": spearman(loo[best], y),
        "n": n,
    }


def predict(model: dict, X: np.ndarray, local_total: np.ndarray) -> np.ndarray:
    if model["kind"] == "ridge":
        Z = (X - np.array(model["mu"])) / np.array(model["sd"])
        out = Z @ np.array(model["w"]) + model["b"]
    elif model["kind"] == "linear":
        out = model["a"] * local_total + model["b"]
    else:
        out = local_total.astype(float)
    return np.clip(out, 0.0, 100.0)


def apply_calibration(ctx: Ctx, cid: int) -> None:
    c = get(ctx, cid)
    cal = c.get("calibration")
    if not cal:
        return
    ids, X = _features(ctx, cid, c["rubric"])
    if not ids:
        return
    totals = {r[0]: r[1] for r in ctx.db.all(
        "SELECT photo_id, local_total FROM contest_scores WHERE contest_id=? AND local IS NOT NULL", (cid,))}  # fmt: skip
    teacher = {r[0]: r[1] for r in ctx.db.all(
        "SELECT photo_id, teacher_total FROM contest_scores WHERE contest_id=? AND teacher_total IS NOT NULL", (cid,))}  # fmt: skip
    pred = predict(cal["model"], X, np.array([totals[i] for i in ids]))
    # Where the teacher has already spoken, its score stands.
    ctx.db.many(
        "UPDATE contest_scores SET calibrated=? WHERE contest_id=? AND photo_id=?",
        [(round(float(teacher.get(pid, p)), 1), cid, pid) for pid, p in zip(ids, pred)],
    )


CRITIQUE = ("strengths", "weaknesses", "title", "edit")
CRITIQUE_GUIDE = (
    "- 每一項標準的分數\n- strengths：最打動人的地方\n- weaknesses：最可能被評審扣分的地方\n"
    "- title：適合投稿用的作品名稱（8 字以內）\n- edit：一句話說明投稿前的後製方向，不需要就寫「不需調整」\n"
    "- adjust：把 edit 的方向寫成可以直接套用的調整。只能做整體調整、裁切和轉正，要符合比賽規則，"
    "並且克制：不需要的項目給 0。\n"
)


def _critique_schema(rubric: dict) -> dict:
    from . import retouch  # imported here: retouch itself depends on this module

    return _teacher_schema(rubric, {**{k: {"type": "string"} for k in CRITIQUE}, "adjust": retouch.ADJUST_SCHEMA})


def _critique(ctx: Ctx, rubric: dict, ph: dict, model: str) -> tuple[dict, float]:
    """One photo's full critique as stored. Also keeps the suggested edit for the editor."""
    from . import retouch

    scores = {cr["key"]: local_vlm.clamp_score(ph.get(cr["key"])) for cr in rubric["criteria"]}
    rec = {"scores": scores, "model": model, **{k: str(ph.get(k, "")).strip()[:300] for k in CRITIQUE}}
    rec["comment"] = "；".join(x for x in (rec["strengths"], rec["weaknesses"]) if x)[:400]
    try:
        retouch.store_cloud_adjust(ctx, ph["id"], ph.get("adjust"), rec["edit"], model)
    except OSError:
        pass  # original missing: the critique is still worth keeping
    return rec, weighted_total(rubric, scores)


def calibrate(ctx: Ctx, cid: int, progress: Progress = lambda *a: None, cancelled: Cancelled = lambda: False) -> dict:
    c = get(ctx, cid)
    rubric = c["rubric"]
    sample = calibration_sample(ctx, cid, int(c["brief"]["sizes"]["calibration"]))
    if len(sample) < 4:
        raise ValueError("地端評分的照片太少，無法校準。請先完成地端評審。")
    prov = get_provider(ctx.settings)
    from . import retouch

    # The full critique is asked for now, so a photo judged here never has to be uploaded again for the finals.
    schema = _critique_schema(rubric)
    intro = (
        f"比賽主題：{c['brief']['theme'] or '一般攝影比賽'}\n比賽規則：{c['brief']['rules'] or '（無）'}\n\n"
        f"評分標準（每項 1～10 分）：\n{_criteria_text(rubric)}\n\n"
        f"以下每張照片前面都有編號。請逐張評審：\n{CRITIQUE_GUIDE}{retouch.ADJUST_GUIDE}\n回傳的 id 必須是照片編號。"
    )
    batches = [sample[i : i + TEACHER_BATCH] for i in range(0, len(sample), TEACHER_BATCH)]
    got = 0
    progress(0, len(sample), f"外部評審校準（{prov.name}）")
    for batch in batches:
        if cancelled():
            break
        res = prov.generate(system=TEACHER_SYSTEM, parts=[intro, *_photo_parts(ctx, batch)], schema=schema)
        log_call(ctx, res, prov.name, "calibrate", cid)
        valid = set(batch)
        for ph in res.data.get("photos", []):
            if ph.get("id") not in valid:
                continue
            rec, total = _critique(ctx, rubric, ph, res.model)
            ctx.db.run(
                "UPDATE contest_scores SET teacher=?, teacher_total=? WHERE contest_id=? AND photo_id=?",
                (json.dumps(rec, ensure_ascii=False), total, cid, ph["id"]),
            )
            got += 1
        progress(got, len(sample), f"外部評審校準（{prov.name}）")

    rows = _paired(ctx, cid)
    if len(rows) < 4:
        raise CloudError("外部模型回傳的評分太少，無法校準，請再試一次。")
    cal = _fit(ctx, cid, rubric, rows)
    cal.update({"provider": prov.name, "at": time.time(), "rounds": [_round(cal, "第一輪")]})

    # Second, text-only pass: the teacher reads both sets of scores and coaches the local judge.
    try:
        cal["notes"], cal["observations"] = _coach_notes(ctx, cid, c, rows, prov)
    except CloudError:
        cal["notes"], cal["observations"] = "", []
    _save(ctx, cid, calibration=cal)
    apply_calibration(ctx, cid)
    return cal


def _paired(ctx: Ctx, cid: int):
    return ctx.db.all(
        "SELECT s.photo_id, s.local, s.local_total, s.teacher, s.teacher_total FROM contest_scores s "
        "WHERE s.contest_id=? AND s.teacher_total IS NOT NULL AND s.local IS NOT NULL ORDER BY s.photo_id",
        (cid,),
    )


def _fit(ctx: Ctx, cid: int, rubric: dict, rows) -> dict:
    ids, X = _features(ctx, cid, rubric)
    pos = {pid: i for i, pid in enumerate(ids)}
    sel = [pos[r["photo_id"]] for r in rows]
    y = np.array([r["teacher_total"] for r in rows], dtype=float)
    lt = np.array([r["local_total"] for r in rows], dtype=float)
    cal = fit_calibration(X[sel], lt, y)
    cal["sample"] = [r["photo_id"] for r in rows]
    return cal


def _round(cal: dict, label: str) -> dict:
    return {"label": label, "agreement": cal["agreement_before"], "mae": cal["mae"]["identity"], "at": time.time()}


def refit(ctx: Ctx, cid: int, label: str) -> dict:
    """Re-measure the local judge against the teacher's existing scores. Costs no tokens."""
    c = get(ctx, cid)
    old = c["calibration"]
    rows = _paired(ctx, cid)
    if len(rows) < 4:
        return old
    cal = {**old, **_fit(ctx, cid, c["rubric"], rows)}
    cal["rounds"] = [*old.get("rounds", []), _round(cal, label)]
    _save(ctx, cid, calibration=cal)
    apply_calibration(ctx, cid)
    return cal


def _coach_notes(ctx: Ctx, cid: int, c: dict, rows, prov) -> tuple[str, list[str]]:
    rubric = c["rubric"]
    names = "、".join(f"{cr['key']}={cr['name']}" for cr in rubric["criteria"])
    lines = []
    for r in rows:
        lo, te = json.loads(r["local"]), json.loads(r["teacher"])
        lines.append(
            f"照片 {r['photo_id']}｜初審 {r['local_total']:.0f} 分 {lo['scores']}「{lo['reason']}」"
            f"｜你 {r['teacher_total']:.0f} 分 {te['scores']}「{te['comment']}」"
        )
    user = (
        f"比賽主題：{c['brief']['theme'] or '一般攝影比賽'}\n評分項目：{names}\n\n"
        "下面是同一批照片的兩組評分：「初審」是一個在個人電腦上執行的小型視覺模型打的，「你」是你剛才打的。\n"
        + "\n".join(lines)
        + "\n\n請比較兩者，找出初審模型有系統的偏差（例如哪一項普遍給太高、哪類照片被高估或低估、分數是否都擠在一起）。\n"
        "- notes：直接寫給初審模型看的修正提醒，150 字以內，要具體可執行，它之後評其他照片時會照著做。\n"
        "- observations：2～4 點給使用者看的觀察，說明初審模型和你的差異。"
    )
    schema = {
        "type": "object",
        "properties": {"notes": {"type": "string"}, "observations": {"type": "array", "items": {"type": "string"}}},
        "required": ["notes", "observations"],
        "additionalProperties": False,
    }
    res = prov.generate(system=TEACHER_SYSTEM, parts=[user], schema=schema, max_tokens=6000)
    log_call(ctx, res, prov.name, "coach_notes", cid)
    return str(res.data.get("notes", ""))[:500], [str(x)[:200] for x in res.data.get("observations", [])][:5]


# ------------------------------------------------------------- step 4: finals


def _by_score(ctx: Ctx, cid: int):
    return ctx.db.all(
        "SELECT s.photo_id, COALESCE(s.calibrated, s.local_total) score, p.event_id, p.flag, s.triage FROM contest_scores s "
        "JOIN photos p ON p.id=s.photo_id WHERE s.contest_id=? AND s.local_total IS NOT NULL "
        "ORDER BY (p.flag='pick') DESC, score DESC, s.photo_id",
        (cid,),
    )


def _cap(rows, cap: int, limit: int) -> list[int]:
    """At most `cap` photos per event (0 = no cap). Photos the user picked are exempt."""
    per_event: dict = {}
    out = []
    for r in rows:
        if cap and r["flag"] != "pick":
            if per_event.get(r["event_id"], 0) >= cap:
                continue
            per_event[r["event_id"]] = per_event.get(r["event_id"], 0) + 1
        out.append(r["photo_id"])
        if len(out) >= limit:
            break
    return out


def triage_size(c: dict, scored: int) -> int:
    """How many candidates the external model glances at on contact sheets before the finals.

    The less the two judges agreed during calibration, the wider the net: tokens are
    spent where the on-device judge is least trustworthy.
    """
    sizes = c["brief"]["sizes"]
    base, finals_n = int(sizes.get("triage") or 0), int(sizes["finals"])
    if base <= 0 or scored <= finals_n:
        return 0
    rounds = (c.get("calibration") or {}).get("rounds") or []
    agree = rounds[-1].get("agreement") if rounds else None
    factor = 1.0 if agree is None or agree >= 0.7 else 1.5 if agree >= 0.5 else 2.0
    return min(int(math.ceil(base * factor / SHEET_TILES) * SHEET_TILES), scored)


def triage_candidates(ctx: Ctx, cid: int) -> list[int]:
    c = get(ctx, cid)
    rows = _by_score(ctx, cid)
    return [r["photo_id"] for r in rows[: triage_size(c, len(rows))]]


def finalists(ctx: Ctx, cid: int) -> list[int]:
    """Who goes to the finals: the external model's picks from the contact sheets if it has made them, else the top scores."""
    c = get(ctx, cid)
    b = c["brief"]
    rows = _by_score(ctx, cid)
    picked = [(json.loads(r["triage"]), r) for r in rows if r["triage"]]
    picked = [r for t, r in sorted(picked, key=lambda x: x[0].get("rank") or 999) if t.get("picked")]
    limit, cap = int(b["sizes"]["finals"]), int(b.get("max_per_event") or 0)
    if picked:
        return _cap(picked, cap, limit)
    return _cap(rows, cap, limit)


def _triage(ctx: Ctx, c: dict, ids: list[int], prov, cancelled: Cancelled) -> None:
    """The external model looks at numbered contact sheets and names the photos worth a full-size look."""
    from PIL import Image

    from . import editing, imaging

    cid, rubric, b = c["id"], c["rubric"], c["brief"]
    q = ",".join("?" * len(ids))
    info = {r["id"]: dict(r) for r in ctx.db.all(
        f"SELECT p.id, p.caption, p.vlm, p.light, COALESCE(s.calibrated, s.local_total) score FROM photos p "
        f"JOIN contest_scores s ON s.photo_id=p.id AND s.contest_id=? WHERE p.id IN ({q})", (cid, *ids))}  # fmt: skip
    number = {pid: i + 1 for i, pid in enumerate(ids)}
    want = int(b["sizes"]["finals"])
    known = brief_text(c, ctx)
    parts: list = [
        f"比賽主題：{b['theme'] or '一般攝影比賽'}\n比賽規則：{b['rules'] or '（無）'}\n"
        + (f"關於這場比賽：\n{known}\n" if known else "")
        + f"理想的得獎作品：{rubric.get('ideal', '')}\n評分重點：" + "、".join(f"{x['name']} {x['weight']}%" for x in rubric["criteria"])
        + f"\n\n下面是初選出來的 {len(ids)} 張候選照片，以縮圖總表呈現，每張的左上角有編號，總表下方有每張的文字說明。"
        "縮圖看不出清晰度，清晰度已經由程式檢查過，請專注在主題、畫面的衝擊力、構圖和氛圍。\n"
        f"請挑出最多 {want} 張最值得進入決選的照片，依優先順序排列，彼此不要太相似；why 用一句話說明理由。"
    ]
    for k in range(0, len(ids), SHEET_TILES):
        chunk = ids[k : k + SHEET_TILES]
        tiles = [Image.open(ctx.thumb_path(pid)) for pid in chunk]
        sheet = editing.contact_sheet(tiles, [number[pid] for pid in chunk])
        parts += [f"總表 {k // SHEET_TILES + 1}：", Img(imaging.jpeg_bytes(sheet, 85))]
        cards = [local_vlm.card(info[pid], "初審 %.0f 分" % info[pid]["score"]) for pid in chunk]
        parts.append("\n".join(f"{number[pid]}. {text}" for pid, text in zip(chunk, cards)))
    schema = {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"no": {"type": "integer"}, "why": {"type": "string"}},
                    "required": ["no", "why"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["picks"],
        "additionalProperties": False,
    }
    res = prov.generate(system=TEACHER_SYSTEM, parts=parts, schema=schema)
    log_call(ctx, res, prov.name, "triage", cid, photos=len(ids))
    by_no = {n: pid for pid, n in number.items()}
    out: dict[int, dict] = {pid: {"picked": False, "no": number[pid]} for pid in ids}
    rank = 0
    for it in res.data.get("picks", []):
        pid = by_no.get(it.get("no"))
        if pid is None or out[pid]["picked"]:
            continue
        rank += 1
        out[pid] = {"picked": True, "no": number[pid], "rank": rank, "why": str(it.get("why", ""))[:200]}
    if rank < min(3, want):
        raise CloudError("外部模型沒有從總表挑出足夠的照片，請再試一次。")
    ctx.db.many(
        "UPDATE contest_scores SET triage=? WHERE contest_id=? AND photo_id=?",
        [(json.dumps(v, ensure_ascii=False), cid, pid) for pid, v in out.items()],
    )


def outgoing(ctx: Ctx, cid: int, step: str) -> dict:
    """What the next external step will upload, and roughly what it will cost. Shown before the user agrees."""
    c = get(ctx, cid)
    sizes = c["brief"]["sizes"]
    one = estimate_image_tokens(ctx.settings.cloud.image_px)
    sheet = estimate_image_tokens(1568)
    if step == "calibrate":
        ids = calibration_sample(ctx, cid, int(sizes["calibration"]))
        return {"photo_ids": ids, "mode": "full", "est_tokens": len(ids) * (one + 450) + 3500}
    if step != "finals":
        raise KeyError(step)
    tri = triage_candidates(ctx, cid)
    if tri:
        sheets = math.ceil(len(tri) / SHEET_TILES)
        full = min(int(sizes["finals"]), len(tri))
        est = sheets * sheet + len(tri) * 90 + full * (one + 450) + sheet + 4000
        return {"photo_ids": tri, "mode": "sheets", "sheets": sheets, "full": full, "est_tokens": est}
    ids = finalists(ctx, cid)
    return {"photo_ids": ids, "mode": "full", "est_tokens": len(ids) * (one + 450) + sheet + 3000}


def finals(ctx: Ctx, cid: int, progress: Progress = lambda *a: None, cancelled: Cancelled = lambda: False) -> dict:
    """Contact-sheet triage, full-size critique of the chosen few, then a side-by-side jury round."""
    from PIL import Image

    from . import editing, imaging, retouch

    c = get(ctx, cid)
    rubric, b = c["rubric"], c["brief"]
    if not ctx.db.val("SELECT COUNT(*) FROM contest_scores WHERE contest_id=? AND local_total IS NOT NULL", (cid,)):
        raise ValueError("還沒有入圍照片。請先完成地端評審。")
    prov = get_provider(ctx.settings)
    ctx.db.run("UPDATE contest_scores SET final=NULL, final_total=NULL, final_rank=NULL, triage=NULL WHERE contest_id=?", (cid,))

    tri = triage_candidates(ctx, cid)
    if tri:
        progress(0, 3, f"外部評審看縮圖總表（{len(tri)} 張候選，{prov.name}）")
        _triage(ctx, c, tri, prov, cancelled)
        if cancelled():
            return {"cancelled": True}
    ids = finalists(ctx, cid)

    # A photo the external model already critiqued during calibration is not uploaded a second time.
    seen = {r["photo_id"]: (json.loads(r["teacher"]), r["teacher_total"]) for r in ctx.db.all(
        "SELECT photo_id, teacher, teacher_total FROM contest_scores WHERE contest_id=? AND teacher IS NOT NULL", (cid,))}  # fmt: skip
    reused = [pid for pid in ids if pid in seen and "strengths" in seen[pid][0]]
    for pid in reused:
        rec, total = seen[pid]
        ctx.db.run("UPDATE contest_scores SET final=?, final_total=? WHERE contest_id=? AND photo_id=?",
                   (json.dumps(rec, ensure_ascii=False), total, cid, pid))  # fmt: skip
    todo = [pid for pid in ids if pid not in set(reused)]

    schema = _critique_schema(rubric)
    intro = (
        f"比賽主題：{b['theme'] or '一般攝影比賽'}\n比賽規則：{b['rules'] or '（無）'}\n\n"
        f"評分標準（每項 1～10 分）：\n{_criteria_text(rubric)}\n\n"
        f"以下是通過初選的入圍照片，每張前面都有編號。請逐張評審：\n{CRITIQUE_GUIDE}{retouch.ADJUST_GUIDE}\n"
        "回傳的 id 必須是照片編號。"
    )
    done, total_steps = len(reused), len(ids) + 1
    progress(done, total_steps, f"外部評審決選（{prov.name}）")
    for i in range(0, len(todo), FINAL_BATCH):
        if cancelled():
            return {"cancelled": True}
        batch = todo[i : i + FINAL_BATCH]
        res = prov.generate(system=TEACHER_SYSTEM, parts=[intro, *_photo_parts(ctx, batch)], schema=schema)
        log_call(ctx, res, prov.name, "finals", cid)
        valid = set(batch)
        for ph in res.data.get("photos", []):
            if ph.get("id") not in valid:
                continue
            rec, total = _critique(ctx, rubric, ph, res.model)
            ctx.db.run(
                "UPDATE contest_scores SET final=?, final_total=? WHERE contest_id=? AND photo_id=?",
                (json.dumps(rec, ensure_ascii=False), total, cid, ph["id"]),
            )
        done += len(batch)
        progress(done, total_steps, f"外部評審決選（{prov.name}）")

    ranked = ctx.db.all(
        "SELECT photo_id, final, final_total FROM contest_scores WHERE contest_id=? AND final_total IS NOT NULL "
        "ORDER BY final_total DESC, photo_id",
        (cid,),
    )
    if not ranked:
        raise CloudError("外部模型沒有回傳入圍照片的評分，請再試一次。")

    # Jury round: the strongest few side by side on one sheet. Ranking needs comparison, not a second full upload.
    picks = int(b["sizes"]["picks"])
    jury = ranked[: max(JURY_SIZE, picks + 2)]
    summary = {"overall": "", "ranking": [], "provider": prov.name, "at": time.time(), "reused": len(reused),
               "triaged": len(tri)}  # fmt: skip
    if len(jury) >= 2 and not cancelled():
        jschema = {
            "type": "object",
            "properties": {
                "ranking": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"no": {"type": "integer"}, "why": {"type": "string"}},
                        "required": ["no", "why"],
                        "additionalProperties": False,
                    },
                },
                "overall": {"type": "string"},
            },
            "required": ["ranking", "overall"],
            "additionalProperties": False,
        }
        lines = []
        for n, r in enumerate(jury, 1):
            f = json.loads(r["final"])
            lines.append(f"{n}.《{f.get('title') or '未命名'}》個別評分 {r['final_total']:.0f}｜優點：{f.get('strengths', '')}｜可能扣分：{f.get('weaknesses', '')}")
        sheet = editing.contact_sheet([Image.open(ctx.preview(r["photo_id"], 1024)) for r in jury],
                                      list(range(1, len(jury) + 1)), cols=4, cell=512)  # fmt: skip
        juser = (
            f"比賽主題：{b['theme'] or '一般攝影比賽'}\n比賽規則：{b['rules'] or '（無）'}\n理想的得獎作品：{rubric['ideal']}\n\n"
            f"下面這張總表是分數最高的 {len(jury)} 張入圍照片，左上角有編號。每一張你都已經個別看過，評語如下：\n"
            + "\n".join(lines)
            + "\n\n請把它們放在一起比較，排出名次：\n"
            "- ranking：由第一名排到最後一名，全部都要列出，no 是總表上的編號，why 用一句話說明它排在這個位置的理由。\n"
            f"- overall：給參賽者的總評與投稿建議（建議投哪 {picks} 張、理由、要注意什麼），200 字以內。提到照片時請用作品名稱。"
        )
        try:
            res = prov.generate(system=TEACHER_SYSTEM, parts=[juser, Img(imaging.jpeg_bytes(sheet, 88))], schema=jschema)
            log_call(ctx, res, prov.name, "jury", cid, photos=len(jury))
            seen_no, order = set(), []
            for it in res.data.get("ranking", []):
                n = it.get("no")
                if isinstance(n, int) and 1 <= n <= len(jury) and n not in seen_no:
                    seen_no.add(n)
                    order.append({"id": jury[n - 1]["photo_id"], "why": str(it.get("why", ""))[:300]})
            summary["ranking"] = order
            summary["overall"] = str(res.data.get("overall", ""))[:800]
        except CloudError as e:
            summary["overall"] = f"（比較排名失敗：{e}。以下名次依個別分數排列。）"
    progress(total_steps, total_steps, "完成")

    order_ids = [it["id"] for it in summary["ranking"]]
    order_ids += [r["photo_id"] for r in ranked if r["photo_id"] not in set(order_ids)]
    ctx.db.many(
        "UPDATE contest_scores SET final_rank=? WHERE contest_id=? AND photo_id=?",
        [(i + 1, cid, pid) for i, pid in enumerate(order_ids)],
    )
    summary["picks"] = order_ids[:picks]
    _save(ctx, cid, summary=summary)
    return summary


# ------------------------------------------------------------------- results


def state(ctx: Ctx, cid: int) -> dict:
    """Everything the contest screen needs in one object."""
    c = get(ctx, cid)
    n = ctx.db.one(
        "SELECT SUM(local IS NOT NULL) l, SUM(teacher IS NOT NULL) t, SUM(final IS NOT NULL) f, SUM(triage IS NOT NULL) g "
        "FROM contest_scores WHERE contest_id=?",
        (cid,),
    )
    pool = candidate_pool(ctx, c)
    scored = n["l"] or 0
    c.update({
        "scope_photos": scope_count(ctx, c["brief"]),
        "library_photos": ctx.db.val("SELECT COUNT(*) FROM photos WHERE error IS NULL") or 0,
        "pool": len(pool),
        "scored_local": scored, "scored_teacher": n["t"] or 0, "scored_final": n["f"] or 0, "scored_triage": n["g"] or 0,
        "triage_next": triage_size(c, scored),
        "usage": usage(ctx, cid),
        "image_tokens": estimate_image_tokens(ctx.settings.cloud.image_px),
    })  # fmt: skip
    return c


def results(ctx: Ctx, cid: int, limit: int = 300) -> list[dict]:
    rows = ctx.db.all(
        "SELECT s.*, p.filename, p.path, p.caption, p.category, p.aesthetic, p.tech, p.event_id, p.width, p.height, "
        "(SELECT e.recipe IS NOT NULL FROM edits e WHERE e.photo_id=p.id) edited "
        "FROM contest_scores s JOIN photos p ON p.id=s.photo_id WHERE s.contest_id=? AND s.local_total IS NOT NULL "
        "ORDER BY (s.final_rank IS NULL), s.final_rank, COALESCE(s.calibrated, s.local_total) DESC, s.photo_id LIMIT ?",
        (cid, limit),
    )
    out = []
    for r in rows:
        d = dict(r)
        for k in ("local", "teacher", "final", "triage"):
            d[k] = json.loads(d[k]) if d[k] else None
        out.append(d)
    return out
