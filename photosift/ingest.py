"""Stage 1: find photos, read EXIF, measure technical quality, make thumbnails.

Costs no model time at all. Unchanged files are skipped on a re-scan.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from . import imaging, quality
from .context import THUMB_PX, Ctx

SKIP_DIR_SUFFIXES = (".photoslibrary", ".lrdata", ".lrcat-data", ".app")
SKIP_DIR_NAMES = {"@eaDir", "node_modules", "__MACOSX", "$RECYCLE.BIN"}


def find_files(root: Path) -> list[tuple[Path, Path | None]]:
    """Returns (image path, paired RAW path). A RAW next to a same-named JPEG rides along with it."""
    by_stem: dict[tuple[str, str], dict[str, Path]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".") and d not in SKIP_DIR_NAMES and not d.lower().endswith(SKIP_DIR_SUFFIXES)
        ]
        for name in filenames:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in imaging.ALL_EXTS:
                continue
            key = (dirpath, os.path.splitext(name)[0].lower())
            kind = "raw" if ext in imaging.RAW_EXTS else "img"
            by_stem.setdefault(key, {}).setdefault(kind, Path(dirpath) / name)
    out = []
    for pair in by_stem.values():
        if "img" in pair:
            out.append((pair["img"], pair.get("raw")))
        else:
            out.append((pair["raw"], None))
    out.sort(key=lambda t: str(t[0]))
    return out


def process_file(path: Path) -> dict:
    """Runs in a worker thread: everything that needs the pixels."""
    st = path.stat()
    rec: dict = {"path": str(path), "filename": path.name, "size": st.st_size, "mtime": st.st_mtime}
    try:
        im, full = imaging.load_image(path, max_side=quality.ANALYSIS_SIDE)
        rec["width"], rec["height"] = full
        rec.update(quality.analyze(im))
        rec["thumb"] = imaging.jpeg_bytes(imaging.resized(im, THUMB_PX), 80)
    except Exception as e:  # unreadable or truncated file: keep a row so it shows up as a problem
        rec["error"] = f"{type(e).__name__}: {e}"[:300]
    try:
        rec.update(imaging.read_meta(path))
    except Exception:
        pass
    return rec


COLS = [
    "path", "raw_path", "filename", "size", "mtime", "width", "height", "taken_at", "taken_ts", "time_source",
    "lat", "lon", "camera", "lens", "focal", "aperture", "shutter", "iso",
    "sharp", "exposure", "contrast", "colorful", "tech", "flags", "error", "tz_offset", "meta_v",
]  # fmt: skip
META_V = 2  # bump when read_meta learns something new; older rows are re-read without decoding pixels

# Everything derived from pixels is cleared when a file changes on disk.
RESET = (
    "aesthetic_raw=NULL, aesthetic=NULL, category=NULL, category_top=NULL, group_id=NULL, group_size=1, "
    "is_best=1, event_id=NULL, prefilter=NULL, caption=NULL, tags=NULL, vlm=NULL, vlm_model=NULL, vlm_score=NULL"
)


def ingest_folder(
    ctx: Ctx,
    folder_id: int,
    progress: Callable[[int, int, str], None] = lambda done, total, msg: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict:
    db = ctx.db
    folder = db.one("SELECT * FROM folders WHERE id=?", (folder_id,))
    root = Path(folder["path"])
    if not root.is_dir():
        raise FileNotFoundError(f"找不到資料夾：{root}")

    progress(0, 0, f"掃描 {root.name} …")
    files = find_files(root)
    known = {r["path"]: r for r in db.all("SELECT id, path, size, mtime FROM photos WHERE folder_id=?", (folder_id,))}

    todo = []
    for path, raw in files:
        k = known.get(str(path))
        if k is None:
            todo.append((path, raw))
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_size != k["size"] or abs(st.st_mtime - k["mtime"]) > 1:
            todo.append((path, raw))

    present = {str(p) for p, _ in files}
    gone = [r for p, r in known.items() if p not in present]
    if gone:
        with db.tx() as c:
            c.executemany("DELETE FROM photos WHERE id=?", [(r["id"],) for r in gone])
        for r in gone:
            ctx.drop_cached(r["id"])

    total, done, added, failed = len(todo), 0, 0, 0
    progress(0, total, f"匯入 {root.name}")
    workers = max(2, min(8, (os.cpu_count() or 4)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        it = iter(todo)
        pending = {}

        def feed():
            # Keep a bounded number in flight so cancelling is quick and memory stays flat.
            while len(pending) < workers * 3:
                nxt = next(it, None)
                if nxt is None:
                    return
                pending[pool.submit(process_file, nxt[0])] = nxt

        feed()
        while pending:
            for fut in as_completed(list(pending)):
                path, raw = pending.pop(fut)
                rec = fut.result()
                rec["raw_path"] = str(raw) if raw else None
                rec["meta_v"] = META_V
                rec["flags"] = json.dumps(rec.get("flags", []))
                thumb = rec.pop("thumb", None)
                vals = [rec.get(c) for c in COLS]
                old = known.get(str(path))
                with db.tx() as c:
                    if old:
                        sets = ", ".join(f"{col}=?" for col in COLS)
                        c.execute(f"UPDATE photos SET {sets}, {RESET} WHERE id=?", (*vals, old["id"]))
                        c.execute("DELETE FROM embeddings WHERE photo_id=?", (old["id"],))
                        pid = old["id"]
                        ctx.drop_cached(pid)
                    else:
                        cur = c.execute(
                            f"INSERT INTO photos(folder_id, {', '.join(COLS)}) VALUES (?{', ?' * len(COLS)})",
                            (folder_id, *vals),
                        )
                        pid = cur.lastrowid
                if thumb:
                    tp = ctx.thumb_path(pid)
                    tp.parent.mkdir(parents=True, exist_ok=True)
                    tp.write_bytes(thumb)
                    added += 1
                else:
                    failed += 1
                done += 1
                if done % 5 == 0 or done == total:
                    progress(done, total, f"匯入 {root.name}")
                break  # re-check cancellation and top up the queue after each result
            if cancelled():
                for f in pending:
                    f.cancel()
                break
            feed()

    db.run("UPDATE folders SET last_scan=? WHERE id=?", (time.time(), folder_id))
    return {"found": len(files), "processed": done, "added": added, "failed": failed, "removed": len(gone)}


def refresh_meta(ctx: Ctx, progress: Callable[[int, int, str], None] = lambda done, total, msg: None) -> int:
    """Re-reads EXIF for rows written before the reader learned a field. Fast: no image is decoded."""
    rows = ctx.db.all("SELECT id, path FROM photos WHERE meta_v < ? AND error IS NULL", (META_V,))
    done = 0
    for i, r in enumerate(rows):
        try:
            m = imaging.read_meta(Path(r["path"]))
            ctx.db.run("UPDATE photos SET tz_offset=?, meta_v=? WHERE id=?", (m.get("tz_offset"), META_V, r["id"]))
            done += 1
        except OSError:
            ctx.db.run("UPDATE photos SET meta_v=? WHERE id=?", (META_V, r["id"]))  # file gone: do not retry forever
        if i % 50 == 0:
            progress(i, len(rows), "更新拍攝資訊")
    return done
