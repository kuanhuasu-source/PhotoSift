from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from photosift import contest, grouping, local_vlm, sun, taste, wizard
from photosift.server import create_app

from .test_server import analyse, wait


def ts(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


def test_sun_phases_around_a_taipei_solstice():
    def at(h, m):
        return sun.light_for(ts(2026, 6, 21, h, m), 25.03, 121.56, 480)[1]

    assert [at(3, 0), at(4, 45), at(5, 10), at(12, 0), at(18, 30), at(19, 5), at(21, 0)] == [
        "night", "blue", "golden", "day", "golden", "blue", "night"]  # fmt: skip
    # Without a recorded time zone the clock may be an hour off: only the unmistakable cases are named.
    assert sun.light_for(ts(2026, 6, 21, 12, 0), 25.03, 121.56, None)[1] == "day"
    assert sun.light_for(ts(2026, 6, 21, 18, 40), 25.03, 121.56, None)[1] is None
    assert sun.light_for(ts(2026, 6, 21, 1, 0), 25.03, 121.56, None)[1] == "night"


def test_light_is_filled_in_during_analysis(analysed):
    ctx, _ = analysed
    rows = ctx.db.all("SELECT filename, light, sun_elev, meta_v, tz_offset FROM photos ORDER BY filename")
    with_gps = [r for r in rows if r["filename"][0] in "ABC"]
    assert all(r["sun_elev"] is not None and r["meta_v"] == 2 for r in with_gps)
    assert all(r["light"] == "day" for r in with_gps if r["filename"].startswith("A"))  # mid-morning in Taipei
    assert next(r for r in rows if r["filename"] == "old.jpg")["sun_elev"] is None  # no GPS, no capture time
    # an exact offset lets the same evening photos be told apart as golden hour
    ctx.db.run("UPDATE photos SET tz_offset=540 WHERE filename LIKE 'C%'")
    grouping.update_light(ctx)
    assert {r[0] for r in ctx.db.all("SELECT light FROM photos WHERE filename LIKE 'C%'")} <= {"golden", "blue", "night"}


def test_taste_learns_from_picks_and_rejects(analysed):
    ctx, _ = analysed
    assert taste.update_library(ctx)["ready"] is False  # nothing to learn from yet
    assert ctx.db.val("SELECT COUNT(*) FROM photos WHERE taste IS NOT NULL") == 0
    ids = [r[0] for r in ctx.db.all("SELECT id FROM photos ORDER BY id")]
    ctx.db.many("UPDATE photos SET flag='pick' WHERE id=?", [(i,) for i in ids[:4]])
    ctx.db.many("UPDATE photos SET flag='reject' WHERE id=?", [(i,) for i in ids[4:8]])
    info = taste.update_library(ctx)
    assert info["ready"] and info["picks"] == 4 and info["rejects"] == 4
    got = {r["id"]: r["taste"] for r in ctx.db.all("SELECT id, taste FROM photos")}
    assert all(v is not None for v in got.values())
    assert min(got[i] for i in ids[:4]) > max(got[i] for i in ids[4:8])  # what was picked outranks what was rejected
    ctx.db.run("UPDATE photos SET flag=NULL")
    assert taste.update_library(ctx)["ready"] is False and ctx.db.val("SELECT COUNT(*) FROM photos WHERE taste IS NOT NULL") == 0


def test_annotation_keeps_attributes_and_scores_apart(analysed, fakes):
    ctx, _ = analysed
    from photosift import pipeline
    from photosift.jobs import Job

    pipeline.annotate(ctx, Job(1, "a", "t"), limit=2)
    import json

    v = json.loads(ctx.db.val("SELECT vlm FROM photos WHERE vlm IS NOT NULL"))
    assert v["v"] == local_vlm.ANNOTATION_V and isinstance(v["composition"], int) and isinstance(v["techniques"], list)
    assert v["light_type"] in local_vlm.LIGHTING and v["shot"] in local_vlm.SHOTS and isinstance(v["main"], str)
    card = local_vlm.card({"caption": "c", "light": "golden", "vlm": v}, "初審 70 分")
    assert "黃金時刻" in card and "初審 70 分" in card and v["shot"] in card
    # an annotation from before these fields existed is picked up again on the next run
    ctx.db.run("UPDATE photos SET vlm='{\"caption\": \"old\"}' WHERE vlm IS NOT NULL")
    assert pipeline.annotate(ctx, Job(2, "a", "t"), limit=2)["annotated"] == 2
    assert ctx.db.val("SELECT COUNT(*) FROM photos WHERE vlm LIKE '%\"v\": 2%'") == 2


def test_suggested_sizes_scale_with_the_library():
    small, big = wizard.suggest_sizes(30), wizard.suggest_sizes(5000, max_entries=3)
    assert small["pool"] == 30 and small["triage"] == 0 and small["finals"] <= 15
    assert big == {"pool": 400, "calibration": 16, "triage": 48, "finals": 12, "picks": 3}
    assert wizard.suggest_sizes(1500)["pool"] == 120


@pytest.fixture
def client(ctx, library, fakes):
    app = create_app(ctx)
    with TestClient(app) as c:
        c.app_ctx, c.library, c.vlm, c.prov = ctx, library, fakes[0], fakes[1]
        yield c


def test_wizard_end_to_end(client):
    analyse(client)
    ctx = client.app_ctx
    qs = client.get("/api/wizard/questions").json()["questions"]
    assert [q["id"] for q in qs] == ["occasion", "judges", "strictness", "goal", "styles"]

    assert client.post("/api/wizard/parse", json={"text": "太短"}).status_code == 400
    parsed = client.post("/api/wizard/parse", json={"text": "二〇二六年公司攝影比賽簡章：主題為旅途，每人限投三張……" * 3}).json()
    assert set(parsed) == {"name", "theme", "rules", "judging", "max_entries", "constraints", "spec"}

    draft = {"name": "年度攝影比賽", "theme": "旅途中的寧靜時刻", "rules": "不可合成",
             "profile": {"judges": "主管或同事", "goal": "想得獎，投評審所好", "styles": ["寧靜、有餘韻"]},
             "constraints": {"date_from": "2026-03-01", "date_to": "2026-03-31"}, "qa": [], "scope": {}}  # fmt: skip
    pv = client.post("/api/wizard/preview", json={"draft": draft}).json()
    assert pv["library"] == 13 and pv["after_rules"] == 8 and pv["eligible"] == 6 and pv["sizes"]["pool"] == 6

    fu = client.post("/api/wizard/followups", json={"draft": draft}).json()["questions"]
    assert len(fu) == 2 and all(len(q["options"]) == 2 and q["question"] for q in fu)
    draft["qa"] = [{"q": fu[0]["question"], "a": fu[0]["options"][0]}]

    draft["constraints"] = {}
    r1 = client.post("/api/wizard/examples", json={"draft": draft, "round": 1, "labels": {}}).json()
    assert len(r1["photo_ids"]) == 8 and len(set(r1["photo_ids"])) == 8 and r1["probes"]
    draft["probes"] = r1["probes"]
    labels = {str(p): ("yes" if i < 3 else "no" if i < 6 else "maybe") for i, p in enumerate(r1["photo_ids"])}
    r2 = client.post("/api/wizard/examples", json={"draft": draft, "round": 2, "labels": labels}).json()
    assert r2["photo_ids"] and not set(r2["photo_ids"]) & set(r1["photo_ids"])  # round two shows new photos

    assert client.post("/api/wizard/insight", json={"draft": draft, "labels": {"1": "yes"}}).status_code == 400
    calls = client.vlm.calls
    ins = client.post("/api/wizard/insight", json={"draft": draft, "labels": labels}).json()
    assert ins["hypothesis"] and len(ins["observations"]) == 2 and len(ins["options"]) == 2
    assert client.vlm.calls == calls + 1 and client.prov.calls == []  # all of this stayed on the machine

    draft.update(labels=labels, insight=ins["hypothesis"], judging="評審為各部門主管")
    c = client.post("/api/wizard/create", json={"draft": draft}).json()
    b = c["brief"]
    assert b["theme"] == "旅途中的寧靜時刻" and b["profile"]["judges"] == "主管或同事" and b["insight"] == ins["hypothesis"]
    assert len(b["examples"]["liked"]) == 3 and len(b["examples"]["disliked"]) == 3 and b["qa"] == draft["qa"]
    assert "評審為各部門主管" in b["notes"] and b["sizes"]["picks"] == 5

    # the external model gets the whole conversation, as text only, when it writes the rubric
    rubric = client.post(f"/api/contests/{c['id']}/coach", json={"use_cloud": True}).json()["rubric"]
    sent = client.prov.calls[-1]
    assert sent["images"] == 0 and "主管或同事" in sent["text"] and ins["hypothesis"] in sent["text"] and fu[0]["question"] in sent["text"]
    assert rubric["plan"] and rubric["probes"]
    pool = contest.candidate_pool(ctx, contest.get(ctx, c["id"]))
    assert set(b["examples"]["liked"]) <= set(pool) and not set(b["examples"]["disliked"]) & set(pool)


def test_library_filters_for_light_and_taste(client):
    analyse(client)
    ctx = client.app_ctx
    f = client.get("/api/facets").json()
    # the trip is clearly daytime; the dinner is near sunset with no time zone recorded, so it is left unlabelled
    assert f["lights"] == [{"key": "day", "label": "白天", "count": 8}]
    day = client.get("/api/photos", params={"light": "day"}).json()["total"]
    assert day == next(x["count"] for x in f["lights"] if x["key"] == "day")
    assert client.get("/api/status").json()["taste_ready"] is False
    ids = [p["id"] for p in client.get("/api/photos", params={"limit": 20}).json()["items"]]
    for i in ids[:4]:
        client.patch(f"/api/photos/{i}", json={"flag": "pick"})
    for i in ids[4:8]:
        client.patch(f"/api/photos/{i}", json={"flag": "reject"})
    top = client.get("/api/photos", params={"sort": "taste", "limit": 4}).json()["items"]
    assert all(p["taste"] is not None for p in top) and {p["id"] for p in top} <= set(ids[:4]) | set(ids[8:])
    assert client.get("/api/status").json()["taste_ready"] is True
