from __future__ import annotations

import numpy as np
import pytest

from photosift import contest, export
from photosift.cloud import CloudError

SIZES = {"pool": 10, "calibration": 6, "triage": 0, "finals": 4, "picks": 2}  # triage has its own tests below


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
    assert contest.scope_count(ctx, c["brief"]) == 4
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

    # 4. finals: only finalists the teacher has not already critiqued are uploaded, then one jury sheet
    before = len(prov.calls)
    finalists = contest.finalists(ctx, cid)
    assert len(finalists) == 4
    summary = contest.finals(ctx, cid)
    critique = [c for c in prov.calls[before:] if "photos" in c["props"]]
    fresh = sorted(set(finalists) - set(sample))
    assert sorted(sum((c["ids"] for c in critique), [])) == fresh and summary["reused"] == 4 - len(fresh)
    jury = [c for c in prov.calls[before:] if "ranking" in c["props"]]
    assert len(jury) == 1 and jury[0]["images"] == 1  # one contact sheet, not four more uploads
    assert len(summary["picks"]) == 2 and len(summary["ranking"]) == 4
    res = contest.results(ctx, cid)
    assert [r["final_rank"] for r in res[:4]] == [1, 2, 3, 4]
    assert [r["photo_id"] for r in res[:4]] == [x["id"] for x in summary["ranking"]]
    assert all(r["final"]["title"] for r in res[:4]) and res[4]["final_rank"] is None

    u = contest.usage(ctx, cid)
    assert {"coach", "calibrate", "coach_notes", "jury"} <= set(u["by_purpose"])
    assert u["images_sent"] == 6 + len(fresh) + 1
    assert u["photos_seen"] == len(set(sample) | set(finalists))

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


# ----------------------------------------------------------------- the wider funnel


def test_hard_rules_filter_before_any_model(analysed):
    ctx, _ = analysed

    def count(**cons):
        return contest.scope_count(ctx, {"scope": {}, "constraints": cons})

    assert count() == 13
    assert count(date_from="2026-09-01") == 4  # the dinner; the undated scan cannot prove its date and is left out
    assert count(date_from="2026-03-01", date_to="2026-03-01") == 8
    assert count(date_to="2026-03-01") == 8
    assert count(orientation="landscape") == 13 and count(orientation="portrait") == 0
    assert count(min_long_edge=900) == 13 and count(min_long_edge=901) == 0
    ctx.db.run("UPDATE photos SET camera='Apple iPhone 17 Pro' WHERE filename LIKE 'C%'")
    assert count(device="phone") == 4 and count(device="camera") == 8  # the scan has no camera recorded
    ctx.db.run("UPDATE photos SET vlm=? WHERE filename='A0.jpg'", ('{"people": 2}',))
    assert count(people="none") == 12
    cid = contest.create(ctx, "t", "主題", sizes=SIZES, extra={"constraints": {"date_from": "2026-09-01"}, "junk": 1})
    c = contest.get(ctx, cid)
    assert "junk" not in c["brief"] and len(contest.candidate_pool(ctx, c)) == 4


def test_theme_probes_and_examples_shape_the_pool(analysed, fakes):
    ctx, _ = analysed
    ids = [r[0] for r in ctx.db.all("SELECT id FROM photos WHERE is_best=1 AND flags='[]' ORDER BY id")]
    liked, disliked = ids[:2], ids[2:4]
    cid = contest.create(ctx, "t", "寧靜", sizes={**SIZES, "pool": 6}, extra={"examples": {"liked": liked, "disliked": disliked}})
    contest.coach(ctx, cid)
    c = contest.get(ctx, cid)
    assert [p["en"] for p in c["rubric"]["probes"]] == ["a quiet street", "reflections on water"] and c["rubric"]["plan"]
    ranked = contest.rank_candidates(ctx, c)
    got = [d["id"] for d in ranked]
    assert set(liked) <= set(got[:2])  # what the user said fits is always in
    assert not set(disliked) & set(got)  # what they said does not fit never is
    assert all(d["theme"] is not None and d["probe"] in ("安靜的街道", "水邊的倒影") and d["pref"] is not None for d in ranked)
    assert sum(d["quota"] for d in ranked) >= 2  # each reading of the theme has seats
    contest.score_local(ctx, cid)
    rec = contest.results(ctx, cid)[0]["local"]
    assert rec["probe"] in ("安靜的街道", "水邊的倒影") and rec["theme"] is not None


def test_wizard_answers_reach_both_judges(analysed, fakes):
    ctx, _ = analysed
    vlm, prov = fakes
    extra = {"profile": {"judges": "大眾票選", "goal": "想得獎"}, "qa": [{"q": "有人的畫面算嗎", "a": "不算"}],
             "insight": "偏好大面積留白"}  # fmt: skip
    cid = contest.create(ctx, "t", "寧靜", sizes=SIZES, extra=extra)
    contest.coach(ctx, cid)
    coach_prompt = prov.calls[-1]["text"]
    assert "大眾票選" in coach_prompt and "有人的畫面算嗎" in coach_prompt and "偏好大面積留白" in coach_prompt
    contest.score_local(ctx, cid)
    judge_system, judge_user = contest.local_prompt(contest.get(ctx, cid), ctx)
    assert "大眾票選" in judge_user and "不算" in judge_user


def test_contact_sheet_triage_widens_what_the_external_model_sees(analysed, fakes):
    ctx, _ = analysed
    _, prov = fakes
    cid = contest.create(ctx, "t", "主題", sizes={"pool": 10, "calibration": 4, "triage": 16, "finals": 3, "picks": 2})
    contest.coach(ctx, cid, use_cloud=False)
    contest.score_local(ctx, cid)
    out = contest.outgoing(ctx, cid, "finals")
    assert out["mode"] == "sheets" and out["sheets"] == 1 and len(out["photo_ids"]) == 10 and out["full"] == 3
    before = len(prov.calls)
    summary = contest.finals(ctx, cid)
    calls = prov.calls[before:]
    tri = [c for c in calls if "picks" in c["props"]]
    assert len(tri) == 1 and tri[0]["images"] == 1 and "初審" in tri[0]["text"]  # one sheet, with a text card per photo
    res = contest.results(ctx, cid)
    picked = [r for r in res if r["triage"] and r["triage"]["picked"]]
    assert len(picked) == 3 and sum(r["triage"] is not None for r in res) == 10
    assert {r["photo_id"] for r in picked} == {r["photo_id"] for r in res if r["final_rank"]}
    assert summary["triaged"] == 10
    u = contest.usage(ctx, cid)
    assert u["images_sent"] == 1 + 3 + 1 and u["photos_seen"] == 10  # 5 uploads let it look at 10 photos
    assert u["by_purpose"]["triage"]["photos"] == 10


def test_triage_widens_when_the_judges_disagree():
    def c(agree):
        cal = {"rounds": [{"agreement": agree}]} if agree is not None else None
        return {"brief": {"sizes": {"triage": 48, "finals": 12}}, "calibration": cal}

    assert contest.triage_size(c(0.9), 500) == 48
    assert contest.triage_size(c(0.6), 500) == 80  # 1.5x, rounded up to whole sheets
    assert contest.triage_size(c(0.2), 500) == 96
    assert contest.triage_size(c(None), 500) == 48
    assert contest.triage_size(c(0.2), 30) == 30  # never more than there are
    assert contest.triage_size(c(0.9), 12) == 0  # nothing to narrow down
    assert contest.triage_size({"brief": {"sizes": {"triage": 0, "finals": 12}}}, 500) == 0
