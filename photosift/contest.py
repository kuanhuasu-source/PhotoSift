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
import time
from typing import Callable

import numpy as np

from . import local_vlm, taxonomy
from .cloud import CloudError, CloudResult, Img, estimate_image_tokens, get_provider
from .context import Ctx

Progress = Callable[[int, int, str], None]
Cancelled = Callable[[], bool]

DEFAULT_SIZES = {"pool": 150, "calibration": 16, "finals": 24, "picks": 5}
TEACHER_BATCH = 8
FINAL_BATCH = 6
JURY_SIZE = 8

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
           sizes: dict | None = None, exclude_flawed: bool = True, max_per_event: int = 0) -> int:  # fmt: skip
    brief = {
        "theme": theme.strip(),
        "rules": rules.strip(),
        "notes": notes.strip(),
        "scope": scope or {},
        "sizes": {**DEFAULT_SIZES, **(sizes or {})},
        "exclude_flawed": exclude_flawed,
        "max_per_event": max_per_event,
    }
    cur = ctx.db.run(
        "INSERT INTO contests(name, created_at, brief) VALUES (?,?,?)",
        (name.strip() or "未命名比賽", time.time(), json.dumps(brief, ensure_ascii=False)),
    )
    return cur.lastrowid


def get(ctx: Ctx, cid: int) -> dict:
    r = ctx.db.one("SELECT * FROM contests WHERE id=?", (cid,))
    if r is None:
        raise KeyError(cid)
    d = dict(r)
    for k in ("brief", "rubric", "calibration", "summary"):
        d[k] = json.loads(d[k]) if d[k] else None
    return d


def _save(ctx: Ctx, cid: int, **cols) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in cols.values()]
    ctx.db.run(f"UPDATE contests SET {sets} WHERE id=?", (*vals, cid))


def scope_sql(scope: dict) -> tuple[str, list]:
    where, params = ["p.error IS NULL"], []
    for key, col in (("folders", "p.folder_id"), ("events", "p.event_id"), ("categories", "p.category")):
        vals = scope.get(key) or []
        if vals:
            where.append(f"{col} IN ({','.join('?' * len(vals))})")
            params += list(vals)
    if scope.get("min_rating"):
        where.append("p.rating >= ?")
        params.append(int(scope["min_rating"]))
    return " AND ".join(where), params


def scope_count(ctx: Ctx, scope: dict) -> int:
    w, p = scope_sql(scope)
    return ctx.db.val(f"SELECT COUNT(*) FROM photos p WHERE {w}", p) or 0


def candidate_pool(ctx: Ctx, c: dict) -> list[int]:
    """The funnel's narrow end before any language model: best frame of each burst, ordered by local score."""
    b = c["brief"]
    w, p = scope_sql(b["scope"])
    w += " AND p.is_best=1 AND COALESCE(p.flag,'') != 'reject' AND p.prefilter IS NOT NULL"
    rows = ctx.db.all(f"SELECT p.id, p.flag, p.flags, p.prefilter, p.vlm_score FROM photos p WHERE {w}", p)
    out = []
    for r in rows:
        flags = json.loads(r["flags"] or "[]")
        if b.get("exclude_flawed", True) and r["flag"] != "pick" and flags:
            continue
        # A photo the user picked always competes. An earlier VLM annotation refines the ordering.
        score = r["prefilter"] if r["vlm_score"] is None else 0.5 * r["prefilter"] + 0.5 * r["vlm_score"]
        out.append((1 if r["flag"] == "pick" else 0, score, r["id"]))
    out.sort(reverse=True)
    return [pid for _, _, pid in out[: int(b["sizes"]["pool"])]]


def log_call(ctx: Ctx, res: CloudResult, provider: str, purpose: str, cid: int | None) -> None:
    ctx.db.run(
        "INSERT INTO cloud_calls(ts, provider, model, purpose, contest_id, images, input_tokens, output_tokens, seconds) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (time.time(), provider, res.model, purpose, cid, res.images, res.input_tokens, res.output_tokens, res.seconds),
    )


def usage(ctx: Ctx, cid: int) -> dict:
    c = get(ctx, cid)
    rows = ctx.db.all(
        "SELECT purpose, COUNT(*) n, SUM(images) images, SUM(input_tokens) i, SUM(output_tokens) o "
        "FROM cloud_calls WHERE contest_id=? GROUP BY purpose",
        (cid,),
    )
    by = {r["purpose"]: {"calls": r["n"], "images": r["images"], "input": r["i"], "output": r["o"]} for r in rows}
    total = sum(v["input"] + v["output"] for v in by.values())
    n_scope = scope_count(ctx, c["brief"]["scope"])
    # What it would have cost to show every photo in scope to the external model once.
    baseline = n_scope * (estimate_image_tokens(ctx.settings.cloud.image_px) + 150)
    return {
        "by_purpose": by,
        "total_tokens": total,
        "images_sent": sum(v["images"] for v in by.values()),
        "scope_photos": n_scope,
        "baseline_tokens": baseline,
        "saved_pct": round((1 - total / baseline) * 100, 1) if baseline and total else None,
    }


# --------------------------------------------------------------- step 1: coach

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
    },
    "required": ["criteria", "judge_brief", "pitfalls", "ideal"],
    "additionalProperties": False,
}

COACH_SYSTEM = (
    "你是資深的攝影比賽評審，也是攝影老師。你要為一場攝影比賽設計評分標準。"
    "這份標準不是給人看的：它會交給一個在個人電腦上執行的小型視覺模型，由它逐張替數百張照片打分數做初選。"
    "小模型的理解力有限，所以每一項標準都要具體、看得見、容易判斷，避免抽象的形容。全部用繁體中文。"
)


def _library_summary(ctx: Ctx, scope: dict) -> str:
    w, p = scope_sql(scope)
    cats = ctx.db.all(f"SELECT p.category c, COUNT(*) n FROM photos p WHERE {w} GROUP BY p.category ORDER BY n DESC", p)
    lines = ["相片庫概況（依內容分類的張數）：" + "、".join(f"{taxonomy.LABELS.get(r['c'], r['c'] or '未分類')} {r['n']}" for r in cats[:10])]
    caps = ctx.db.all(
        f"SELECT p.caption FROM photos p WHERE {w} AND p.caption IS NOT NULL ORDER BY p.prefilter DESC LIMIT 15", p
    )
    if caps:
        lines.append("其中幾張照片的內容：\n" + "\n".join(f"- {r['caption']}" for r in caps))
    return "\n".join(lines)


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
        "source": source,
    }


def coach(ctx: Ctx, cid: int, use_cloud: bool = True) -> dict:
    """Ask the external model for a rubric. Falls back to the built-in one when told to stay offline."""
    c = get(ctx, cid)
    b = c["brief"]
    if not use_cloud:
        rubric = dict(DEFAULT_RUBRIC)
    else:
        user = (
            f"比賽名稱：{c['name']}\n比賽主題：{b['theme'] or '（未指定，一般攝影比賽）'}\n"
            f"比賽規則：{b['rules'] or '（無）'}\n主辦單位或評審重視的地方：{b['notes'] or '（未說明）'}\n\n"
            f"{_library_summary(ctx, b['scope'])}\n\n"
            "請設計 4～6 項評分標準：\n"
            "- weight 是比重，全部加起來 100；意境、情感、故事性這類決定作品能不能打動評審的面向，比重不要低於技術面。\n"
            "- description 用一句話說明這項在看什麼。\n"
            "- high 描述 9～10 分的照片長什麼樣子，low 描述 1～3 分的樣子，都要是畫面上看得出來的特徵。\n"
            "- judge_brief：給小模型的評審須知，200 字以內，直接告訴它要怎麼看、常見的誤判要怎麼避免、分數要怎麼拉開差距。\n"
            "- pitfalls：這個主題常見的扣分點。\n"
            "- ideal：一句話描述理想的得獎作品。"
        )
        prov = get_provider(ctx.settings)
        res = prov.generate(system=COACH_SYSTEM, parts=[user], schema=RUBRIC_SCHEMA, max_tokens=8000)
        log_call(ctx, res, prov.name, "coach", cid)
        rubric = normalise_rubric(res.data, f"{prov.name}:{res.model}")
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


def local_prompt(c: dict) -> tuple[str, str]:
    rubric, b = c["rubric"], c["brief"]
    system = rubric["judge_brief"] + "\n只輸出 JSON，用繁體中文。"
    notes = (c.get("calibration") or {}).get("notes")
    if notes:
        system += f"\n\n資深評審看過你之前的評分後給的提醒，請照著修正：\n{notes}"
    user = (
        f"比賽主題：{b['theme'] or '一般攝影比賽'}\n\n評分標準（每項 1～10 分）：\n{_criteria_text(rubric)}\n\n"
        "請為這張照片的每一項打分數，並在 reason 用一句話說出它最主要的優點或缺點。"
    )
    return system, user


def score_local(ctx: Ctx, cid: int, progress: Progress = lambda *a: None, cancelled: Cancelled = lambda: False,
                rescore: bool = False) -> dict:  # fmt: skip
    c = get(ctx, cid)
    if not c["rubric"]:
        raise ValueError("請先產生評分標準。")
    rubric = c["rubric"]
    pool = candidate_pool(ctx, c)
    done_ids = set()
    if not rescore:
        done_ids = {r[0] for r in ctx.db.all(
            "SELECT photo_id FROM contest_scores WHERE contest_id=? AND local IS NOT NULL", (cid,))}  # fmt: skip
    todo = [pid for pid in pool if pid not in done_ids]
    vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge")
    system, user = local_prompt(c)
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
        rec = {"scores": scores, "reason": str(d.get("reason", "")).strip()[:200], "model": vlm.model}
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


def calibrate(ctx: Ctx, cid: int, progress: Progress = lambda *a: None, cancelled: Cancelled = lambda: False) -> dict:
    c = get(ctx, cid)
    rubric = c["rubric"]
    sample = calibration_sample(ctx, cid, int(c["brief"]["sizes"]["calibration"]))
    if len(sample) < 4:
        raise ValueError("地端評分的照片太少，無法校準。請先完成地端評審。")
    prov = get_provider(ctx.settings)
    schema = _teacher_schema(rubric, {"comment": {"type": "string"}})
    intro = (
        f"比賽主題：{c['brief']['theme'] or '一般攝影比賽'}\n\n評分標準（每項 1～10 分）：\n{_criteria_text(rubric)}\n\n"
        "以下每張照片前面都有編號。請為每張照片的每一項評分，並在 comment 用一句話寫出評語。"
        "回傳的 id 必須是照片編號。"
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
            pid = ph.get("id")
            if pid not in valid:
                continue
            scores = {cr["key"]: local_vlm.clamp_score(ph.get(cr["key"])) for cr in rubric["criteria"]}
            rec = {"scores": scores, "comment": str(ph.get("comment", "")).strip()[:300], "model": res.model}
            ctx.db.run(
                "UPDATE contest_scores SET teacher=?, teacher_total=? WHERE contest_id=? AND photo_id=?",
                (json.dumps(rec, ensure_ascii=False), weighted_total(rubric, scores), cid, pid),
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


def finalists(ctx: Ctx, cid: int) -> list[int]:
    c = get(ctx, cid)
    b = c["brief"]
    rows = ctx.db.all(
        "SELECT s.photo_id, COALESCE(s.calibrated, s.local_total) score, p.event_id, p.flag FROM contest_scores s "
        "JOIN photos p ON p.id=s.photo_id WHERE s.contest_id=? AND s.local_total IS NOT NULL "
        "ORDER BY (p.flag='pick') DESC, score DESC, s.photo_id",
        (cid,),
    )
    cap = int(b.get("max_per_event") or 0)
    per_event: dict = {}
    out = []
    for r in rows:
        if cap and r["flag"] != "pick":
            if per_event.get(r["event_id"], 0) >= cap:
                continue
            per_event[r["event_id"]] = per_event.get(r["event_id"], 0) + 1
        out.append(r["photo_id"])
        if len(out) >= int(b["sizes"]["finals"]):
            break
    return out


def finals(ctx: Ctx, cid: int, progress: Progress = lambda *a: None, cancelled: Cancelled = lambda: False) -> dict:
    c = get(ctx, cid)
    rubric, b = c["rubric"], c["brief"]
    ids = finalists(ctx, cid)
    if not ids:
        raise ValueError("還沒有入圍照片。請先完成地端評審。")
    prov = get_provider(ctx.settings)
    extra = {k: {"type": "string"} for k in ("strengths", "weaknesses", "title", "edit")}
    schema = _teacher_schema(rubric, extra)
    intro = (
        f"比賽主題：{b['theme'] or '一般攝影比賽'}\n比賽規則：{b['rules'] or '（無）'}\n\n"
        f"評分標準（每項 1～10 分）：\n{_criteria_text(rubric)}\n\n"
        "以下是通過初選的入圍照片，每張前面都有編號。請逐張評審：\n"
        "- 每一項標準的分數\n- strengths：最打動人的地方\n- weaknesses：最可能被評審扣分的地方\n"
        "- title：適合投稿用的作品名稱（8 字以內）\n- edit：投稿前值得做的一項調整（裁切、調色、轉正等），不需要就寫「不需調整」\n"
        "回傳的 id 必須是照片編號。"
    )
    ctx.db.run("UPDATE contest_scores SET final=NULL, final_total=NULL, final_rank=NULL WHERE contest_id=?", (cid,))
    done = 0
    total_steps = len(ids) + 1
    progress(0, total_steps, f"外部評審決選（{prov.name}）")
    for i in range(0, len(ids), FINAL_BATCH):
        if cancelled():
            return {"cancelled": True}
        batch = ids[i : i + FINAL_BATCH]
        res = prov.generate(system=TEACHER_SYSTEM, parts=[intro, *_photo_parts(ctx, batch)], schema=schema)
        log_call(ctx, res, prov.name, "finals", cid)
        valid = set(batch)
        for ph in res.data.get("photos", []):
            pid = ph.get("id")
            if pid not in valid:
                continue
            scores = {cr["key"]: local_vlm.clamp_score(ph.get(cr["key"])) for cr in rubric["criteria"]}
            rec = {"scores": scores, "model": res.model, **{k: str(ph.get(k, "")).strip()[:300] for k in extra}}
            ctx.db.run(
                "UPDATE contest_scores SET final=?, final_total=? WHERE contest_id=? AND photo_id=?",
                (json.dumps(rec, ensure_ascii=False), weighted_total(rubric, scores), cid, pid),
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

    # Jury round: the strongest few are seen side by side, because ranking needs comparison.
    picks = int(b["sizes"]["picks"])
    jury = [r["photo_id"] for r in ranked[: max(JURY_SIZE, picks + 2)]]
    summary = {"overall": "", "ranking": [], "provider": prov.name, "at": time.time()}
    if len(jury) >= 2 and not cancelled():
        jschema = {
            "type": "object",
            "properties": {
                "ranking": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}, "why": {"type": "string"}},
                        "required": ["id", "why"],
                        "additionalProperties": False,
                    },
                },
                "overall": {"type": "string"},
            },
            "required": ["ranking", "overall"],
            "additionalProperties": False,
        }
        juser = (
            f"比賽主題：{b['theme'] or '一般攝影比賽'}\n比賽規則：{b['rules'] or '（無）'}\n理想的得獎作品：{rubric['ideal']}\n\n"
            f"以下是分數最高的 {len(jury)} 張入圍照片。請把它們放在一起比較，排出名次：\n"
            "- ranking：由第一名排到最後一名，全部都要列出，why 用一句話說明它排在這個位置的理由。\n"
            f"- overall：給參賽者的總評與投稿建議（建議投哪 {picks} 張、理由、要注意什麼），200 字以內。"
        )
        try:
            res = prov.generate(system=TEACHER_SYSTEM, parts=[juser, *_photo_parts(ctx, jury)], schema=jschema)
            log_call(ctx, res, prov.name, "jury", cid)
            seen, order = set(), []
            for it in res.data.get("ranking", []):
                if it.get("id") in jury and it["id"] not in seen:
                    seen.add(it["id"])
                    order.append({"id": it["id"], "why": str(it.get("why", ""))[:300]})
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
        "SELECT SUM(local IS NOT NULL) l, SUM(teacher IS NOT NULL) t, SUM(final IS NOT NULL) f "
        "FROM contest_scores WHERE contest_id=?",
        (cid,),
    )
    pool = candidate_pool(ctx, c)
    c.update({
        "scope_photos": scope_count(ctx, c["brief"]["scope"]),
        "pool": len(pool),
        "scored_local": n["l"] or 0, "scored_teacher": n["t"] or 0, "scored_final": n["f"] or 0,
        "usage": usage(ctx, cid),
        "image_tokens": estimate_image_tokens(ctx.settings.cloud.image_px),
    })  # fmt: skip
    return c


def results(ctx: Ctx, cid: int, limit: int = 300) -> list[dict]:
    rows = ctx.db.all(
        "SELECT s.*, p.filename, p.path, p.caption, p.category, p.aesthetic, p.tech, p.event_id, p.width, p.height "
        "FROM contest_scores s JOIN photos p ON p.id=s.photo_id WHERE s.contest_id=? AND s.local_total IS NOT NULL "
        "ORDER BY (s.final_rank IS NULL), s.final_rank, COALESCE(s.calibrated, s.local_total) DESC, s.photo_id LIMIT ?",
        (cid, limit),
    )
    out = []
    for r in rows:
        d = dict(r)
        for k in ("local", "teacher", "final"):
            d[k] = json.loads(d[k]) if d[k] else None
        out.append(d)
    return out
