"""The local funnel, stage by stage. Everything here runs on-device."""

from __future__ import annotations

import json
import time

from . import grouping, ingest, local_vlm
from .context import Ctx
from .embedder import embed_images, get_embedder
from .jobs import Job


def embed_missing(ctx: Ctx, job: Job) -> int:
    st = ctx.settings.embed
    job.progress(0, 0, "載入影像模型（第一次會下載約 1.5 GB）…")
    emb = get_embedder(st.model, st.device)
    have = {r[0] for r in ctx.db.all("SELECT photo_id FROM embeddings WHERE model=?", (emb.name,))}
    rows = [r["id"] for r in ctx.db.all("SELECT id FROM photos WHERE error IS NULL ORDER BY id") if r["id"] not in have]
    total, done = len(rows), 0

    def on_batch(items):
        nonlocal done
        ctx.db.put_embeddings(emb.name, items)
        done += len(items)
        job.progress(done, total, "分析影像內容")

    job.progress(0, total, "分析影像內容")
    embed_images(emb, ((pid, ctx.thumb_path(pid)) for pid in rows), st.batch_size, on_batch, job.cancelled)
    ctx.bump()
    return done


def annotate(ctx: Ctx, job: Job, limit: int = 0, redo: bool = False) -> dict:
    """Captions, tags and craft scores from the on-device VLM, best candidates first."""
    vlm = local_vlm.LocalVLM(ctx.settings.local, role="annotator")
    where = "error IS NULL AND is_best=1 AND COALESCE(flag,'') != 'reject'"
    if not redo:
        where += " AND vlm IS NULL"
    rows = ctx.db.all(f"SELECT id FROM photos WHERE {where} ORDER BY (flag='pick') DESC, prefilter DESC, id")
    ids = [r["id"] for r in rows]
    if limit:
        ids = ids[:limit]
    px = ctx.settings.local.image_px
    n, failed, t0 = 0, 0, time.time()
    job.progress(0, len(ids), f"地端模型標註（{vlm.model}）")
    for pid in ids:
        if job.cancelled():
            break
        try:
            a = local_vlm.annotate(vlm, ctx.preview_bytes(pid, px))
        except local_vlm.LocalModelError:
            failed += 1
            if failed >= 5 and n == 0:
                raise
            continue
        except OSError:
            failed += 1
            continue
        ctx.db.run(
            "UPDATE photos SET caption=?, tags=?, vlm=?, vlm_model=?, vlm_score=?, category=COALESCE(?, category) WHERE id=?",
            (a["caption"], json.dumps(a["tags"], ensure_ascii=False), json.dumps(a, ensure_ascii=False), vlm.model,
             a["score"], a["scene"], pid),
        )  # the model looked at the photo, so its scene beats the embedding's guess
        n += 1
        job.progress(n + failed, len(ids), f"地端模型標註（{vlm.model}，每張約 {(time.time() - t0) / n:.1f} 秒）")
    titled = title_events(ctx, vlm) if n and not job.cancelled() else 0
    return {"annotated": n, "failed": failed, "events_titled": titled, "seconds": round(time.time() - t0, 1)}


def title_events(ctx: Ctx, vlm: local_vlm.LocalVLM) -> int:
    """Give auto-named events a human title once enough of their photos are captioned."""
    n = 0
    for ev in ctx.db.all("SELECT id, title, place FROM events WHERE user_title=0 AND sig NOT LIKE 'undated:%'"):
        caps = [r[0] for r in ctx.db.all(
            "SELECT caption FROM photos WHERE event_id=? AND caption IS NOT NULL ORDER BY prefilter DESC LIMIT 12", (ev["id"],))]  # fmt: skip
        if len(caps) < 3:
            continue
        name = local_vlm.title_event(vlm, caps, ev["place"])
        if not name:
            continue
        date = ev["title"].split(" ")[0]
        # user_title=2 marks a model-written title: kept across re-analysis, still replaceable by the user.
        ctx.db.run("UPDATE events SET title=?, user_title=2 WHERE id=?", (f"{date} {name}", ev["id"]))
        n += 1
    return n


def analyze(ctx: Ctx, job: Job, annotate_limit: int = 0) -> dict:
    """Scan every folder, then embed, group and (optionally) annotate."""
    out: dict = {"folders": []}
    folders = ctx.db.all("SELECT id, path FROM folders ORDER BY id")
    stages = 3 + (1 if annotate_limit else 0)
    job.stage = f"1/{stages} 匯入照片"
    for f in folders:
        if job.cancelled():
            return out
        out["folders"].append({"path": f["path"], **ingest.ingest_folder(ctx, f["id"], job.progress, job.cancelled)})
    ctx.bump()
    if job.cancelled():
        return out
    job.stage = f"2/{stages} 影像內容分析"
    out["embedded"] = embed_missing(ctx, job)
    if job.cancelled():
        return out
    job.stage = f"3/{stages} 分組、事件與分類"
    out.update(grouping.rebuild(ctx, job.progress))
    ctx.bump()
    if annotate_limit and not job.cancelled():
        job.stage = f"4/{stages} 地端模型標註"
        try:
            out.update(annotate(ctx, job, annotate_limit))
        except local_vlm.LocalModelError as e:
            out["annotate_error"] = str(e)  # the rest of the analysis is still valid
    return out
