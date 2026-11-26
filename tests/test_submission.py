from __future__ import annotations

import io
import json
import struct
import time
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageCms
from PIL.TiffImagePlugin import IFDRational as R

from photosift import contest, pipeline, retouch, submission, wizard
from photosift.jobs import Job
from photosift.server import create_app

from .test_server import analyse, wait


def wide_gamut_profile() -> bytes:
    """A Display-P3-like ICC profile built by hand, so colour conversion can be tested without shipping one."""
    def s15(v):
        return struct.pack(">i", int(round(v * 65536)))

    def xyz(x, y, z):
        return b"XYZ " + b"\0" * 4 + s15(x) + s15(y) + s15(z)

    def curv(gamma):
        return b"curv" + b"\0" * 4 + struct.pack(">I", 1) + struct.pack(">H", int(round(gamma * 256))) + b"\0\0"

    def desc(s):
        b = s.encode("ascii") + b"\0"
        d = b"desc" + b"\0" * 4 + struct.pack(">I", len(b)) + b + b"\0" * 78
        return d + b"\0" * (-len(d) % 4)

    text = b"text" + b"\0" * 4 + b"none\0\0\0\0"
    tags = [(b"desc", desc("Test Wide Gamut")), (b"cprt", text), (b"wtpt", xyz(0.9642, 1.0, 0.8249)),
            (b"rXYZ", xyz(0.5151, 0.2412, -0.0011)), (b"gXYZ", xyz(0.2920, 0.6922, 0.0419)),
            (b"bXYZ", xyz(0.1571, 0.0666, 0.7841)), (b"rTRC", curv(2.2)), (b"gTRC", curv(2.2)), (b"bTRC", curv(2.2))]  # fmt: skip
    table, body, off = b"", b"", 128 + 4 + 12 * len(tags)
    for sig, data in tags:
        table += sig + struct.pack(">II", off + len(body), len(data))
        body += data
    head = struct.pack(">I", 128 + 4 + len(table) + len(body)) + b"lcms" + struct.pack(">I", 0x02100000) + b"mntrRGB XYZ "
    head += struct.pack(">6H", 2026, 1, 1, 0, 0, 0) + b"acsp" + b"APPL" + b"\0" * 24 + s15(0.9642) + s15(1.0) + s15(0.8249) + b"lcms" + b"\0" * 44
    return head + struct.pack(">I", len(tags)) + table + body


def big_photo(path: Path, seed: int, when: datetime, icc: bytes | None = None, flat=None, size=(3000, 2000)) -> None:
    """A large, detailed frame like a camera original: incompressible noise, capture time, GPS and a colour profile."""
    w, h = size
    if flat:
        arr = np.full((h, w, 3), flat, dtype=np.uint8)
    else:
        rng = np.random.default_rng(seed)
        base = Image.fromarray(rng.integers(0, 255, (20, 30, 3), dtype=np.uint8)).resize((w, h), Image.Resampling.BICUBIC)
        arr = np.clip(np.asarray(base).astype(int) + rng.integers(-45, 45, (h, w, 3)), 0, 255).astype(np.uint8)
    ex = Image.Exif()
    ex[0x010F], ex[0x0110] = "TestCam", "T-9"
    sub = ex.get_ifd(0x8769)
    sub[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
    sub[0x829D], sub[0x829A], sub[0x8827], sub[0x920A] = R(56, 10), R(1, 60), 100, R(35, 1)
    g = ex.get_ifd(0x8825)
    g[1], g[2], g[3], g[4] = "N", (R(35, 1), R(0, 1), R(0, 1)), "E", (R(135, 1), R(45, 1), R(0, 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = {"icc_profile": icc} if icc else {}
    Image.fromarray(arr).save(path, "JPEG", quality=97, exif=ex.tobytes(), **extra)


@pytest.fixture
def entered(ctx, tmp_path, fakes):
    """A small library of camera-sized originals with a contest ready for packaging."""
    root = tmp_path / "originals"
    icc = wide_gamut_profile()
    big_photo(root / "DSC_0001.jpg", 1, datetime(2026, 5, 1, 10, 0), icc)
    (root / "DSC_0001.dng").write_bytes(b"RAWDATA" * 50000)  # the paired RAW, 350 KB here
    big_photo(root / "DSC_0002.jpg", 2, datetime(2026, 5, 2, 10, 0))
    big_photo(root / "DSC_0003.jpg", 3, datetime(2026, 5, 3, 10, 0), icc, flat=(200, 60, 60), size=(1800, 1200))
    big_photo(root / "DSC_0004.jpg", 4, datetime(2025, 5, 3, 10, 0), size=(1500, 1000))
    ctx.db.run("INSERT INTO folders(path, added_at) VALUES (?,?)", (str(root), time.time()))
    pipeline.analyze(ctx, Job(1, "a", "t"))
    cid = contest.create(ctx, "年度攝影比賽", "旅途", sizes={"pool": 4, "calibration": 4, "triage": 0, "finals": 4, "picks": 3},
                         extra={"spec": {"long_edge": 2000, "max_mb": 1.0, "desc_max": 80}})  # fmt: skip
    contest.coach(ctx, cid, use_cloud=False)
    contest.score_local(ctx, cid)
    contest.finals(ctx, cid)
    ids = {r["filename"]: r["id"] for r in ctx.db.all("SELECT id, filename FROM photos")}
    submission.set_entries(ctx, cid, [ids["DSC_0001.jpg"], ids["DSC_0002.jpg"], ids["DSC_0003.jpg"]])
    return ctx, cid, ids, root


def test_spec_comes_from_the_announcement_then_the_user(entered):
    ctx, cid, _, _ = entered
    sp = submission.spec(ctx, cid)
    assert (sp["long_edge"], sp["max_mb"], sp["desc_max"], sp["exif"]) == (2000, 1.0, 80, "all")
    assert submission.set_spec(ctx, cid, {"max_mb": 0.4, "exif": "bogus", "name": "{nope}", "author": " 王小明 "}) == {
        **sp, "max_mb": 0.4, "author": "王小明"}  # fmt: skip
    assert submission.spec(ctx, cid)["long_edge"] == 2000  # untouched settings keep the announcement's value
    parsed = wizard._num("5", 0.2, 200), wizard._num(99999, 600, 12000), wizard._num("x", 1, 2)
    assert parsed == (5, None, None)


def test_entries_start_from_the_jurys_picks(analysed, fakes):
    ctx, _ = analysed
    cid = contest.create(ctx, "t", "主題", sizes={"pool": 8, "calibration": 4, "triage": 0, "finals": 4, "picks": 2})
    contest.coach(ctx, cid, use_cloud=False)
    contest.score_local(ctx, cid)
    summary = contest.finals(ctx, cid)
    assert submission.seed(ctx, cid) == 2 and submission.seed(ctx, cid) == 0  # seeded once
    es = submission.entries(ctx, cid)
    assert [e["photo_id"] for e in es] == summary["picks"]
    assert all(e["title"].startswith("評語") and e["status"] == "draft" for e in es)  # the jury's suggested titles
    # reordering and swapping keeps what was already written
    submission.update_entry(ctx, cid, es[0]["photo_id"], {"description": "我寫的", "junk": 1})
    other = next(r["photo_id"] for r in contest.results(ctx, cid) if r["photo_id"] not in summary["picks"])
    submission.set_entries(ctx, cid, [other, es[0]["photo_id"]])
    now = submission.entries(ctx, cid)
    assert [e["photo_id"] for e in now] == [other, es[0]["photo_id"]] and now[1]["description"] == "我寫的"


def test_facts_come_from_the_file_not_a_model(entered):
    ctx, cid, ids, _ = entered
    e = next(x for x in submission.entries(ctx, cid) if x["photo_id"] == ids["DSC_0001.jpg"])
    facts = dict(e["facts"])
    assert facts["拍攝時間"].startswith("2026-05-01 10:00") and facts["器材"] == "TestCam T-9"
    assert facts["拍攝參數"] == "35mm　f/5.6　1/60s　ISO 100" and facts["原始尺寸"] == "3000 × 2000 像素"
    assert "京都" in facts["拍攝地點"]
    assert e["raw_size"] == 350000


def test_background_questions_then_copy_from_text_only(entered):
    ctx, cid, ids, _ = entered
    pid = ids["DSC_0001.jpg"]
    qs = submission.ask_background(ctx, cid, pid)
    assert len(qs) == 2 and all(q["q"] and len(q["options"]) == 2 and q["a"] == "" for q in qs)
    qs[0]["a"] = "那天清晨只有我一個人"
    submission.update_entry(ctx, cid, pid, {"questions": qs})

    calls_before = ctx.db.val("SELECT COUNT(*) FROM cloud_calls")
    out = submission.write_copy(ctx, cid)
    assert out["written"] == 3 and out["tokens"] > 0
    call = ctx.db.one("SELECT purpose, images FROM cloud_calls ORDER BY id DESC LIMIT 1")
    assert (call["purpose"], call["images"]) == ("copy", 0)  # text only: no photo is uploaded again
    assert ctx.db.val("SELECT COUNT(*) FROM cloud_calls") == calls_before + 1  # one call covers every entry
    es = submission.entries(ctx, cid)
    assert all(e["description"] and len(e["description"]) <= 80 for e in es)
    # the jury's titles are kept; only the photo that never reached the jury gets one from the writer
    assert [e["title"][:2] for e in es] == ["評語", "評語", "名稱"] and es[2]["title"] == f"名稱{ids['DSC_0003.jpg']}"
    assert out["over_limit"] == 0


def test_overlong_descriptions_go_back_once_to_be_tightened(entered, fakes):
    ctx, cid, ids, _ = entered
    _, prov = fakes
    real, seen = prov.generate, []

    def wordy(system, parts, schema, max_tokens=0):
        res = real(system=system, parts=parts, schema=schema, max_tokens=max_tokens)
        seen.append(parts[0])
        if len(seen) == 1:  # first draft: one entry runs well over
            res.data["entries"][0]["description"] = "太長了" * 60
        elif len(seen) == 2:  # asked to tighten: only that one comes back, one is still too long
            res.data["entries"][0]["description"] = "精簡後的說明。"
        return res

    prov.generate = wordy
    out = submission.write_copy(ctx, cid)
    assert len(seen) == 2 and "改寫到 80 個字以內" in seen[1] and seen[1].count("作品編號") == 1
    assert out["written"] == 3 and out["over_limit"] == 0
    assert submission.entries(ctx, cid)[0]["description"] == "精簡後的說明。"
    assert ctx.db.val("SELECT COUNT(*) FROM cloud_calls WHERE purpose='copy' AND images=0") == 2


def test_copy_prompt_carries_the_photographers_words_and_forbids_invention(entered, fakes):
    ctx, cid, ids, _ = entered
    _, prov = fakes
    pid = ids["DSC_0002.jpg"]
    submission.update_entry(ctx, cid, pid, {"questions": [{"q": "為什麼拍", "options": [], "a": "想記住那道光"}]})
    submission.write_copy(ctx, cid)
    sent = prov.calls[-1]
    assert sent["images"] == 0 and "想記住那道光" in sent["text"] and "攝影師沒有補充拍攝動機" in sent["text"]
    assert "80 個字以內" in sent["text"] and "評審認為的優點" in sent["text"]
    assert "不可以編造" in submission.COPY_SYSTEM
    # only_empty leaves finished text alone
    submission.update_entry(ctx, cid, pid, {"description": "我自己改過的"})
    n = len(prov.calls)
    assert submission.write_copy(ctx, cid, only_empty=True)["written"] == 0 and len(prov.calls) == n
    # without an external model the on-device one writes instead
    ctx.db.run("UPDATE submissions SET description=NULL WHERE contest_id=?", (cid,))
    out = submission.write_copy(ctx, cid, use_cloud=False)
    assert out["written"] == 3 and out["tokens"] == 0 and len(prov.calls) == n
    assert all(e["description"].startswith("地端寫的說明") for e in submission.entries(ctx, cid))


def test_preflight_catches_what_would_be_rejected(entered):
    ctx, cid, ids, _ = entered
    checks = submission.preflight(ctx, cid)
    assert [c["level"] for c in checks if c["level"] == "error"] == []
    assert any("還沒有作品說明" in c["text"] for c in checks)
    assert any("1800 像素" in c["text"] and "不會放大" in c["text"] for c in checks)  # smaller than the upload size

    submission.set_entries(ctx, cid, list(ids.values()))  # four entries, limit is three
    assert any("超過每人 3 件" in c["text"] and c["level"] == "error" for c in submission.preflight(ctx, cid))
    submission.set_entries(ctx, cid, [ids["DSC_0004.jpg"]])
    brief = contest.get(ctx, cid)["brief"]
    brief["constraints"] = {"date_from": "2026-01-01", "date_to": "2026-12-31", "min_long_edge": 2000}
    ctx.db.run("UPDATE contests SET brief=? WHERE id=?", (json.dumps(brief), cid))
    texts = [c["text"] for c in submission.preflight(ctx, cid) if c["level"] == "error"]
    assert any("2025-05-03" in t and "不在規定的拍攝期間" in t for t in texts)
    assert any("長邊只有 1500 像素" in t for t in texts)
    submission.update_entry(ctx, cid, ids["DSC_0004.jpg"], {"description": "字" * 81})
    assert any("超過 80 字" in c["text"] for c in submission.preflight(ctx, cid))
    submission.set_entries(ctx, cid, [])
    assert submission.preflight(ctx, cid) == [{"level": "error", "text": "還沒有選擇要投稿的作品。"}]


def test_preliminary_package_is_small_srgb_and_complete(entered, tmp_path):
    ctx, cid, ids, root = entered
    before = {p: p.read_bytes() for p in root.iterdir()}
    for e in submission.entries(ctx, cid):
        submission.update_entry(ctx, cid, e["photo_id"], {"description": f"{e['filename']} 的說明"})
    out = submission.build_prelim(ctx, cid, str(tmp_path / "out"))
    folder = Path(out["folder"])
    assert folder.name == "年度攝影比賽_初審" and out["count"] == 3
    names = sorted(p.name for p in folder.iterdir())
    # the third photo never reached the jury (flat colour), so it has no suggested title yet
    assert names == ["01_評語_%d.jpg" % ids["DSC_0001.jpg"], "02_評語_%d.jpg" % ids["DSC_0002.jpg"], "03_未命名.jpg", "作品說明.html"]
    first, second, third = (Image.open(folder / n) for n in names[:3])
    # resized to the upload size (never enlarged) and each within the limit
    assert first.size == (2000, 1333) and third.size == (1800, 1200)
    assert all(f["bytes"] <= 1.0e6 for f in out["files"]) and out["files"][0]["quality"] < 92  # noise needs a lower quality to fit
    assert out["bytes"] < out["original_bytes"] / 2
    # everything is sRGB afterwards, and wide-gamut pixels were really converted
    assert all(ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(im.info["icc_profile"]))).startswith("sRGB")
               for im in (first, second, third))  # fmt: skip
    r, g, b = third.getpixel((900, 600))
    assert r > 212 and g < 45  # (200, 60, 60) in the wide profile is a more saturated red in sRGB
    # capture information is kept in full by default, GPS included
    ex = first.getexif()
    assert ex.get_ifd(0x8769)[0x9003] == "2026:05:01 10:00:00" and ex.get_ifd(0x8825)[1] == "N" and ex.get(0x0112) == 1
    # the written entry: titles, descriptions and facts, self-contained
    doc = (folder / "作品說明.html").read_text("utf-8")
    assert doc.count("<section") == 3 and "DSC_0002.jpg 的說明" in doc and "TestCam T-9" in doc and names[0] in doc
    assert "data:image/jpeg;base64," in doc and "http" not in doc and "<script" not in doc
    with zipfile.ZipFile(out["zip"]) as z:
        assert sorted(z.namelist()) == [f"年度攝影比賽_初審/{n}" for n in names]
    assert {e["status"] for e in submission.entries(ctx, cid)} == {"sent"}
    assert {p: p.read_bytes() for p in root.iterdir()} == before  # originals untouched
    # a second run does not overwrite the first
    again = submission.build_prelim(ctx, cid, str(tmp_path / "out"))
    assert Path(again["folder"]).name == "年度攝影比賽_初審_2"


def test_exif_policy_and_file_naming(entered, tmp_path):
    ctx, cid, ids, _ = entered
    submission.set_entries(ctx, cid, [ids["DSC_0001.jpg"]])
    submission.set_spec(ctx, cid, {"exif": "no_gps", "name": "{author}_{title}", "author": "王 小明", "max_mb": 0.3})
    out = submission.build_prelim(ctx, cid, str(tmp_path / "a"))
    f = Path(out["folder"]) / out["files"][0]["name"]
    assert f.name.startswith("王_小明_評語") and f.stat().st_size <= 300_000
    ex = Image.open(f).getexif()
    assert ex.get_ifd(0x8769)[0x9003] == "2026:05:01 10:00:00" and not ex.get_ifd(0x8825)  # date kept, coordinates gone
    submission.set_spec(ctx, cid, {"exif": "none", "name": "{original}"})
    out = submission.build_prelim(ctx, cid, str(tmp_path / "b"))
    f = Path(out["folder"]) / out["files"][0]["name"]
    assert f.name == "DSC_0001.jpg" and len(Image.open(f).getexif()) == 0


def test_full_package_only_for_entries_that_passed(entered, tmp_path):
    ctx, cid, ids, root = entered
    with pytest.raises(ValueError, match="通過初審"):
        submission.build_full(ctx, cid, str(tmp_path / "full"))
    a, b = ids["DSC_0001.jpg"], ids["DSC_0002.jpg"]
    retouch.save_recipe(ctx, a, {"exposure": 0.3, "crop": {"left": 0.1, "top": 0, "right": 1, "bottom": 1}})
    submission.update_entry(ctx, cid, a, {"status": "passed", "use_edit": True})
    submission.update_entry(ctx, cid, b, {"status": "failed"})
    est = submission.estimate_full(ctx, cid)
    assert est["count"] == 1 and est["raws"] == 350000 and est["edits"] > 0 and est["bytes"] > est["originals"]
    out = submission.build_full(ctx, cid, str(tmp_path / "full"), make_zip=True)
    folder = Path(out["folder"])
    names = sorted(p.name for p in folder.iterdir())
    stem = "01_評語_%d" % a
    assert names == [f"{stem}_RAW.dng", f"{stem}_修圖.jpg", f"{stem}_原始檔.jpg", "作品說明.html"]
    assert (folder / f"{stem}_原始檔.jpg").read_bytes() == (root / "DSC_0001.jpg").read_bytes()  # byte for byte
    assert (folder / f"{stem}_RAW.dng").read_bytes() == (root / "DSC_0001.dng").read_bytes()
    edit = Image.open(folder / f"{stem}_修圖.jpg")
    assert edit.size == (2700, 2000) and edit.getexif().get_ifd(0x8769)[0x9003] == "2026:05:01 10:00:00"  # full size, cropped
    assert "Test Wide Gamut" in ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(edit.info["icc_profile"])))
    doc = (folder / "作品說明.html").read_text("utf-8")
    assert doc.count("<section") == 1 and "完整檔" in doc and "未合成" in doc
    with zipfile.ZipFile(out["zip"]) as z:
        assert len(z.namelist()) == 4 and all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
    assert abs(out["bytes"] - sum((folder / n).stat().st_size for n in names[:3])) < 10


@pytest.fixture
def client(ctx, library, fakes):
    app = create_app(ctx)
    with TestClient(app) as c:
        c.app_ctx, c.library, c.prov = ctx, library, fakes[1]
        yield c


def test_submission_over_http(client, tmp_path):
    analyse(client)
    cid = client.post("/api/contests", json={"name": "c", "theme": "t", "sizes": {"pool": 8, "calibration": 4, "triage": 0, "finals": 3, "picks": 2}}).json()["id"]
    client.post(f"/api/contests/{cid}/coach", json={"use_cloud": False})
    client.post(f"/api/contests/{cid}/local")
    wait(client)
    client.post(f"/api/contests/{cid}/finals")
    wait(client)
    s = client.get(f"/api/contests/{cid}/submission").json()
    assert len(s["entries"]) == 2 and s["spec"]["long_edge"] == 3000 and s["full"]["count"] == 0
    assert all("path" not in e and e["facts"] for e in s["entries"])
    pid = s["entries"][0]["photo_id"]
    qs = client.post(f"/api/contests/{cid}/submission/{pid}/questions").json()["questions"]
    qs[0]["a"] = qs[0]["options"][0]
    s = client.patch(f"/api/contests/{cid}/submission/{pid}", json={"questions": qs, "title": "新名稱"}).json()
    assert s["entries"][0]["title"] == "新名稱" and s["entries"][0]["questions"][0]["a"]
    s = client.post(f"/api/contests/{cid}/submission/copy", json={}).json()
    assert s["written"] == 2 and all(e["description"] for e in s["entries"])
    s = client.put(f"/api/contests/{cid}/submission", json={"spec": {"long_edge": 800, "author": "我"}}).json()
    assert s["spec"]["long_edge"] == 800 and s["spec"]["author"] == "我"

    assert client.post(f"/api/contests/{cid}/submission/package", json={"kind": "zip", "dest": str(tmp_path)}).status_code == 400
    client.post(f"/api/contests/{cid}/submission/package", json={"kind": "prelim", "dest": str(tmp_path / "p")})
    j = wait(client)
    assert j["status"] == "done" and j["result"]["count"] == 2 and Path(j["result"]["zip"]).exists()
    client.post(f"/api/contests/{cid}/submission/package", json={"kind": "full", "dest": str(tmp_path / "f")})
    assert wait(client)["status"] == "error"  # nothing has passed the first round yet
    client.patch(f"/api/contests/{cid}/submission/{pid}", json={"status": "passed"})
    client.post(f"/api/contests/{cid}/submission/package", json={"kind": "full", "dest": str(tmp_path / "f")})
    j = wait(client)
    assert j["status"] == "done" and j["result"]["count"] == 1 and j["result"]["zip"] is None
    s = client.get(f"/api/contests/{cid}/submission").json()
    assert [p["kind"] for p in s["packages"]] == ["prelim", "full"] and s["full"]["count"] == 1


def test_nothing_is_entered_before_the_jury_unless_asked(client):
    analyse(client)
    cid = client.post("/api/contests", json={"name": "c", "theme": "t", "sizes": {"pool": 8, "calibration": 4, "triage": 0, "finals": 3, "picks": 2}}).json()["id"]
    client.post(f"/api/contests/{cid}/coach", json={"use_cloud": False})
    client.post(f"/api/contests/{cid}/local")
    wait(client)
    s = client.get(f"/api/contests/{cid}/submission?seed=0").json()
    assert s["entries"] == [] and s["suggested"] == [] and s["limit"] == 2  # looking does not start a list
    assert s["checks"][0]["level"] == "error"
    s = client.get(f"/api/contests/{cid}/submission").json()  # asking does: the top of the on-device ranking
    top = [r["photo_id"] for r in client.get(f"/api/contests/{cid}/results").json()[:2]]
    assert [e["photo_id"] for e in s["entries"]] == top
    # once the jury has spoken, the panel can offer its picks instead
    client.post(f"/api/contests/{cid}/finals")
    wait(client)
    s = client.get(f"/api/contests/{cid}/submission?seed=0").json()
    assert len(s["suggested"]) == 2 and [e["photo_id"] for e in s["entries"]] == top  # the user's list is not replaced
    # only folders this contest packaged can be opened from the app
    assert client.post(f"/api/contests/{cid}/submission/reveal", json={"at": 123.0}).status_code == 404
    assert client.get("/api/contests/999/submission").status_code == 404


def test_saved_edit_can_be_previewed(client):
    analyse(client)
    pid = client.get("/api/photos?limit=1").json()["items"][0]["id"]
    plain = client.get(f"/api/edit/{pid}/render?r=saved").content
    client.put(f"/api/edit/{pid}", json={"recipe": {"exposure": 1.0}})
    edited = client.get(f"/api/edit/{pid}/render?r=saved")
    assert edited.status_code == 200 and edited.content != plain
    assert Image.open(io.BytesIO(edited.content)).convert("L").resize((1, 1)).getpixel((0, 0)) > \
        Image.open(io.BytesIO(plain)).convert("L").resize((1, 1)).getpixel((0, 0))
