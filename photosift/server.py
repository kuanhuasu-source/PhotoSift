"""HTTP API and static UI. Bound to localhost only: this is a single-user desktop app."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import cloud, config, contest, export, grouping, local_vlm, pipeline, taxonomy
from .cloud import CloudError
from .context import Ctx
from .db import photo_dict
from .jobs import Busy, JobRunner

WEB = Path(__file__).parent / "web"

LIST_COLS = (
    "p.id, p.filename, p.width, p.height, p.taken_at, p.category, p.aesthetic, p.tech, p.sharp, p.prefilter, "
    "p.vlm_score, p.group_id, p.group_size, p.is_best, p.flags, p.rating, p.flag, p.caption, p.event_id, p.error"
)
SORTS = {
    "time": "p.taken_ts ASC, p.id",
    "time_desc": "p.taken_ts DESC, p.id DESC",
    # Photos the local model has scored come first: its score and the rough pre-score are different scales.
    "score": "(p.vlm_score IS NULL), COALESCE(p.vlm_score, p.prefilter) DESC, p.id",
    "prefilter": "p.prefilter DESC, p.id",
    "aesthetic": "p.aesthetic DESC, p.id",
    "tech": "p.tech DESC, p.id",
    "rating": "p.rating DESC, p.prefilter DESC, p.id",
}


class FolderIn(BaseModel):
    path: str


class PhotoPatch(BaseModel):
    rating: int | None = None
    flag: str | None = None  # "pick" | "reject" | "" to clear


class KeyIn(BaseModel):
    provider: str
    key: str


class AnalyzeIn(BaseModel):
    annotate_limit: int = 0


class AnnotateIn(BaseModel):
    limit: int = 200
    redo: bool = False


class ContestIn(BaseModel):
    name: str
    theme: str = ""
    rules: str = ""
    notes: str = ""
    scope: dict = {}
    sizes: dict = {}
    exclude_flawed: bool = True
    max_per_event: int = 0


class CoachIn(BaseModel):
    use_cloud: bool = True


class RubricIn(BaseModel):
    rubric: dict


class ExportIn(BaseModel):
    dest: str
    photo_ids: list[int] = []
    include_raw: bool = True
    top: int | None = None


class RenameIn(BaseModel):
    title: str


def create_app(ctx: Ctx | None = None) -> FastAPI:
    ctx = ctx or Ctx()
    jobs = JobRunner()
    app = FastAPI(title="PhotoSift", docs_url=None, redoc_url=None)
    app.state.ctx, app.state.jobs, app.state.window = ctx, jobs, None
    local_cache = {"t": 0.0, "v": None}

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        # A web page open in the user's browser must not be able to drive this local API.
        origin = request.headers.get("origin")
        if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
            host = request.headers.get("host", "")
            if origin.split("://", 1)[-1] != host:
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(CloudError)
    async def cloud_error(_, e: CloudError):
        return JSONResponse({"detail": str(e)}, status_code=502)

    @app.exception_handler(local_vlm.LocalModelError)
    async def local_error(_, e):
        return JSONResponse({"detail": str(e)}, status_code=502)

    @app.exception_handler(Busy)
    async def busy(_, e: Busy):
        return JSONResponse({"detail": str(e)}, status_code=409)

    def local_status(force: bool = False) -> dict:
        if force or time.time() - local_cache["t"] > 8:
            st = local_vlm.LocalVLM(ctx.settings.local).status()
            judge = ctx.settings.local.judge_model
            st["judge"] = judge or st.get("model", "")
            st["judge_ok"] = (not judge) or any(m["name"] == judge for m in st["models"])
            ram = local_vlm.system_ram_gb()
            st["ram_gb"] = round(ram)
            st["recommended"] = local_vlm.recommend_models(ram)
            local_cache.update(t=time.time(), v=st)
        return local_cache["v"]

    # ------------------------------------------------------------- overview

    @app.get("/api/status")
    def status():
        db = ctx.db
        c = db.one(
            "SELECT COUNT(*) n, SUM(error IS NOT NULL) bad, SUM(vlm IS NOT NULL) annotated, "
            "SUM(is_best=0) dup, SUM(flags != '[]') flawed, SUM(flag='pick') picks, SUM(flag='reject') rejects, "
            "SUM(prefilter IS NOT NULL) analysed FROM photos"
        )
        prov = ctx.settings.cloud.provider
        return {
            "photos": c["n"] or 0, "unreadable": c["bad"] or 0, "analysed": c["analysed"] or 0,
            "annotated": c["annotated"] or 0, "duplicates": c["dup"] or 0, "flawed": c["flawed"] or 0,
            "picks": c["picks"] or 0, "rejects": c["rejects"] or 0,
            "embedded": db.val("SELECT COUNT(*) FROM embeddings") or 0,
            "events": db.val("SELECT COUNT(*) FROM events") or 0,
            "folders": db.val("SELECT COUNT(*) FROM folders") or 0,
            "job": jobs.snapshot(),
            "local": local_status(),
            "cloud": {"provider": prov, "label": cloud.LABELS[prov], "model": ctx.settings.cloud.models.get(prov, ""),
                      "has_key": _has_key(prov)},
            "native_dialog": app.state.window is not None,
            "home": str(ctx.home),
        }  # fmt: skip

    def _has_key(provider: str) -> bool:
        if config.get_api_key(provider):
            return True
        # Claude can also sign in through an `ant auth login` profile, which the SDK finds itself.
        return provider == "claude" and (Path.home() / ".config" / "anthropic").exists()

    @app.get("/api/job")
    def job():
        return {"job": jobs.snapshot()}

    @app.post("/api/job/cancel")
    def job_cancel():
        return {"cancelled": jobs.cancel()}

    # -------------------------------------------------------------- folders

    @app.get("/api/folders")
    def folders():
        rows = ctx.db.all(
            "SELECT f.id, f.path, f.last_scan, COUNT(p.id) photos FROM folders f LEFT JOIN photos p ON p.folder_id=f.id "
            "GROUP BY f.id ORDER BY f.id"
        )
        return [dict(r) | {"exists": Path(r["path"]).is_dir()} for r in rows]

    @app.post("/api/folders")
    def add_folder(body: FolderIn):
        p = Path(body.path).expanduser()
        if not p.is_dir():
            raise HTTPException(400, f"找不到資料夾：{body.path}")
        p = p.resolve()
        for r in ctx.db.all("SELECT path FROM folders"):
            q = Path(r["path"])
            if p == q or q in p.parents:
                raise HTTPException(400, f"這個資料夾已經包含在「{q}」裡了。")
            if p in q.parents:
                raise HTTPException(400, f"已加入的「{q}」在這個資料夾裡面。請先移除它，再加入上層資料夾。")
        cur = ctx.db.run("INSERT INTO folders(path, added_at) VALUES (?,?)", (str(p), time.time()))
        return {"id": cur.lastrowid, "path": str(p)}

    @app.delete("/api/folders/{fid}")
    def remove_folder(fid: int):
        """Forgets the folder and its analysis. The photos on disk are not touched."""
        ids = [r[0] for r in ctx.db.all("SELECT id FROM photos WHERE folder_id=?", (fid,))]
        ctx.db.run("DELETE FROM folders WHERE id=?", (fid,))
        for pid in ids:
            ctx.drop_cached(pid)
        ctx.bump()
        if ctx.db.val("SELECT COUNT(*) FROM photos"):
            grouping.rebuild_events(ctx)
        else:
            ctx.db.run("DELETE FROM events")
        return {"removed": len(ids)}

    @app.post("/api/pick-folder")
    def pick_folder():
        w = app.state.window
        if w is None:
            return {"supported": False}
        import webview

        # pywebview 6 renamed the constant; older versions only have FOLDER_DIALOG.
        kind = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
        res = w.create_file_dialog(kind)
        return {"supported": True, "path": res[0] if res else None}

    @app.get("/api/browse")
    def browse(path: str = ""):
        """Sub-folders of a directory, for the folder chooser when there is no native dialog."""
        p = Path(path).expanduser() if path else Path.home()
        if not p.is_dir():
            raise HTTPException(400, "找不到這個資料夾。")
        p = p.resolve()
        try:
            dirs = sorted((d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")), key=str.lower)
        except PermissionError:
            dirs = []
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs}

    # ----------------------------------------------------------------- jobs

    @app.post("/api/analyze")
    def analyze(body: AnalyzeIn):
        if not ctx.db.val("SELECT COUNT(*) FROM folders"):
            raise HTTPException(400, "請先加入至少一個相片資料夾。")
        j = jobs.submit("analyze", "分析相片庫", lambda job: pipeline.analyze(ctx, job, body.annotate_limit))
        return j.to_dict()

    @app.post("/api/annotate")
    def annotate(body: AnnotateIn):
        def run(job):
            job.stage = "地端模型標註"
            return pipeline.annotate(ctx, job, body.limit, body.redo)

        return jobs.submit("annotate", "地端模型標註", run).to_dict()

    @app.post("/api/regroup")
    def regroup():
        def run(job):
            job.stage = "重新分組"
            out = grouping.rebuild(ctx, job.progress)
            ctx.bump()
            return out

        return jobs.submit("regroup", "重新分組與分類", run).to_dict()

    # --------------------------------------------------------------- photos

    def _filters(q: dict) -> tuple[str, list]:
        where, params = ["1=1"], []
        for key, col in (("folder", "p.folder_id"), ("event", "p.event_id"), ("category", "p.category"), ("group", "p.group_id")):
            if q.get(key) not in (None, ""):
                where.append(f"{col}=?")
                params.append(q[key])
        if q.get("best") == "1":
            where.append("p.is_best=1")
        if q.get("flag") in ("pick", "reject"):
            where.append("p.flag=?")
            params.append(q["flag"])
        elif q.get("flag") == "none":
            where.append("p.flag IS NULL")
        if q.get("hide_rejects") == "1":
            where.append("COALESCE(p.flag,'') != 'reject'")
        if q.get("quality") == "ok":
            where.append("p.flags='[]' AND p.error IS NULL")
        elif q.get("quality") == "flawed":
            where.append("(p.flags!='[]' OR p.error IS NOT NULL)")
        elif q.get("quality") in ("blurry", "overexposed", "underexposed", "low_contrast"):
            where.append("p.flags LIKE ?")
            params.append(f'%"{q["quality"]}"%')
        if q.get("dups") == "1":
            where.append("p.group_size>1")
        if q.get("min_rating"):
            where.append("p.rating>=?")
            params.append(int(q["min_rating"]))
        if q.get("annotated") == "1":
            where.append("p.vlm IS NOT NULL")
        return " AND ".join(where), params

    def _search(q: str, limit: int = 240) -> list[tuple[int, float]]:
        """Meaning first (text-image similarity), with literal caption/tag/file-name hits pulled to the front."""
        scores: dict[int, float] = {}
        if ctx.db.val("SELECT COUNT(*) FROM embeddings"):
            emb, ids, vecs = ctx.vectors()
            if len(ids):
                sims = vecs @ emb.texts([q])[0]
                prob = emb.match_prob(sims)
                if prob is not None:
                    # Calibrated model: drop everything when nothing really matches ("a dog" in a
                    # library with no dogs), else keep what is within reach of the best match.
                    keep = prob >= max(1.5e-4, float(prob.max()) / 300.0)
                else:
                    keep = sims >= max(float(np.quantile(sims, 0.80)), float(sims.max()) - 0.06)
                for i in np.argsort(-sims)[:limit]:
                    if keep[i]:
                        scores[int(ids[i])] = float(sims[i])
        like = f"%{q}%"
        for r in ctx.db.all("SELECT id FROM photos WHERE caption LIKE ? OR tags LIKE ? OR filename LIKE ? LIMIT 400", (like, like, like)):
            scores[r["id"]] = scores.get(r["id"], 0.0) + 1.0
        return sorted(scores.items(), key=lambda kv: -kv[1])[:limit]

    @app.get("/api/photos")
    def photos(request: Request, offset: int = 0, limit: int = 120, sort: str = "time_desc", q: str = ""):
        params_in = dict(request.query_params)
        where, params = _filters(params_in)
        if q.strip():
            hits = _search(q.strip())
            if not hits:
                return {"total": 0, "items": [], "search": True}
            order = {pid: i for i, (pid, _) in enumerate(hits)}
            rows = ctx.db.all(
                f"SELECT {LIST_COLS} FROM photos p WHERE {where} AND p.id IN ({','.join('?' * len(order))})",
                (*params, *order),
            )
            rows.sort(key=lambda r: order[r["id"]])
            return {"total": len(rows), "items": [photo_dict(r) for r in rows[offset : offset + limit]], "search": True}
        total = ctx.db.val(f"SELECT COUNT(*) FROM photos p WHERE {where}", params)
        rows = ctx.db.all(
            f"SELECT {LIST_COLS} FROM photos p WHERE {where} ORDER BY {SORTS.get(sort, SORTS['time_desc'])} LIMIT ? OFFSET ?",
            (*params, min(limit, 500), offset),
        )
        return {"total": total, "items": [photo_dict(r) for r in rows]}

    @app.get("/api/photos/{pid}")
    def photo(pid: int):
        r = ctx.db.one("SELECT * FROM photos WHERE id=?", (pid,))
        if r is None:
            raise HTTPException(404, "找不到這張照片")
        d = photo_dict(r, full=True)
        d["category_label"] = taxonomy.LABELS.get(d["category"], d["category"])
        if d["event_id"]:
            ev = ctx.db.one("SELECT title FROM events WHERE id=?", (d["event_id"],))
            d["event_title"] = ev["title"] if ev else None
        d["group"] = []
        if d["group_size"] and d["group_size"] > 1:
            d["group"] = [dict(x) for x in ctx.db.all(
                "SELECT id, is_best, sharp, prefilter, flags FROM photos WHERE group_id=? ORDER BY is_best DESC, prefilter DESC",
                (d["group_id"],))]  # fmt: skip
        d["contests"] = []
        for s in ctx.db.all(
            "SELECT s.*, c.name FROM contest_scores s JOIN contests c ON c.id=s.contest_id WHERE s.photo_id=? ORDER BY c.id DESC",
            (pid,),
        ):
            x = dict(s)
            for k in ("local", "teacher", "final"):
                x[k] = json.loads(x[k]) if x[k] else None
            d["contests"].append(x)
        return d

    @app.patch("/api/photos/{pid}")
    def patch_photo(pid: int, body: PhotoPatch):
        if body.rating is not None:
            ctx.db.run("UPDATE photos SET rating=? WHERE id=?", (max(0, min(5, body.rating)), pid))
        if body.flag is not None:
            if body.flag not in ("pick", "reject", ""):
                raise HTTPException(400, "flag 只能是 pick、reject 或空字串")
            ctx.db.run("UPDATE photos SET flag=? WHERE id=?", (body.flag or None, pid))
        return dict(ctx.db.one("SELECT id, rating, flag FROM photos WHERE id=?", (pid,)))

    @app.post("/api/groups/{gid}/best/{pid}")
    def set_best(gid: int, pid: int):
        """The user overrides which frame of a burst is kept."""
        if not ctx.db.one("SELECT 1 FROM photos WHERE id=? AND group_id=?", (pid, gid)):
            raise HTTPException(404, "這張照片不在這個群組裡")
        with ctx.db.tx() as c:
            c.execute("UPDATE photos SET is_best=0 WHERE group_id=?", (gid,))
            c.execute("UPDATE photos SET is_best=1 WHERE id=?", (pid,))
        return {"ok": True}

    @app.get("/api/thumb/{pid}")
    def thumb(pid: int):
        p = ctx.thumb_path(pid)
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/preview/{pid}")
    def preview(pid: int, px: int = 1600):
        px = 2400 if px > 2000 else 1600 if px > 1024 else 1024
        try:
            p = ctx.preview(pid, px)
        except KeyError:
            raise HTTPException(404)
        except Exception:
            p = ctx.thumb_path(pid)  # original moved or unreadable: the thumbnail is better than nothing
            if not p.exists():
                raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/reveal/{pid}")
    def reveal(pid: int):
        r = ctx.db.one("SELECT path FROM photos WHERE id=?", (pid,))
        if r is None or not Path(r["path"]).exists():
            raise HTTPException(404, "原始檔已不在原本的位置")
        path = r["path"]
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                subprocess.Popen(["xdg-open", str(Path(path).parent)])
        except OSError:
            raise HTTPException(500, f"無法開啟檔案管理員。檔案位置：{path}")
        return {"ok": True}

    # ---------------------------------------------- events, categories, groups

    @app.get("/api/events")
    def events():
        return [dict(r) for r in ctx.db.all("SELECT * FROM events ORDER BY (start_ts IS NULL), start_ts DESC")]

    @app.patch("/api/events/{eid}")
    def rename_event(eid: int, body: RenameIn):
        t = body.title.strip()
        if not t:
            raise HTTPException(400, "名稱不能是空的")
        ctx.db.run("UPDATE events SET title=?, user_title=1 WHERE id=?", (t[:80], eid))
        return {"ok": True}

    @app.get("/api/categories")
    def categories():
        rows = ctx.db.all(
            "SELECT category, COUNT(*) n, (SELECT id FROM photos q WHERE q.category=p.category ORDER BY q.is_best DESC, COALESCE(q.vlm_score, q.prefilter) DESC LIMIT 1) cover "
            "FROM photos p WHERE category IS NOT NULL GROUP BY category ORDER BY n DESC"
        )
        return [{"key": r["category"], "label": taxonomy.LABELS.get(r["category"], r["category"]), "count": r["n"], "cover": r["cover"]} for r in rows]

    @app.get("/api/groups")
    def groups(offset: int = 0, limit: int = 40):
        total = ctx.db.val("SELECT COUNT(DISTINCT group_id) FROM photos WHERE group_size>1")
        gids = [r[0] for r in ctx.db.all(
            "SELECT group_id FROM photos WHERE group_size>1 GROUP BY group_id ORDER BY MIN(taken_ts) DESC LIMIT ? OFFSET ?",
            (limit, offset))]  # fmt: skip
        out = []
        for gid in gids:
            members = ctx.db.all(
                f"SELECT {LIST_COLS} FROM photos p WHERE p.group_id=? ORDER BY p.is_best DESC, p.prefilter DESC", (gid,))  # fmt: skip
            out.append({"id": gid, "photos": [photo_dict(m) for m in members]})
        return {"total": total, "groups": out}

    @app.get("/api/facets")
    def facets():
        """Counts for the filter controls."""
        flaw = {}
        for k in ("blurry", "overexposed", "underexposed", "low_contrast"):
            flaw[k] = ctx.db.val("SELECT COUNT(*) FROM photos WHERE flags LIKE ?", (f'%"{k}"%',))
        return {
            "folders": folders(),
            "events": [{"id": e["id"], "title": e["title"], "count": e["count"]} for e in events()],
            "categories": categories(),
            "flaws": flaw,
        }

    # -------------------------------------------------------------- contests

    @app.get("/api/contests")
    def contests():
        return [
            {"id": r["id"], "name": r["name"], "created_at": r["created_at"], "has_rubric": r["rubric"] is not None,
             "done": r["summary"] is not None}
            for r in ctx.db.all("SELECT * FROM contests ORDER BY id DESC")
        ]  # fmt: skip

    @app.post("/api/contests")
    def new_contest(body: ContestIn):
        cid = contest.create(ctx, body.name, body.theme, body.rules, body.notes, body.scope, body.sizes,
                             body.exclude_flawed, body.max_per_event)  # fmt: skip
        return contest.state(ctx, cid)

    def _contest(cid: int) -> dict:
        try:
            return contest.state(ctx, cid)
        except KeyError:
            raise HTTPException(404, "找不到這個比賽")

    @app.get("/api/contests/{cid}")
    def get_contest(cid: int):
        return _contest(cid)

    @app.put("/api/contests/{cid}")
    def update_contest(cid: int, body: ContestIn):
        c = _contest(cid)
        brief = {
            "theme": body.theme.strip(), "rules": body.rules.strip(), "notes": body.notes.strip(), "scope": body.scope,
            "sizes": {**contest.DEFAULT_SIZES, **body.sizes}, "exclude_flawed": body.exclude_flawed,
            "max_per_event": body.max_per_event,
        }  # fmt: skip
        ctx.db.run("UPDATE contests SET name=?, brief=? WHERE id=?", (body.name.strip() or c["name"], json.dumps(brief, ensure_ascii=False), cid))
        return _contest(cid)

    @app.delete("/api/contests/{cid}")
    def delete_contest(cid: int):
        ctx.db.run("DELETE FROM contests WHERE id=?", (cid,))
        return {"ok": True}

    @app.post("/api/contests/{cid}/coach")
    def coach(cid: int, body: CoachIn):
        _contest(cid)
        contest.coach(ctx, cid, body.use_cloud)
        return _contest(cid)

    @app.put("/api/contests/{cid}/rubric")
    def edit_rubric(cid: int, body: RubricIn):
        """The user's own edits to the rubric. Scores made with the old one are discarded."""
        _contest(cid)
        try:
            rubric = contest.normalise_rubric(body.rubric, "edited")
        except CloudError:
            raise HTTPException(400, "評分標準至少要有兩項。")
        rubric["id"] = f"edit{int(time.time())}"
        with ctx.db.tx() as c:
            c.execute("DELETE FROM contest_scores WHERE contest_id=?", (cid,))
            c.execute("UPDATE contests SET rubric=?, calibration=NULL, summary=NULL WHERE id=?",
                      (json.dumps(rubric, ensure_ascii=False), cid))  # fmt: skip
        return _contest(cid)

    @app.post("/api/contests/{cid}/local")
    def contest_local(cid: int, rescore: bool = False):
        _contest(cid)

        def run(job):
            job.stage = "地端評審"
            return contest.score_local(ctx, cid, job.progress, job.cancelled, rescore)

        return jobs.submit("contest_local", "地端評審", run).to_dict()

    @app.get("/api/contests/{cid}/outgoing/{step}")
    def outgoing(cid: int, step: str):
        """Exactly which photos the next external step would upload, so the user can look before agreeing."""
        c = _contest(cid)
        if step == "calibrate":
            ids = contest.calibration_sample(ctx, cid, int(c["brief"]["sizes"]["calibration"]))
            extra = 1500
        elif step == "finals":
            ids = contest.finalists(ctx, cid)
            extra = contest.JURY_SIZE * c["image_tokens"] + 3000
        else:
            raise HTTPException(404)
        est = len(ids) * (c["image_tokens"] + 250) + extra
        prov = ctx.settings.cloud.provider
        return {"photo_ids": ids, "est_tokens": est, "image_px": ctx.settings.cloud.image_px,
                "provider": cloud.LABELS[prov], "model": ctx.settings.cloud.models.get(prov, "")}  # fmt: skip

    @app.post("/api/contests/{cid}/calibrate")
    def contest_calibrate(cid: int):
        _contest(cid)

        def run(job):
            job.stage = "外部評審校準"
            return contest.calibrate(ctx, cid, job.progress, job.cancelled)

        return jobs.submit("contest_calibrate", "外部評審校準", run).to_dict()

    @app.post("/api/contests/{cid}/finals")
    def contest_finals(cid: int):
        _contest(cid)

        def run(job):
            job.stage = "外部評審決選"
            return contest.finals(ctx, cid, job.progress, job.cancelled)

        return jobs.submit("contest_finals", "外部評審決選", run).to_dict()

    @app.get("/api/contests/{cid}/results")
    def contest_results(cid: int):
        _contest(cid)
        return contest.results(ctx, cid)

    @app.post("/api/contests/{cid}/export")
    def contest_export(cid: int, body: ExportIn):
        _contest(cid)
        return export.export_contest(ctx, cid, body.dest, body.top, body.include_raw)

    @app.post("/api/export")
    def export_photos(body: ExportIn):
        if not body.photo_ids:
            raise HTTPException(400, "沒有選取照片")
        return export.copy_photos(ctx, body.photo_ids, body.dest, body.include_raw)

    # -------------------------------------------------------------- settings

    @app.get("/api/settings")
    def get_settings():
        return {
            "settings": ctx.settings.model_dump(),
            "keys": {p: config.key_source(p) for p in cloud.PROVIDERS},
            "providers": cloud.LABELS,
        }

    @app.put("/api/settings")
    def put_settings(body: dict):
        try:
            new = config.Settings.model_validate(body)
        except Exception as e:
            raise HTTPException(400, f"設定格式不正確：{e}")
        regroup_needed = new.analysis != ctx.settings.analysis
        ctx.settings = new
        ctx.save_settings()
        local_cache["t"] = 0.0
        return {"ok": True, "regroup_needed": regroup_needed}

    @app.post("/api/keys")
    def set_key(body: KeyIn):
        if body.provider not in cloud.PROVIDERS:
            raise HTTPException(400, "不支援的外部模型")
        where = config.set_api_key(body.provider, body.key)
        return {"stored": where if body.key.strip() else None, "source": config.key_source(body.provider)}

    @app.get("/api/models/local")
    def models_local():
        return local_status(force=True)

    @app.get("/api/models/cloud/{provider}")
    def models_cloud(provider: str):
        if provider not in cloud.PROVIDERS:
            raise HTTPException(404)
        return {"models": cloud.get_provider(ctx.settings, provider).list_models()}

    @app.get("/api/usage")
    def usage():
        rows = ctx.db.all(
            "SELECT provider, model, COUNT(*) calls, SUM(images) images, SUM(input_tokens) i, SUM(output_tokens) o "
            "FROM cloud_calls GROUP BY provider, model ORDER BY MAX(ts) DESC"
        )
        s = ctx.settings.cloud
        out = []
        for r in rows:
            d = dict(r)
            d["cost"] = round((d["i"] * s.price_in + d["o"] * s.price_out) / 1e6, 4) if (s.price_in or s.price_out) else None
            out.append(d)
        return {"models": out, "total_tokens": sum(d["i"] + d["o"] for d in out)}

    # ------------------------------------------------------------------- UI

    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return app
