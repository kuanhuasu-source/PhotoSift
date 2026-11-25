from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageCms, ImageDraw

from photosift import contest, editing, export, imaging, retouch
from photosift.server import create_app

from .conftest import make_photo
from .test_server import analyse, wait


def scene(size=(900, 600), level=120) -> Image.Image:
    """A picture with a horizon and a few verticals, so geometry has something to hold on to."""
    im = Image.new("RGB", size, (level, level + 10, level + 30))
    d = ImageDraw.Draw(im)
    w, h = size
    d.rectangle([0, h * 0.55, w, h], fill=(level - 60, level - 40, level - 70))
    for x in (0.2, 0.45, 0.7):
        d.rectangle([w * x, h * 0.25, w * x + 26, h * 0.55], fill=(level + 70, level + 60, level + 40))
    return im


def mean(im) -> float:
    return float(np.asarray(im.convert("RGB"), dtype=np.float32).mean())


def test_clean_clamps_and_drops_neutral_values():
    assert editing.clean(None) == {}
    assert editing.clean({"exposure": 0, "contrast": 0.2, "bw": False, "junk": 5}) == {}
    assert editing.clean({"exposure": 9, "contrast": -500, "rotate": 0.004}) == {"exposure": 1.5, "contrast": -100}
    assert editing.clean({"sharpen": "x", "vignette": float("nan")}) == {}
    # a crop that keeps almost nothing is treated as a mistake, a full-frame crop as no crop
    assert editing.clean({"crop": {"left": 0.45, "top": 0, "right": 0.55, "bottom": 1}}) == {}
    assert editing.clean({"crop": {"left": 0, "top": 0, "right": 1, "bottom": 1}}) == {}
    assert editing.clean({"crop": {"left": 0.1, "top": 0, "right": 0.9, "bottom": 1}})["crop"]["left"] == 0.1
    assert editing.is_identity({"exposure": 0.001})


def test_identity_recipe_returns_the_same_image():
    im = scene()
    assert editing.render(im, {}) is im
    assert editing.render(im, {"contrast": 0}) is im


def test_each_control_moves_the_picture_the_right_way():
    im = scene()
    a = np.asarray(im, dtype=np.float32)
    assert mean(editing.render(im, {"exposure": 0.5})) > mean(im) + 8
    assert mean(editing.render(im, {"exposure": -0.5})) < mean(im) - 8
    assert np.asarray(editing.render(im, {"contrast": 50}), dtype=np.float32).std() > a.std() + 3
    dark = a.mean(axis=2) < 90
    lifted = np.asarray(editing.render(im, {"shadows": 60}), dtype=np.float32)
    assert lifted[dark].mean() > a[dark].mean() + 5
    bright = a.mean(axis=2) > 150
    tamed = np.asarray(editing.render(im, {"highlights": -60}), dtype=np.float32)
    assert tamed[bright].mean() < a[bright].mean() - 5
    warm = np.asarray(editing.render(im, {"temperature": 50}), dtype=np.float32)
    assert (warm[..., 0] - warm[..., 2]).mean() > (a[..., 0] - a[..., 2]).mean() + 5
    assert abs(mean(editing.render(im, {"temperature": 50})) - mean(im)) < 6  # colour shift, not a brightness change
    bw = np.asarray(editing.render(im, {"bw": True}))
    assert (bw[..., 0] == bw[..., 1]).all() and (bw[..., 1] == bw[..., 2]).all()
    corners = np.asarray(editing.render(im, {"vignette": -80}), dtype=np.float32)
    assert corners[:40, :40].mean() < a[:40, :40].mean() - 10 and abs(corners[280:320, 430:470].mean() - a[280:320, 430:470].mean()) < 3
    out = editing.render(im, {"exposure": 1.5, "contrast": 100, "saturation": 100, "sharpen": 100})
    assert out.size == im.size and out.mode == "RGB"


def test_crop_and_straighten_geometry():
    im = scene((900, 600))
    out = editing.render(im, {"crop": {"left": 0.1, "top": 0.2, "right": 0.9, "bottom": 1.0}})
    assert out.size == (720, 480)
    white = Image.new("RGB", (900, 600), (255, 255, 255))
    lvl = editing.straighten(white, 5)
    assert np.asarray(lvl).min() == 255  # no empty corners
    assert abs(lvl.width / lvl.height - 1.5) < 0.01 and lvl.width < 900


def test_tilt_is_measured_on_a_scene_with_clear_lines():
    level = scene((1200, 800))
    t0, _ = editing.detect_tilt(level)
    assert abs(t0) <= 0.3
    crooked = editing.straighten(level, 3.0)
    tilt, conf = editing.detect_tilt(crooked)
    assert abs(tilt + 3.0) <= 0.4 and conf >= 0.6
    fixed = editing.straighten(crooked, tilt)
    assert abs(editing.detect_tilt(fixed)[0]) <= 0.4


def test_tone_stats_only_flags_clear_problems():
    ramp = Image.fromarray(np.tile(np.linspace(15, 240, 400, dtype=np.uint8), (300, 1))).convert("RGB")
    assert editing.tone_stats(ramp)["need"] == {}  # full range, normal brightness: nothing to fix
    dark = editing.tone_stats(ramp.point(lambda v: v * 0.3))
    assert dark["need"]["exposure"] > 0.5
    flat = Image.new("RGB", (400, 300), (128, 128, 128))
    assert "contrast" in editing.tone_stats(flat)["need"]


def test_colour_profile_survives_previews_and_edits(tmp_path):
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    scene().save(tmp_path / "p.jpg", "JPEG", icc_profile=icc)
    im, _ = imaging.load_image(tmp_path / "p.jpg", 400)
    assert im.info.get("icc_profile") == icc
    assert editing.render(im, {"exposure": 0.3}).info.get("icc_profile") == icc
    back = Image.open(io.BytesIO(imaging.jpeg_bytes(im)))
    assert back.info.get("icc_profile") == icc and len(back.getexif()) == 0


def test_model_direction_plus_measurement_sets_the_amount():
    plain = {"need": {}, "tilt": 0.0, "tilt_confidence": 0.0}
    r = retouch.intents_to_recipe({"exposure": 1, "shadows": 2, "warmth": -1, "clarity": 1}, plain)
    assert r == {"exposure": 0.2, "shadows": 30, "temperature": -8, "clarity": 10}
    # a photo measured one stop too dark: a small "brighter" becomes a real correction
    dark = {"need": {"exposure": 1.0}, "tilt": 0.0, "tilt_confidence": 0.0}
    assert retouch.intents_to_recipe({"exposure": 1}, dark)["exposure"] == 0.6
    assert retouch.intents_to_recipe({"exposure": 3}, dark)["exposure"] == 1.0
    # ...but measurement never overrides the model's choice of direction, or its choice to leave it
    assert "exposure" not in retouch.intents_to_recipe({"exposure": 0}, dark)
    assert retouch.intents_to_recipe({"exposure": -1}, dark)["exposure"] == -0.2
    assert retouch.intents_to_recipe({}, plain) == {}
    assert retouch.intents_to_recipe({"black_white": True, "saturation": 2}, plain) == {"bw": True}


def test_straighten_and_crop_need_evidence():
    sure = {"need": {}, "tilt": -2.5, "tilt_confidence": 0.9}
    unsure = {"need": {}, "tilt": -2.5, "tilt_confidence": 0.3}
    assert retouch.intents_to_recipe({"straighten": True}, sure) == {"rotate": -2.5}
    assert retouch.intents_to_recipe({"straighten": True}, unsure) == {}
    assert retouch.intents_to_recipe({"straighten": False}, sure) == {}
    box = {"crop_left": 10, "crop_top": 0, "crop_right": 90, "crop_bottom": 100}
    assert retouch.intents_to_recipe(box, unsure)["crop"] == {"left": 0.1, "top": 0.0, "right": 0.9, "bottom": 1.0}
    tiny = {"crop_left": 40, "crop_top": 40, "crop_right": 60, "crop_bottom": 60}
    assert retouch.intents_to_recipe(tiny, unsure) == {}
    assert retouch.intents_to_recipe({"crop_left": 0, "crop_top": 0, "crop_right": 100, "crop_bottom": 100}, unsure) == {}


def test_suggestions_are_capped_and_reviews_nudge():
    wild = retouch.intents_to_recipe({"saturation": 3, "contrast": 3, "warmth": 3}, {"need": {"contrast": 45}})
    assert wild["vibrance"] <= 35 and wild["contrast"] <= 45 and wild["temperature"] <= 30
    base = {"exposure": 0.6, "contrast": 20}
    assert retouch.apply_review(base, {"verdict": "good", "exposure": 2}) == base
    assert retouch.apply_review(base, {"verdict": "worse"}) == {"exposure": 0.3, "contrast": 10}
    assert retouch.apply_review(base, {"verdict": "less", "exposure": -1})["exposure"] == pytest.approx(0.4)
    assert retouch.apply_review(base, {"verdict": "more", "exposure": 1}, weight=0.5)["exposure"] == pytest.approx(0.7)
    assert "轉正" in retouch.describe({"rotate": 1.5, "exposure": 0.4}) and retouch.describe({}) == "沒有調整"


def test_local_suggestion_with_self_review(analysed, fakes):
    ctx, _ = analysed
    vlm, prov = fakes
    pid = ctx.db.val("SELECT id FROM photos WHERE filename='A0.jpg'")
    s = retouch.suggest_local(ctx, pid, rounds=2)
    assert s["source"] == "local" and s["model"] == "fake-vlm" and s["recipe"]
    assert [v["verdict"] for v in s["reviews"]] == ["less", "good"]  # corrected once, then accepted
    assert s["reviews"][0]["changed"] is True
    assert prov.calls == []  # nothing was uploaded
    assert retouch.load(ctx, pid)["suggested"]["local"]["recipe"] == s["recipe"]
    assert retouch.load(ctx, pid)["recipe"] == {}  # a suggestion is not applied until the user says so
    before = vlm.calls
    retouch.suggest_local(ctx, pid, rounds=0)
    assert vlm.calls == before + 1  # no self-review round when it is switched off


@pytest.fixture
def client(ctx, library, fakes):
    app = create_app(ctx)
    with TestClient(app) as c:
        c.app_ctx, c.library, c.prov = ctx, library, fakes[1]
        yield c


def test_edit_api_round_trip_and_export(client, tmp_path):
    analyse(client)
    ctx = client.app_ctx
    row = ctx.db.one("SELECT id, path, width, height FROM photos WHERE filename='A1.jpg'")
    pid, src = row["id"], Path(row["path"])
    before = (src.stat().st_size, src.stat().st_mtime, src.read_bytes())

    d = client.get(f"/api/edit/{pid}").json()
    assert d["recipe"] == {} and "brightness" in d["stats"] and "tilt" in d["stats"]
    recipe = {"exposure": 0.4, "contrast": 0, "crop": {"left": 0.1, "top": 0.0, "right": 0.9, "bottom": 1.0}}
    assert client.put(f"/api/edit/{pid}", json={"recipe": recipe}).json()["recipe"] == {
        "exposure": 0.4, "crop": {"left": 0.1, "top": 0.0, "right": 0.9, "bottom": 1.0}}  # fmt: skip
    listed = {p["id"]: p for p in client.get("/api/photos").json()["items"]}
    assert listed[pid]["edited"] == 1 and sum(bool(p["edited"]) for p in listed.values()) == 1

    import json as _json

    r = client.get(f"/api/edit/{pid}/render", params={"r": _json.dumps(recipe)})
    assert r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"] == "no-store"
    w, h = Image.open(io.BytesIO(r.content)).size
    assert abs(w / h - (0.8 * row["width"]) / row["height"]) < 0.02  # the preview shows the crop
    assert client.get(f"/api/edit/{pid}/render", params={"r": "not json"}).status_code == 400
    assert client.get("/api/edit/99999").status_code == 404

    out = client.post("/api/edit/export", json={"dest": str(tmp_path / "out"), "photo_ids": [pid, pid + 1]}).json()
    assert out["exported"] == 1 and out["skipped"] == 1  # the second photo has no edit
    f = tmp_path / "out" / out["files"][0]
    done = Image.open(f)
    assert done.size == (round(row["width"] * 0.8), row["height"])  # full resolution, cropped
    ex = done.getexif()
    assert ex.get_ifd(0x8769)[0x9003] == "2026:03:01 09:20:00" and ex.get(0x0112) == 1  # when it was taken stays
    assert (src.stat().st_size, src.stat().st_mtime, src.read_bytes()) == before  # the original is untouched

    # clearing the edit takes the photo back to untouched
    assert client.put(f"/api/edit/{pid}", json={"recipe": {}}).json()["recipe"] == {}
    assert not any(p["edited"] for p in client.get("/api/photos").json()["items"])
    assert client.post("/api/edit/export", json={"dest": str(tmp_path / "out2"), "photo_ids": [pid]}).json()["exported"] == 0


def test_suggest_and_cloud_review_endpoints(client):
    analyse(client)
    pid = client.get("/api/photos", params={"limit": 1}).json()["items"][0]["id"]
    s = client.post(f"/api/edit/{pid}/suggest", json={"rounds": 1}).json()
    assert s["source"] == "local" and s["recipe"] and len(s["reviews"]) == 1
    assert client.get(f"/api/edit/{pid}").json()["suggested"]["local"]["recipe"] == s["recipe"]

    assert client.post(f"/api/edit/{pid}/review", json={"recipe": {}}).status_code == 400  # nothing to review
    calls = len(client.prov.calls)
    r = client.post(f"/api/edit/{pid}/review", json={"recipe": {"exposure": 0.8}}).json()
    assert r["verdict"] == "less" and r["recipe"]["exposure"] == pytest.approx(0.6) and r["tokens"] > 0
    assert len(client.prov.calls) == calls + 1 and client.prov.calls[-1]["images"] == 1  # one composite image
    assert client.get("/api/usage").json()["total_tokens"] == r["tokens"]


def test_finals_bring_an_edit_for_each_finalist(client, tmp_path):
    analyse(client)
    ctx = client.app_ctx
    cid = client.post("/api/contests", json={"name": "c", "theme": "t", "sizes": {"pool": 8, "calibration": 4, "finals": 3, "picks": 1}}).json()["id"]
    client.post(f"/api/contests/{cid}/coach", json={"use_cloud": False})
    client.post(f"/api/contests/{cid}/local")
    wait(client)
    images_before = sum(c["images"] for c in client.prov.calls)
    client.post(f"/api/contests/{cid}/finals")
    assert wait(client)["status"] == "done"
    finalists = [r for r in client.get(f"/api/contests/{cid}/results").json() if r["final_rank"]]
    assert len(finalists) == 3
    # the edits came in the same finals calls: 3 critique images + 3 jury images, nothing extra
    assert sum(c["images"] for c in client.prov.calls) - images_before == 6
    for r in finalists:
        sug = retouch.load(ctx, r["photo_id"])["suggested"]["cloud"]
        assert sug["recipe"]["exposure"] == 0.2 and sug["recipe"]["crop"]["left"] == 0.05 and sug["summary"]
        assert r["edited"] in (None, 0)  # suggested, not applied

    first = finalists[0]["photo_id"]
    cloud = retouch.load(ctx, first)["suggested"]["cloud"]["recipe"]
    client.put(f"/api/edit/{first}", json={"recipe": cloud})
    out = client.post(f"/api/contests/{cid}/export", json={"dest": str(tmp_path / "entry")}).json()
    names = sorted(p.name for p in (tmp_path / "entry").iterdir())
    assert out["copied"] == 3 and out["edited"] == 1
    assert sum(n.endswith("_修圖.jpg") for n in names) == 1 and len(names) == 3 + 1 + 1  # originals, one edit, the report


def test_night_scenes_are_not_brightened_by_measurement(analysed):
    ctx, _ = analysed
    pid = ctx.db.val("SELECT id FROM photos WHERE filename='A0.jpg'")
    dark = Image.new("RGB", (600, 400), (18, 18, 30))
    ctx.db.run("UPDATE photos SET category='landscape' WHERE id=?", (pid,))
    st = retouch.photo_stats(ctx, pid, dark)
    assert st["need"]["exposure"] > 0 and retouch.intents_to_recipe({"exposure": 1}, st)["exposure"] > 0.5
    ctx.db.run("UPDATE photos SET category='night' WHERE id=?", (pid,))
    st = retouch.photo_stats(ctx, pid, dark)
    assert "exposure" not in st["need"] and st["dark_scene"] == "夜景"
    assert retouch.intents_to_recipe({"exposure": 1}, st)["exposure"] == 0.2  # only the model's own small step
    assert "不要為了數據而提亮" in retouch._measured(st)
    assert retouch._label("Exposure") == "曝光" and retouch._label("裁掉右側雜物") == "裁掉右側雜物"
