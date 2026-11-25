from __future__ import annotations

import numpy as np
import pytest

from photosift import contest, export
from photosift.cloud import CloudError

SIZES = {"pool": 10, "calibration": 6, "finals": 4, "picks": 2}


def test_spearman():
    assert contest.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert contest.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert contest.spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None
    assert contest.spearman([1, 2], [1, 2]) is None  # too few to say anything


def test_rubric_weights_are_normalised():
    r = contest.normalise_rubric(
        {"criteria": [{"name": "a", "weight": 3}, {"name": "b", "weight": 1}, {"name": "c", "weight": 0}], "judge_brief": "x"},
        "test",
    )
    assert [c["key"] for c in r["criteria"]] == ["c1", "c2", "c3"]
    assert sum(c["weight"] for c in r["criteria"]) == 100
    with pytest.raises(CloudError):
        contest.normalise_rubric({"criteria": [{"name": "only", "weight": 1}]}, "test")


def test_calibration_learns_a_systematic_offset():
    rng = np.random.default_rng(0)
    local = rng.uniform(40, 90, size=14)
    X = np.column_stack([local / 10, rng.uniform(4, 6, 14), rng.uniform(4, 6, 14)])
    teacher = 0.6 * local + 5 + rng.normal(0, 1.0, 14)  # the teacher is harsher and compresses the range
    cal = contest.fit_calibration(X, local, teacher)
    assert cal["model"]["kind"] in ("linear", "ridge")
    assert cal["mae"][cal["model"]["kind"]] < cal["mae"]["identity"]
    assert cal["agreement_before"] > 0.9
    pred = contest.predict(cal["model"], X, local)
    assert np.abs(pred - teacher).mean() < 3


def test_calibration_keeps_identity_when_already_close():
    local = np.array([50.0, 60, 70, 80, 55, 65])
    X = np.column_stack([local / 10, np.ones(6), np.ones(6)])
    cal = contest.fit_calibration(X, local, local + np.array([0.3, -0.2, 0.1, -0.3, 0.2, 0.0]))
    assert cal["model"]["kind"] == "identity"


def test_candidate_pool_drops_duplicates_flaws_and_rejects(analysed):
    ctx, _ = analysed
    cid = contest.create(ctx, "t", "主題", sizes=SIZES)
    pool = contest.candidate_pool(ctx, contest.get(ctx, cid))
    rows = {r["id"]: r for r in ctx.db.all("SELECT id, is_best, flags, filename FROM photos")}
    assert all(rows[p]["is_best"] and rows[p]["flags"] == "[]" for p in pool)
    assert len(pool) == 10  # capped by the pool size

    victim = pool[0]
    ctx.db.run("UPDATE photos SET flag='reject' WHERE id=?", (victim,))
    assert victim not in contest.candidate_pool(ctx, contest.get(ctx, cid))

    # a photo the user picked competes even if it was flagged
    blurry = ctx.db.val("SELECT id FROM photos WHERE flags LIKE '%blurry%'")
    ctx.db.run("UPDATE photos SET flag='pick', is_best=1 WHERE id=?", (blurry,))
    assert contest.candidate_pool(ctx, contest.get(ctx, cid))[0] == blurry


def test_scope_limits_candidates(analysed):
    ctx, _ = analysed
    eid = ctx.db.val("SELECT id FROM events WHERE count=4")
    cid = contest.create(ctx, "t", "主題", scope={"events": [eid]}, sizes=SIZES)
    c = contest.get(ctx, cid)
    assert contest.scope_count(ctx, c["brief"]["scope"]) == 4
    assert len(contest.candidate_pool(ctx, c)) == 4


def test_offline_flow_needs_no_external_model(analysed, fakes):
    ctx, _ = analysed
    _, prov = fakes
    cid = contest.create(ctx, "t", "主題", sizes=SIZES)
    rubric = contest.coach(ctx, cid, use_cloud=False)
    assert rubric["source"] == "builtin"
    out = contest.score_local(ctx, cid)
    assert out["scored"] == 10
    assert prov.calls == []  # nothing left the machine
    assert len(contest.results(ctx, cid)) == 10
    assert contest.usage(ctx, cid)["total_tokens"] == 0


def test_full_collaboration_flow(analysed, fakes, tmp_path):
    ctx, _ = analysed
    vlm, prov = fakes
    cid = contest.create(ctx, "年度比賽", "旅途", "最多兩張", "重視意境", sizes=SIZES)

    # 1. coach: text only
    rubric = contest.coach(ctx, cid)
    assert prov.calls[-1]["images"] == 0
    assert [c["weight"] for c in rubric["criteria"]] == [50, 30, 20]

    # 2. local judge: every candidate, zero external calls
    before = len(prov.calls)
    assert contest.score_local(ctx, cid)["scored"] == 10
    assert len(prov.calls) == before
    assert vlm.systems[-1].startswith("評審須知")

    # 3. calibration: only the sample is uploaded
    sample = contest.calibration_sample(ctx, cid, SIZES["calibration"])
    assert len(sample) == 6
    cal = contest.calibrate(ctx, cid)
    sent = [c for c in prov.calls[before:] if c["images"]]
    assert sum(c["images"] for c in sent) == 6 and sorted(sum((c["ids"] for c in sent), [])) == sorted(sample)
    assert cal["n"] == 6 and cal["notes"] == "請把技術分數打嚴一點" and len(cal["rounds"]) == 1
    assert ctx.db.val("SELECT COUNT(*) FROM contest_scores WHERE contest_id=? AND calibrated IS NULL", (cid,)) == 0
    # where the teacher scored a photo, its score is the calibrated one
    r = ctx.db.one("SELECT teacher_total, calibrated FROM contest_scores WHERE contest_id=? AND teacher IS NOT NULL", (cid,))
    assert r["teacher_total"] == r["calibrated"]

    # re-scoring carries the teacher's notes to the local judge and costs no tokens
    tokens = contest.usage(ctx, cid)["total_tokens"]
    contest.score_local(ctx, cid, rescore=True)
    assert "請把技術分數打嚴一點" in vlm.systems[-1]
    assert contest.usage(ctx, cid)["total_tokens"] == tokens
    assert len(contest.get(ctx, cid)["calibration"]["rounds"]) == 2

    # 4. finals: only the finalists, then a side-by-side jury round
    before = len(prov.calls)
    finalists = contest.finalists(ctx, cid)
    assert len(finalists) == 4
    summary = contest.finals(ctx, cid)
    critique = [c for c in prov.calls[before:] if "photos" in c["props"]]
    assert sorted(sum((c["ids"] for c in critique), [])) == sorted(finalists)
    assert len(summary["picks"]) == 2 and len(summary["ranking"]) == 4
    res = contest.results(ctx, cid)
    assert [r["final_rank"] for r in res[:4]] == [1, 2, 3, 4]
    assert [r["photo_id"] for r in res[:4]] == [x["id"] for x in summary["ranking"]]
    assert all(r["final"]["title"] for r in res[:4]) and res[4]["final_rank"] is None

    u = contest.usage(ctx, cid)
    assert set(u["by_purpose"]) == {"coach", "calibrate", "coach_notes", "finals", "jury"}
    assert u["images_sent"] == 6 + 4 + 4

    # export copies originals in rank order and never moves them
    out = export.export_contest(ctx, cid, str(tmp_path / "out"))
    files = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert out["copied"] == 4 and "評審報告.html" in files
    assert sum(f.startswith(("01_", "02_", "03_", "04_")) for f in files) == 4
    for r in res[:4]:
        assert ctx.db.val("SELECT path FROM photos WHERE id=?", (r["photo_id"],))
        from pathlib import Path

        assert Path(r["path"]).exists()


def test_new_rubric_discards_old_scores(analysed, fakes):
    ctx, _ = analysed
    cid = contest.create(ctx, "t", "主題", sizes=SIZES)
    contest.coach(ctx, cid, use_cloud=False)
    contest.score_local(ctx, cid)
    contest.coach(ctx, cid)
    assert ctx.db.val("SELECT COUNT(*) FROM contest_scores WHERE contest_id=?", (cid,)) == 0


def test_max_per_event_spreads_finalists(analysed, fakes):
    ctx, _ = analysed
    cid = contest.create(ctx, "t", "主題", sizes={**SIZES, "finals": 6}, max_per_event=1)
    contest.coach(ctx, cid, use_cloud=False)
    contest.score_local(ctx, cid)
    ids = contest.finalists(ctx, cid)
    events = [ctx.db.val("SELECT event_id FROM photos WHERE id=?", (i,)) for i in ids]
    assert len(events) == len(set(events))
