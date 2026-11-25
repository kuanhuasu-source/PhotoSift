"""Stage 3: turn vectors and timestamps into structure.

- near-duplicate / burst groups, with the best frame of each chosen
- events (a trip, an evening out) from gaps in time and jumps in place
- the funnel score that orders everything downstream
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import numpy as np

from . import geocode, taxonomy
from .context import Ctx


class UnionFind:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def find_groups(vecs: np.ndarray, ts: np.ndarray, burst_window: float, burst_sim: float, dup_sim: float) -> np.ndarray:
    """Group labels. Two ways in: nearly identical anywhere, or similar and shot moments apart."""
    n = len(vecs)
    uf = UnionFind(n)
    if n == 0:
        return np.zeros(0, dtype=np.int64)

    # (a) copies and re-saves of the same frame, wherever they live
    step = 2048
    for s in range(0, n, step):
        sims = vecs[s : s + step] @ vecs.T
        ii, jj = np.nonzero(sims >= dup_sim)
        for i, j in zip(ii + s, jj):
            if i < j:
                uf.union(int(i), int(j))

    # (b) bursts: consecutive-in-time frames that look alike
    order = np.argsort(ts, kind="stable")
    for a in range(n):
        i = order[a]
        for b in range(a + 1, min(a + 40, n)):
            j = order[b]
            if ts[j] - ts[i] > burst_window:
                break
            if float(vecs[i] @ vecs[j]) >= burst_sim:
                uf.union(int(i), int(j))

    return np.array([uf.find(i) for i in range(n)], dtype=np.int64)


def split_events(rows: list[dict], gap_hours: float, jump_km: float) -> list[list[dict]]:
    """Rows sorted by time. A new event starts after a long pause or a big move."""
    events: list[list[dict]] = []
    cur: list[dict] = []
    last_gps = None
    for r in rows:
        new = False
        if cur:
            gap_h = (r["taken_ts"] - cur[-1]["taken_ts"]) / 3600.0
            if gap_h > gap_hours:
                new = True
            elif last_gps and r["lat"] is not None and gap_h > 1.0:
                if geocode.haversine_km(last_gps[0], last_gps[1], r["lat"], r["lon"]) > jump_km:
                    new = True
        if new:
            events.append(cur)
            cur, last_gps = [], None
        cur.append(r)
        if r["lat"] is not None:
            last_gps = (r["lat"], r["lon"])
    if cur:
        events.append(cur)
    return events


def _event_title(rows: list[dict], place: str | None) -> str:
    a = datetime.fromtimestamp(rows[0]["taken_ts"], tz=timezone.utc)
    b = datetime.fromtimestamp(rows[-1]["taken_ts"], tz=timezone.utc)
    if a.date() == b.date():
        when = a.strftime("%Y-%m-%d")
    elif (a.year, a.month) == (b.year, b.month):
        when = f"{a.strftime('%Y-%m-%d')}～{b.strftime('%d')}"
    else:
        when = f"{a.strftime('%Y-%m-%d')}～{b.strftime('%m-%d')}"
    return f"{when} {place}" if place else when


def rebuild(ctx: Ctx, progress=lambda done, total, msg: None) -> dict:
    """Recomputes categories, aesthetics, groups, events and funnel scores for the whole library."""
    from .embedder import get_embedder

    db, st = ctx.db, ctx.settings
    emb = get_embedder(st.embed.model, st.embed.device)
    ids, vecs = db.load_embeddings(emb.name)
    if len(ids) == 0:
        return {"photos": 0}
    progress(0, 4, "場景分類與美感初評")
    scorer = taxonomy.Scorer(emb)
    cats = scorer.categories(vecs)
    raw = scorer.aesthetic_raw(vecs)
    aest = taxonomy.to_percentile(raw)

    meta = {
        r["id"]: dict(r)
        for r in db.all("SELECT id, taken_ts, time_source, lat, lon, sharp, tech, flags, flag, width, height, vlm FROM photos")
    }
    ts = np.array([meta[i]["taken_ts"] or 0.0 for i in ids])

    progress(1, 4, "找出連拍與重複")
    a = st.analysis
    labels = find_groups(vecs, ts, a.burst_window_s, a.burst_sim, a.dup_sim)

    # Funnel score: what the eye would probably keep, before any language model looks.
    tech = np.array([meta[i]["tech"] if meta[i]["tech"] is not None else 50.0 for i in ids])
    prefilter = 0.55 * aest + 0.45 * tech

    updates = []
    groups: dict[int, list[int]] = {}
    for k, lab in enumerate(labels):
        groups.setdefault(int(lab), []).append(k)
    best_of: dict[int, int] = {}
    for lab, members in groups.items():
        def keep_rank(k):
            m = meta[int(ids[k])]
            pinned = 1 if m["flag"] == "pick" else (-1 if m["flag"] == "reject" else 0)
            return (pinned, float(prefilter[k]), (m["width"] or 0) * (m["height"] or 0))

        best_of[lab] = max(members, key=keep_rank)

    def category_of(k: int) -> str:
        vlm = meta[int(ids[k])]["vlm"]
        scene = json.loads(vlm).get("scene") if vlm else None
        return scene or cats[k][0][0]  # what the local model saw beats the zero-shot guess

    for k, pid in enumerate(ids):
        m = meta[int(pid)]
        lab = int(labels[k])
        flags = [f for f in json.loads(m["flags"] or "[]") if f != "blurry"]
        if m["sharp"] is not None and m["sharp"] < a.blur_below:
            flags.append("blurry")
        penalty = 25.0 if "blurry" in flags else 0.0
        penalty += 12.0 if ("overexposed" in flags or "underexposed" in flags) else 0.0
        updates.append((
            category_of(best_of[lab]), json.dumps(cats[k]), float(raw[k]), round(float(aest[k]), 1),
            int(ids[labels[k]]), len(groups[lab]), 1 if best_of[lab] == k else 0,
            round(float(max(prefilter[k] - penalty, 0.0)), 1), json.dumps(flags), int(pid),
        ))  # fmt: skip
    db.many(
        "UPDATE photos SET category=?, category_top=?, aesthetic_raw=?, aesthetic=?, group_id=?, group_size=?, "
        "is_best=?, prefilter=?, flags=? WHERE id=?",
        updates,
    )

    progress(2, 4, "切分事件")
    n_events = rebuild_events(ctx)
    progress(4, 4, "完成")
    return {"photos": len(ids), "groups": sum(1 for g in groups.values() if len(g) > 1), "events": n_events}


def rebuild_events(ctx: Ctx) -> int:
    db, a = ctx.db, ctx.settings.analysis
    rows = [dict(r) for r in db.all(
        "SELECT id, taken_ts, time_source, lat, lon, prefilter, folder_id FROM photos "
        "WHERE taken_ts IS NOT NULL ORDER BY taken_ts, id"
    )]  # fmt: skip
    dated = [r for r in rows if r["time_source"] == "exif"]
    undated = [r for r in rows if r["time_source"] != "exif"]
    # Titles a person (1) or the local model (2) wrote are kept across re-analysis.
    renamed = {r["sig"]: (r["title"], r["user_title"]) for r in db.all("SELECT sig, title, user_title FROM events WHERE user_title>0")}

    plan = []  # (title, user_title, rows, place, sig)
    for ev in split_events(dated, a.event_gap_hours, a.event_jump_km):
        gps = [(r["lat"], r["lon"]) for r in ev if r["lat"] is not None]
        place = None
        if gps:
            place = geocode.place_name(float(np.median([g[0] for g in gps])), float(np.median([g[1] for g in gps])))
        # Signature: first photo and day. Survives adding photos to the end of an event.
        day = datetime.fromtimestamp(ev[0]["taken_ts"], tz=timezone.utc).strftime("%Y%m%d")
        sig = hashlib.sha1(f"{ev[0]['id']}:{day}".encode()).hexdigest()[:16]
        title, kind = renamed.get(sig, (_event_title(ev, place), 0))
        plan.append((title, kind, ev, place, sig))

    # Photos with no capture time (scans, downloads, edited exports) are kept together per folder.
    by_folder: dict[int, list[dict]] = {}
    for r in undated:
        by_folder.setdefault(r["folder_id"], []).append(r)
    folder_names = {r["id"]: r["path"] for r in db.all("SELECT id, path FROM folders")}
    for fid, ev in by_folder.items():
        sig = f"undated:{fid}"
        name = folder_names.get(fid, "").rstrip("/").split("/")[-1]
        title, kind = renamed.get(sig, (f"未知拍攝時間・{name}", 0))
        plan.append((title, kind, ev, None, sig))

    with db.tx() as c:
        c.execute("DELETE FROM events")
        c.execute("UPDATE photos SET event_id=NULL")
        for title, user_title, ev, place, sig in plan:
            cover = max(ev, key=lambda r: r["prefilter"] if r["prefilter"] is not None else -1)
            cur = c.execute(
                "INSERT INTO events(title, user_title, start_ts, end_ts, place, cover_id, count, sig) VALUES (?,?,?,?,?,?,?,?)",
                (title, user_title, ev[0]["taken_ts"], ev[-1]["taken_ts"], place, cover["id"], len(ev), sig),
            )
            c.executemany("UPDATE photos SET event_id=? WHERE id=?", [(cur.lastrowid, r["id"]) for r in ev])
    return len(plan)
