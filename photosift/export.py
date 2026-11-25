"""Copy chosen originals somewhere else. Copies only: source files are never moved or changed."""

from __future__ import annotations

import html
import re
import shutil
from pathlib import Path

from . import contest
from .context import Ctx


def _free_name(dest: Path, name: str) -> Path:
    p = dest / name
    stem, suf, i = p.stem, p.suffix, 2
    while p.exists():
        p = dest / f"{stem} ({i}){suf}"
        i += 1
    return p


def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_")[:40]


def copy_photos(ctx: Ctx, photo_ids: list[int], dest: str, include_raw: bool = True, prefix: dict | None = None) -> dict:
    d = Path(dest).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    copied, missing = 0, []
    for pid in photo_ids:
        r = ctx.db.one("SELECT path, raw_path, filename FROM photos WHERE id=?", (pid,))
        if r is None:
            continue
        src = Path(r["path"])
        if not src.exists():
            missing.append(r["filename"])
            continue
        pre = (prefix or {}).get(pid, "")
        shutil.copy2(src, _free_name(d, pre + src.name))
        copied += 1
        if include_raw and r["raw_path"] and Path(r["raw_path"]).exists():
            raw = Path(r["raw_path"])
            shutil.copy2(raw, _free_name(d, pre + raw.name))
    return {"copied": copied, "missing": missing, "dest": str(d)}


def export_contest(ctx: Ctx, cid: int, dest: str, top: int | None = None, include_raw: bool = True) -> dict:
    """Finalists in rank order plus a readable report of the jury's notes."""
    c = contest.get(ctx, cid)
    rows = [r for r in contest.results(ctx, cid) if r["final_rank"] is not None]
    if not rows:  # no external jury ran: fall back to the local ranking
        rows = contest.results(ctx, cid)[: c["brief"]["sizes"]["finals"]]
    if top:
        rows = rows[:top]
    prefix = {}
    for i, r in enumerate(rows):
        title = _safe((r["final"] or {}).get("title", ""))
        prefix[r["photo_id"]] = f"{i + 1:02d}_{title}_" if title else f"{i + 1:02d}_"
    out = copy_photos(ctx, [r["photo_id"] for r in rows], dest, include_raw, prefix)

    rubric = c["rubric"] or {"criteria": []}
    names = {cr["key"]: cr["name"] for cr in rubric["criteria"]}
    e = html.escape
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>{e(c['name'])} 評審報告</title>",
        "<style>body{font:15px/1.7 -apple-system,'PingFang TC','Noto Sans TC',sans-serif;max-width:860px;margin:40px auto;"
        "padding:0 20px;color:#222}h1{font-size:24px}h2{font-size:17px;margin:28px 0 6px}"
        ".m{color:#666;font-size:13px}.box{background:#f5f5f3;border-radius:8px;padding:12px 16px;margin:12px 0}"
        "table{border-collapse:collapse;font-size:13px}td{padding:2px 14px 2px 0}</style>",
        f"<h1>{e(c['name'])}</h1><p class='m'>主題：{e(c['brief']['theme'] or '—')}</p>",
    ]
    summary = c.get("summary") or {}
    if summary.get("overall"):
        parts.append(f"<div class='box'><b>總評與投稿建議</b><br>{e(summary['overall'])}</div>")
    why = {it["id"]: it["why"] for it in summary.get("ranking", [])}
    for i, r in enumerate(rows):
        f = r["final"] or {}
        score = r["final_total"] if r["final_total"] is not None else (r["calibrated"] or r["local_total"])
        parts.append(f"<h2>{i + 1}. {e(f.get('title') or r['filename'])}　<span class='m'>{score:.0f} 分・{e(r['filename'])}</span></h2>")
        if why.get(r["photo_id"]):
            parts.append(f"<p>{e(why[r['photo_id']])}</p>")
        src = f.get("scores") or (r["local"] or {}).get("scores") or {}
        if src:
            parts.append("<table><tr>" + "".join(f"<td>{e(names.get(k, k))} <b>{v}</b></td>" for k, v in src.items()) + "</tr></table>")
        for label, key in (("優點", "strengths"), ("可能扣分", "weaknesses"), ("投稿前調整", "edit")):
            if f.get(key):
                parts.append(f"<p><b>{label}：</b>{e(f[key])}</p>")
        if not f and (r["local"] or {}).get("reason"):
            parts.append(f"<p><b>地端評審：</b>{e(r['local']['reason'])}</p>")
    (Path(out["dest"]) / "評審報告.html").write_text("\n".join(parts), "utf-8")
    out["report"] = str(Path(out["dest"]) / "評審報告.html")
    return out
