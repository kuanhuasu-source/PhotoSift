from __future__ import annotations

import json
import time
from datetime import datetime

import numpy as np
from PIL import Image

from photosift import grouping, imaging, ingest, pipeline, quality
from photosift.jobs import Job

from .conftest import make_photo


def test_find_files_skips_hidden_and_non_images(library):
    names = {p.name for p, _ in ingest.find_files(library)}
    assert "A0.jpg" in names and "old.jpg" in names
    assert "skip.jpg" not in names and "notes.txt" not in names
    assert len(names) == 13


def test_raw_next_to_jpeg_is_one_photo(tmp_path):
    make_photo(tmp_path / "IMG_1.jpg", seed=1)
    (tmp_path / "IMG_1.CR3").write_bytes(b"raw")
    (tmp_path / "IMG_2.NEF").write_bytes(b"raw")
    found = ingest.find_files(tmp_path)
    assert [(p.name, r.name if r else None) for p, r in found] == [("IMG_1.jpg", "IMG_1.CR3"), ("IMG_2.NEF", None)]


def test_exif_is_read(tmp_path):
    make_photo(tmp_path / "x.jpg", seed=3, when=datetime(2026, 5, 4, 13, 14, 15), gps=(25.5, 121.5))
    m = imaging.read_meta(tmp_path / "x.jpg")
    assert m["taken_at"] == "2026-05-04T13:14:15" and m["time_source"] == "exif"
    assert abs(m["lat"] - 25.5) < 0.01 and abs(m["lon"] - 121.5) < 0.01
    assert m["camera"] == "TestCam T-1" and m["aperture"] == 2.8 and m["shutter"] == "1/125s" and m["iso"] == 400


def test_missing_exif_falls_back_to_file_time(tmp_path):
    make_photo(tmp_path / "x.jpg", seed=3)
    assert imaging.read_meta(tmp_path / "x.jpg")["time_source"] == "mtime"


def test_upload_copy_has_no_metadata(tmp_path):
    make_photo(tmp_path / "x.jpg", seed=3, when=datetime(2026, 5, 4), gps=(25.5, 121.5))
    im, _ = imaging.load_image(tmp_path / "x.jpg", 400)
    import io

    out = Image.open(io.BytesIO(imaging.jpeg_bytes(im)))
    assert max(out.size) == 400
    assert len(out.getexif()) == 0  # no time, camera or GPS leaves the machine


def test_quality_flags(tmp_path):
    make_photo(tmp_path / "sharp.jpg", seed=1)
    make_photo(tmp_path / "blur.jpg", seed=1, blur=8)
    make_photo(tmp_path / "dark.jpg", seed=1, brightness=0.08)
    make_photo(tmp_path / "bright.jpg", seed=1, brightness=6)
    q = {n: quality.analyze(imaging.load_image(tmp_path / f"{n}.jpg", 2048)[0]) for n in ("sharp", "blur", "dark", "bright")}
    assert q["sharp"]["sharp"] > 60 > 25 > q["blur"]["sharp"]
    assert q["sharp"]["flags"] == []
    assert "underexposed" in q["dark"]["flags"]
    assert "overexposed" in q["bright"]["flags"]
    assert q["sharp"]["tech"] > q["blur"]["tech"]


def test_analyze_end_to_end(analysed):
    ctx, result = analysed
    db = ctx.db
    assert db.val("SELECT COUNT(*) FROM photos") == 13
    assert db.val("SELECT COUNT(*) FROM embeddings") == 13
    assert result["folders"][0]["added"] == 13

    # the burst is one group, and the blurred frame is not the one kept
    burst = db.all("SELECT filename, group_size, is_best, flags FROM photos WHERE filename LIKE 'B%' ORDER BY filename")
    assert [r["group_size"] for r in burst] == [3, 3, 3]
    assert sum(r["is_best"] for r in burst) == 1
    assert burst[2]["is_best"] == 0 and "blurry" in json.loads(burst[2]["flags"])
    assert db.val("SELECT COUNT(*) FROM photos WHERE group_size>1") == 3

    # two dated events plus one bucket for the undated scan
    events = db.all("SELECT title, count, place FROM events ORDER BY start_ts")
    assert sorted(e["count"] for e in events) == [1, 4, 8]
    assert any("未知拍攝時間" in e["title"] for e in events)
    assert db.val("SELECT COUNT(*) FROM photos WHERE event_id IS NULL") == 0
    assert db.val("SELECT COUNT(*) FROM photos WHERE prefilter IS NULL OR category IS NULL") == 0


def test_rescan_only_processes_changes(analysed, library):
    ctx, _ = analysed
    again = ingest.ingest_folder(ctx, 1)
    assert again["processed"] == 0 and again["removed"] == 0

    (library / "dinner" / "C0.jpg").unlink()
    make_photo(library / "dinner" / "new.jpg", seed=999, when=datetime(2026, 9, 10, 19, 0))
    again = ingest.ingest_folder(ctx, 1)
    assert again["processed"] == 1 and again["removed"] == 1
    assert ctx.db.val("SELECT COUNT(*) FROM photos") == 13
    assert not ctx.db.one("SELECT 1 FROM photos WHERE filename='C0.jpg'")


def test_unreadable_file_is_recorded_not_fatal(ctx, tmp_path):
    root = tmp_path / "p"
    make_photo(root / "ok.jpg", seed=1)
    (root / "broken.jpg").write_bytes(b"\xff\xd8 not really a jpeg")
    ctx.db.run("INSERT INTO folders(path, added_at) VALUES (?,?)", (str(root), time.time()))
    out = ingest.ingest_folder(ctx, 1)
    assert out["added"] == 1 and out["failed"] == 1
    assert ctx.db.val("SELECT error FROM photos WHERE filename='broken.jpg'")


def test_event_split_by_gap_and_distance():
    def row(i, hours, lat=None, lon=None):
        return {"id": i, "taken_ts": hours * 3600.0, "lat": lat, "lon": lon}

    rows = [row(1, 0, 25, 121), row(2, 1, 25, 121), row(3, 3, 35, 135), row(4, 30, 35, 135)]
    events = grouping.split_events(rows, gap_hours=6, jump_km=80)
    assert [[r["id"] for r in e] for e in events] == [[1, 2], [3], [4]]


def test_groups_need_similarity_and_time():
    v = np.eye(4, dtype=np.float32)
    vecs = np.stack([v[0], v[0] * 0.99 + v[1] * 0.14, v[2], v[0]])
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    ts = np.array([0.0, 2.0, 4.0, 99999.0])
    labels = grouping.find_groups(vecs, ts, burst_window=60, burst_sim=0.9, dup_sim=0.999)
    assert labels[0] == labels[1]  # alike and seconds apart: a burst
    assert labels[2] != labels[0]  # seconds apart but different
    assert labels[3] == labels[0]  # identical copy, however far apart in time


def test_annotate_uses_local_model_and_names_events(analysed, fakes):
    ctx, _ = analysed
    vlm, _ = fakes
    assert pipeline.annotate(ctx, Job(2, "annotate", "t"), limit=5)["annotated"] == 5
    out = pipeline.annotate(ctx, Job(3, "annotate", "t"))  # the rest; already-annotated photos are skipped
    assert out["annotated"] == 6
    rows = ctx.db.all("SELECT caption, tags, vlm_score, vlm_model, is_best FROM photos WHERE vlm IS NOT NULL")
    assert len(rows) == 11 and all(r["is_best"] for r in rows)  # duplicates are never sent to the model
    assert all(r["caption"] and r["vlm_model"] == "fake-vlm" and 10 <= r["vlm_score"] <= 100 for r in rows)
    assert out["events_titled"] >= 1
    assert ctx.db.val("SELECT COUNT(*) FROM events WHERE user_title=2 AND title LIKE '%測試活動'") >= 1


def test_user_event_title_survives_regrouping(analysed):
    ctx, _ = analysed
    eid = ctx.db.val("SELECT id FROM events WHERE count=8")
    ctx.db.run("UPDATE events SET title='春天的旅行', user_title=1 WHERE id=?", (eid,))
    grouping.rebuild(ctx)
    assert ctx.db.val("SELECT title FROM events WHERE count=8") == "春天的旅行"
