from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photosift import config
from photosift.server import create_app


@pytest.fixture
def client(ctx, library, fakes):
    app = create_app(ctx)
    with TestClient(app) as c:
        c.app_ctx = ctx
        c.jobs = app.state.jobs
        c.library = library
        yield c


def wait(client, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get("/api/job").json()["job"]
        if j and j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def analyse(client):
    assert client.post("/api/folders", json={"path": str(client.library)}).status_code == 200
    assert client.post("/api/analyze", json={}).status_code == 200
    j = wait(client)
    assert j["status"] == "done", j
    return j


def test_empty_library_status(client):
    s = client.get("/api/status").json()
    assert s["photos"] == 0 and s["folders"] == 0 and s["job"] is None
    assert client.post("/api/analyze", json={}).status_code == 400  # nothing to analyse yet


def test_folder_rules(client, tmp_path):
    assert client.post("/api/folders", json={"path": str(tmp_path / "nope")}).status_code == 400
    assert client.post("/api/folders", json={"path": str(client.library)}).status_code == 200
    # a folder already covered by one in the list is refused, with the reason
    r = client.post("/api/folders", json={"path": str(client.library / "trip")})
    assert r.status_code == 400 and "已經包含" in r.json()["detail"]
    r = client.post("/api/folders", json={"path": str(client.library.parent)})
    assert r.status_code == 400 and "上層" in r.json()["detail"]


def test_browse_and_listing(client):
    d = client.get("/api/browse", params={"path": str(client.library)}).json()
    assert d["dirs"] == ["dinner", "scans", "trip"] and d["parent"]
    analyse(client)
    d = client.get("/api/photos", params={"limit": 5, "sort": "time"}).json()
    assert d["total"] == 13 and len(d["items"]) == 5
    assert client.get("/api/photos", params={"best": 1}).json()["total"] == 11
    assert client.get("/api/photos", params={"quality": "blurry"}).json()["total"] == 1
    assert client.get("/api/photos", params={"dups": 1}).json()["total"] == 3
    assert len(client.get("/api/events").json()) == 3
    assert client.get("/api/groups").json()["total"] == 1
    assert client.get("/api/facets").json()["flaws"]["blurry"] == 1
    pid = d["items"][0]["id"]
    assert client.get(f"/api/thumb/{pid}").headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/preview/{pid}").headers["content-type"] == "image/jpeg"
    assert client.get("/api/thumb/99999").status_code == 404


def test_pick_reject_and_best_frame(client):
    analyse(client)
    pid = client.get("/api/photos", params={"limit": 1}).json()["items"][0]["id"]
    assert client.patch(f"/api/photos/{pid}", json={"flag": "pick", "rating": 4}).json() == {"id": pid, "rating": 4, "flag": "pick"}
    assert client.get("/api/photos", params={"flag": "pick"}).json()["total"] == 1
    assert client.patch(f"/api/photos/{pid}", json={"flag": ""}).json()["flag"] is None
    assert client.patch(f"/api/photos/{pid}", json={"flag": "delete"}).status_code == 400

    g = client.get("/api/groups").json()["groups"][0]
    other = next(p for p in g["photos"] if not p["is_best"])
    assert client.post(f"/api/groups/{g['id']}/best/{other['id']}").status_code == 200
    detail = client.get(f"/api/photos/{other['id']}").json()
    assert detail["is_best"] == 1 and sum(m["is_best"] for m in detail["group"]) == 1


def test_originals_are_never_modified(client):
    before = {p: (p.stat().st_size, p.stat().st_mtime) for p in Path(client.library).rglob("*.jpg")}
    analyse(client)
    cid = client.post("/api/contests", json={"name": "c", "theme": "t", "sizes": {"pool": 8, "calibration": 4, "finals": 3, "picks": 1}}).json()["id"]
    client.post(f"/api/contests/{cid}/coach", json={"use_cloud": False})
    client.post(f"/api/contests/{cid}/local")
    wait(client)
    fid = client.get("/api/folders").json()[0]["id"]
    assert client.delete(f"/api/folders/{fid}").status_code == 200  # forgetting a folder deletes nothing on disk
    after = {p: (p.stat().st_size, p.stat().st_mtime) for p in Path(client.library).rglob("*.jpg")}
    assert before == after
    assert client.get("/api/status").json()["photos"] == 0


def test_contest_over_http(client):
    analyse(client)
    c = client.post("/api/contests", json={"name": "年度", "theme": "旅途", "sizes": {"pool": 8, "calibration": 4, "finals": 3, "picks": 2}}).json()
    cid = c["id"]
    assert c["pool"] == 8 and c["rubric"] is None
    assert len(client.post(f"/api/contests/{cid}/coach", json={"use_cloud": True}).json()["rubric"]["criteria"]) == 3
    client.post(f"/api/contests/{cid}/local")
    assert wait(client)["result"]["scored"] == 8

    out = client.get(f"/api/contests/{cid}/outgoing/calibrate").json()
    assert len(out["photo_ids"]) == 4 and out["est_tokens"] > 0
    client.post(f"/api/contests/{cid}/calibrate")
    assert wait(client)["status"] == "done"
    assert len(client.get(f"/api/contests/{cid}/outgoing/finals").json()["photo_ids"]) == 3
    client.post(f"/api/contests/{cid}/finals")
    assert wait(client)["status"] == "done"

    c = client.get(f"/api/contests/{cid}").json()
    assert c["scored_local"] == 8 and c["scored_teacher"] == 4 and c["scored_final"] == 3
    assert len(c["summary"]["picks"]) == 2 and c["usage"]["images_sent"] == 4 + 3 + 3
    res = client.get(f"/api/contests/{cid}/results").json()
    assert res[0]["final_rank"] == 1 and len(res) == 8
    assert client.get("/api/usage").json()["total_tokens"] == c["usage"]["total_tokens"]

    # editing the rubric by hand resets the scores made with the old one
    rubric = c["rubric"]
    rubric["criteria"][0]["weight"] = 80
    c = client.put(f"/api/contests/{cid}/rubric", json={"rubric": rubric}).json()
    assert c["scored_local"] == 0 and c["rubric"]["source"] == "edited"
    assert client.delete(f"/api/contests/{cid}").status_code == 200
    assert client.get(f"/api/contests/{cid}").status_code == 404


def test_only_one_job_at_a_time(client):
    client.post("/api/folders", json={"path": str(client.library)})
    assert client.post("/api/analyze", json={}).status_code == 200
    r = client.post("/api/analyze", json={})
    assert r.status_code in (200, 409)  # 409 while the first is still running
    wait(client)


def test_cross_origin_writes_are_refused(client):
    r = client.post("/api/folders", json={"path": "/"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.get("/api/status", headers={"origin": "https://evil.example"}).status_code == 200


def test_settings_and_keys_never_echo_secrets(client, monkeypatch):
    monkeypatch.setattr(config, "_keyring", lambda: None)  # force the file fallback, away from any real keychain
    s = client.get("/api/settings").json()
    assert s["keys"] == {"claude": None, "openai": None, "gemini": None}
    assert client.post("/api/keys", json={"provider": "gemini", "key": "sk-test-123"}).json()["source"] == "file"
    body = client.get("/api/settings").text + client.get("/api/status").text
    assert "sk-test-123" not in body
    assert config.get_api_key("gemini") == "sk-test-123"
    secrets = config.data_dir() / "secrets.json"
    assert oct(secrets.stat().st_mode & 0o777) == "0o600"
    client.post("/api/keys", json={"provider": "gemini", "key": ""})
    assert config.get_api_key("gemini") is None and not secrets.exists()

    new = s["settings"]
    new["cloud"]["provider"] = "claude"
    new["analysis"]["event_gap_hours"] = 12
    assert client.put("/api/settings", json=new).json() == {"ok": True, "regroup_needed": True}
    assert client.get("/api/status").json()["cloud"]["label"] == "Claude"
    bad = dict(new, cloud={"provider": "nope"})
    assert client.put("/api/settings", json=bad).status_code == 400
