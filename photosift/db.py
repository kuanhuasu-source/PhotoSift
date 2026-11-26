"""SQLite storage. One connection per thread, WAL so the UI can read while a job writes."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    added_at REAL NOT NULL,
    last_scan REAL
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    path TEXT NOT NULL UNIQUE,
    raw_path TEXT,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    width INTEGER, height INTEGER,
    taken_at TEXT,              -- naive local ISO time
    taken_ts REAL,              -- seconds, for ordering and gaps
    time_source TEXT,           -- exif | mtime
    lat REAL, lon REAL,
    camera TEXT, lens TEXT,
    focal REAL, aperture REAL, shutter TEXT, iso INTEGER,
    -- technical quality (0-100)
    sharp REAL, exposure REAL, contrast REAL, colorful REAL, tech REAL,
    flags TEXT NOT NULL DEFAULT '[]',
    -- embedding-derived
    aesthetic_raw REAL, aesthetic REAL,
    category TEXT, category_top TEXT,
    group_id INTEGER, group_size INTEGER NOT NULL DEFAULT 1, is_best INTEGER NOT NULL DEFAULT 1,
    event_id INTEGER,
    prefilter REAL,             -- combined local score used to order the funnel
    -- local VLM annotation
    caption TEXT, tags TEXT, vlm TEXT, vlm_model TEXT, vlm_score REAL,
    -- user decisions (never touch the files)
    rating INTEGER NOT NULL DEFAULT 0,
    flag TEXT,                  -- pick | reject
    error TEXT
);
CREATE INDEX IF NOT EXISTS photos_ts ON photos(taken_ts);
CREATE INDEX IF NOT EXISTS photos_event ON photos(event_id);
CREATE INDEX IF NOT EXISTS photos_group ON photos(group_id);
CREATE INDEX IF NOT EXISTS photos_category ON photos(category);
CREATE INDEX IF NOT EXISTS photos_folder ON photos(folder_id);

CREATE TABLE IF NOT EXISTS embeddings (
    photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    vec BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    user_title INTEGER NOT NULL DEFAULT 0,
    start_ts REAL, end_ts REAL,
    place TEXT,
    cover_id INTEGER,
    count INTEGER NOT NULL DEFAULT 0,
    sig TEXT                    -- stable signature so renames survive re-analysis
);

CREATE TABLE IF NOT EXISTS contests (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    brief TEXT NOT NULL,        -- json: theme, rules, values, scope, sizes
    rubric TEXT,                -- json from the coach
    calibration TEXT,           -- json: weights, agreement, notes
    summary TEXT                -- json: final jury verdict
);

CREATE TABLE IF NOT EXISTS contest_scores (
    contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    local TEXT, local_total REAL,           -- on-device judge
    teacher TEXT, teacher_total REAL,       -- cloud score on the calibration sample
    calibrated REAL,                        -- local score mapped onto the teacher's scale
    final TEXT, final_total REAL, final_rank INTEGER,
    PRIMARY KEY (contest_id, photo_id)
);

CREATE TABLE IF NOT EXISTS submissions (
    contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    title TEXT, description TEXT,
    questions TEXT,             -- json: [{q, options, a}] asked by the local model, answered by the user
    use_edit INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',   -- draft | sent | passed | failed
    updated_at REAL,
    PRIMARY KEY (contest_id, photo_id)
);

CREATE TABLE IF NOT EXISTS edits (
    photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
    recipe TEXT,                -- json: the current edit, NULL when the photo is untouched
    suggested TEXT,             -- json: the latest suggestion from each source, {"local": ..., "cloud": ...}
    updated_at REAL
);

-- Mentor mode: advice on how to take the next picture better.
CREATE TABLE IF NOT EXISTS coach (
    photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
    advice TEXT,                -- json from the on-device model: what to do differently in front of this scene
    pair TEXT,                  -- json: why another frame of the same scene came out better
    model TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,        -- event | library
    scope_id INTEGER,           -- the event, or NULL for the whole library
    stats TEXT NOT NULL,        -- json: what was measured, with no model involved
    review TEXT,                -- json: the mentor's reading of it
    photo_ids TEXT,             -- json: the photos shown on the contact sheet, in sheet order
    provider TEXT, model TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS exercises (
    id INTEGER PRIMARY KEY,
    review_id INTEGER REFERENCES reviews(id) ON DELETE SET NULL,
    title TEXT NOT NULL, how TEXT, why TEXT,
    metric TEXT NOT NULL,       -- which measurement shows progress, or 'manual'
    baseline REAL, baseline_n INTEGER,
    since_ts REAL NOT NULL,     -- photos taken after this moment count towards it (same clock as photos.taken_ts)
    status TEXT NOT NULL DEFAULT 'open',    -- open | done | dropped
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    place TEXT, lat REAL, lon REAL,
    day TEXT NOT NULL,          -- YYYY-MM-DD at the place
    data TEXT NOT NULL          -- json: light timetable, starting settings, shot list, reminders
);

-- A solo exhibition: a fixed number of pictures that belong together, in hanging order.
CREATE TABLE IF NOT EXISTS exhibitions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    spec TEXT NOT NULL,         -- json: how many works, the frame, the author
    themes TEXT,                -- json: series ideas proposed from the library
    theme TEXT,                 -- json: the one chosen, with its ranked candidates
    curation TEXT,              -- json: the curator's reading: title, statement, chapters, weak links, gaps
    profile TEXT,               -- json: interview answers, biography and statement for the poster
    packages TEXT               -- json: log of packages built
);

CREATE TABLE IF NOT EXISTS exhibition_works (
    exhibition_id INTEGER NOT NULL REFERENCES exhibitions(id) ON DELETE CASCADE,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'work',      -- work | reserve
    chapter INTEGER NOT NULL DEFAULT 0,     -- index into the curation's chapters
    position INTEGER NOT NULL DEFAULT 0,    -- hanging order among the works
    title TEXT, description TEXT,
    questions TEXT,             -- json: [{q, options, a}] asked by the local model, answered by the user
    why TEXT,                   -- the curator's reason for choosing it
    use_edit INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (exhibition_id, photo_id)
);

CREATE TABLE IF NOT EXISTS cloud_calls (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    provider TEXT NOT NULL, model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    contest_id INTEGER,
    images INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0
);
"""


MIGRATIONS = {
    "photos": {
        "tz_offset": "INTEGER",                   # minutes east of UTC, when the camera recorded it
        "sun_elev": "REAL",                       # degrees above the horizon at capture
        "light": "TEXT",                          # night | blue | golden | day
        "meta_v": "INTEGER NOT NULL DEFAULT 0",   # which version of the EXIF reader filled this row
        "taste": "REAL",                          # 0-100: how much it resembles what the user picks
        # how the picture was taken, for shooting advice
        "shutter_s": "REAL",                      # exposure time in seconds
        "focal35": "REAL",                        # 35 mm equivalent focal length, when the camera recorded it
        "exp_program": "TEXT",                    # manual | aperture | shutter | program | auto
        "exp_bias": "REAL",                       # exposure compensation in EV
        "metering": "TEXT", "flash": "INTEGER", "wb": "TEXT",
        "heading": "REAL",                        # compass direction the camera pointed, degrees from north
        "tilt": "REAL",                           # measured lean of the horizon in degrees; NULL until measured
    },
    "contest_scores": {"triage": "TEXT"},         # json: what the external model said on the contact sheet
    "contests": {"submission": "TEXT"},           # json: packaging settings and a log of packages built
    "cloud_calls": {"photos": "INTEGER NOT NULL DEFAULT 0"},  # photos shown, counting each tile of a contact sheet
}


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Adds columns introduced after a library was first created. Existing data is kept."""
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    @contextmanager
    def tx(self):
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def val(self, sql: str, params=()):
        r = self.conn.execute(sql, params).fetchone()
        return r[0] if r else None

    def run(self, sql: str, params=()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def many(self, sql: str, rows) -> None:
        with self.tx() as c:
            c.executemany(sql, rows)

    # ------------------------------------------------------------ embeddings

    def put_embeddings(self, model: str, items: list[tuple[int, np.ndarray]]) -> None:
        self.many(
            "INSERT OR REPLACE INTO embeddings(photo_id, model, vec) VALUES (?,?,?)",
            [(pid, model, np.asarray(v, dtype=np.float16).tobytes()) for pid, v in items],
        )

    def load_embeddings(self, model: str, ids: list[int] | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Returns (ids, matrix) for every stored vector of `model`, L2-normalised."""
        rows = self.all("SELECT photo_id, vec FROM embeddings WHERE model=? ORDER BY photo_id", (model,))
        if ids is not None:
            want = set(ids)
            rows = [r for r in rows if r["photo_id"] in want]
        if not rows:
            return np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float32)
        out_ids = np.array([r["photo_id"] for r in rows], dtype=np.int64)
        mat = np.stack([np.frombuffer(r["vec"], dtype=np.float16) for r in rows]).astype(np.float32)
        mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
        return out_ids, mat


JSON_COLS = {"flags", "tags", "vlm", "category_top"}


def photo_dict(row: sqlite3.Row, full: bool = False) -> dict:
    d = dict(row)
    for k in JSON_COLS & d.keys():
        if d[k]:
            try:
                d[k] = json.loads(d[k])
            except Exception:
                d[k] = None
    if not full:
        d.pop("vlm", None)
    return d
