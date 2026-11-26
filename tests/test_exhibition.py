from __future__ import annotations

import io
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageCms
from pptx import Presentation
from pptx.oxml.ns import qn

from photosift import deck, exhibition, imaging, pipeline
from photosift.jobs import Job
from photosift.server import create_app

from .conftest import make_photo
from .test_server import wait

SHOW = {**exhibition.DEFAULT_SPEC}  # the real brief: twenty works in 75 x 50 cm frames


# ---------------------------------------------------------------- printability


def test_how_a_photo_prints_in_a_75_by_50_frame():
    info = exhibition.print_info
    full = info(6000, 4000, None, SHOW)  # a 24 MP camera file
    assert (full["dpi"], full["level"], full["trim"], full["pixels"], full["orientation"]) == (203, "ok", 0.0, [6000, 4000], "landscape")
    phone = info(4032, 3024, None, SHOW)  # a 12 MP phone file is 4:3, so the frame cuts a ninth off its height
    assert (phone["dpi"], phone["level"], phone["trim"], phone["pixels"]) == (137, "soft", 0.111, [4032, 2688])
    assert info(3000, 2000, None, SHOW)["level"] == "soft" and info(2400, 1600, None, SHOW)["level"] == "low"
    upright = info(4000, 6000, None, SHOW)  # an upright photo hangs in an upright frame
    assert (upright["orientation"], upright["pixels"], upright["dpi"], upright["allowed"]) == ("portrait", [4000, 6000], 203, True)
    assert info(4000, 6000, None, {**SHOW, "orientation": "landscape"})["allowed"] is False
    assert info(6000, 4000, None, {**SHOW, "orientation": "portrait"})["allowed"] is False
    # a crop made in the editor is what gets printed, so it costs resolution
    cropped = info(6000, 4000, {"crop": {"left": 0.25, "top": 0.25, "right": 0.75, "bottom": 0.75}}, SHOW)
    assert (cropped["dpi"], cropped["level"], cropped["pixels"]) == (102, "soft", [3000, 2000])
    square = info(3000, 3000, None, SHOW)
    assert (square["pixels"], square["trim"], square["orientation"]) == ([3000, 2000], 0.333, "landscape")
    assert info(None, None, None, SHOW)["level"] == "low"
    spec = exhibition.clean_spec({"frame_w": 50, "frame_h": 75, "count": "x", "orientation": "sideways", "min_dpi": 5, "tone": "?"})
    assert (spec["frame_w"], spec["frame_h"], spec["count"], spec["orientation"], spec["min_dpi"], spec["tone"]) == (75, 50, 20, "both", 40, "詩意")


def test_cutting_a_picture_to_the_frame():
    def cut(w, h):
        return exhibition.to_frame(Image.new("RGB", (w, h)), SHOW).size

    assert cut(4000, 3000) == (4000, 2667) and cut(3000, 4000) == (2667, 4000)  # the long side is kept, the short side trimmed
    assert cut(6000, 3000) == (4500, 3000)  # a panorama loses its ends instead
    assert cut(6000, 4000) == (6000, 4000) and cut(4000, 6000) == (4000, 6000)
    im = Image.new("RGB", (400, 300), "white")
    im.paste("black", (0, 0, 400, 16))
    kept = exhibition.to_frame(im, SHOW)
    assert kept.size == (400, 267) and kept.getpixel((5, 0)) == (255, 255, 255)  # equal amounts come off the top and the bottom


# ------------------------------------------------------------------ a small show


@pytest.fixture
def show(ctx, tmp_path, fakes):
    """Fourteen usable photos from two trips, plus the kinds a show must leave out. Frames are small so the files can be too."""
    root = tmp_path / "work"
    t = datetime(2026, 4, 3, 9, 0)
    for i in range(8):
        make_photo(root / f"K{i}.jpg", 10 + i, t + timedelta(minutes=11 * i), (35.01, 135.68))
    make_photo(root / "K8.jpg", 10, t + timedelta(minutes=1, seconds=2), (35.01, 135.68), variant=2)  # second frame of a burst
    make_photo(root / "KX.jpg", 30, t + timedelta(hours=3), (35.01, 135.68), blur=8)  # blurred
    make_photo(root / "KS.jpg", 31, t + timedelta(hours=4), (35.01, 135.68), size=(420, 280))  # too few pixels to print
    t2 = datetime(2026, 9, 10, 15, 0)
    for i in range(4):
        make_photo(root / f"T{i}.jpg", 50 + i, t2 + timedelta(minutes=13 * i), (25.03, 121.56))
    make_photo(root / "T4.jpg", 60, t2 + timedelta(hours=2), (25.03, 121.56), size=(800, 600))  # 4:3
    make_photo(root / "T5.jpg", 61, t2 + timedelta(hours=3), (25.03, 121.56), size=(600, 900))  # upright
    ctx.db.run("INSERT INTO folders(path, added_at) VALUES (?,?)", (str(root), time.time()))
    pipeline.analyze(ctx, Job(1, "a", "t"))
    ctx.db.run("UPDATE photos SET category=CASE WHEN filename LIKE 'K%' THEN 'landscape' ELSE 'city' END")
    ids = {r["filename"]: r["id"] for r in ctx.db.all("SELECT id, filename FROM photos")}
    xid = exhibition.create(ctx, "2026 員工攝影展", {"count": 6, "frame_w": 15, "frame_h": 10, "pool": 10, "reserves": 1, "desc_max": 60})
    return ctx, xid, ids, root


def test_only_photos_that_can_hang_are_considered(show):
    ctx, xid, ids, _ = show
    spec = exhibition.get(ctx, xid)["spec"]
    ok = {p["filename"] for p in exhibition.eligible(ctx, spec)}
    assert "KX.jpg" not in ok and "KS.jpg" not in ok  # blurred, and too small for the frame
    assert len({"K0.jpg", "K8.jpg"} & ok) == 1 and len(ok) == 14  # one frame of the burst
    assert {"T4.jpg", "T5.jpg"} <= ok
    assert "T5.jpg" not in {p["filename"] for p in exhibition.eligible(ctx, {**spec, "orientation": "landscape"})}


def test_themes_are_proposed_from_the_library_itself(show, fakes):
    ctx, xid, ids, _ = show
    vlm, prov = fakes
    calls = vlm.calls
    ideas = exhibition.propose(ctx, xid, use_model=False)
    assert vlm.calls == calls and not prov.calls  # grouping is arithmetic
    kinds = {i["kind"] for i in ideas}
    assert "place" in kinds and 2 <= len(ideas) <= 6
    assert "subject" not in kinds  # every Kyoto photo is also a landscape here: the same photos are one idea, not two
    top = ideas[0]
    assert top["enough"] and top["count"] >= 6 and 6 <= len(top["cover"]) <= 8 and top["title"] == top["hint"] and top["named_by"] is None
    assert all(i["count"] >= 4 and i["kind_zh"] for i in ideas)  # an idea needs enough photos behind it to be offered
    assert all(set(i["photo_ids"]) <= set(ids.values()) and ids["KX.jpg"] not in i["photo_ids"] for i in ideas)
    tops = [frozenset(i["photo_ids"][:6]) for i in ideas]
    assert len(set(tops)) == len(tops)  # two ideas that pick the same photos are offered once
    # the on-device model only names them: one look at nine photos each
    named = exhibition.propose(ctx, xid)
    assert vlm.calls == calls + len(named) and not prov.calls and all(i["named_by"] == "fake-vlm" and i["title"] == "測試活動" for i in named)
    s = exhibition.state(ctx, xid)
    assert s["ideas"]["eligible"] == 14 and "photo_ids" not in s["ideas"]["ideas"][0] and s["theme"] is None and s["checks"][0]["level"] == "error"


def test_choosing_a_theme_sets_the_candidates(show, fakes):
    ctx, xid, ids, _ = show
    ideas = exhibition.propose(ctx, xid, use_model=False)
    idea = max(ideas, key=lambda i: i["count"])
    theme = exhibition.choose(ctx, xid, key=idea["key"])
    assert theme["title"] == idea["title"] and theme["photo_ids"] == idea["photo_ids"] and len(theme["photo_ids"]) >= 6
    x = exhibition.get(ctx, xid)
    first = [p["id"] for p in exhibition.pool(ctx, x)]
    assert first == theme["photo_ids"]
    exhibition.set_pool(ctx, xid, excluded=[first[0]], pinned=[first[-1]])
    now = [p["id"] for p in exhibition.pool(ctx, exhibition.get(ctx, xid))]
    assert now[0] == first[-1] and first[0] not in now and len(now) == len(first) - 1  # pinned first, excluded gone
    with pytest.raises(ValueError, match="找不到"):
        exhibition.choose(ctx, xid, key="nope")
    # the user's own words work too, matched against every eligible photo
    own = exhibition.choose(ctx, xid, text="安靜的水面")
    assert own["kind"] == "custom" and own["title"] == "安靜的水面" and len(own["photo_ids"]) == 10  # as many as the curator is shown
    with pytest.raises(ValueError, match="請輸入"):
        exhibition.choose(ctx, xid, text="  ")


def test_like_some_of_the_chosen_photos_not_their_average():
    import numpy as np

    def unit(*v):
        a = np.array(v, dtype=float)
        return a / np.linalg.norm(a)

    two = np.stack([unit(1, 0, 0), unit(0, 1, 0)])  # the user chose two quite different pictures
    cands = np.stack([unit(1, 0.1, 0), unit(1, 1, 0), unit(0, 0, 1), unit(0.1, 1, 0)])
    near_a, between, unrelated, near_b = exhibition.seed_fit(cands, two)
    assert near_a > between > unrelated and near_b > between and abs(near_a - near_b) < 1e-9
    # with more examples one odd choice no longer decides: a candidate has to be like two of them
    five = np.stack([unit(1, 0, 0), unit(1, 0.05, 0), unit(1, 0, 0.05), unit(0, 1, 0), unit(0, 1, 0.05)])
    like_three, like_two, like_none = exhibition.seed_fit(np.stack([unit(1, 0.02, 0), unit(0.02, 1, 0), unit(0, 0, 1)]), five)
    assert abs(like_three - like_two) < 0.01 and like_none < 0.1
    lone = np.stack([*five[:4], unit(0, 0, 1)])  # one outlier among the examples pulls in nothing on its own
    assert exhibition.seed_fit(np.stack([unit(0, 0.02, 1)]), lone)[0] < 0.6


def test_a_theme_from_a_few_photos_the_user_chose(show, fakes):
    ctx, xid, ids, _ = show
    vlm, prov = fakes
    seeds = [ids["K2.jpg"], ids["K5.jpg"]]
    calls = vlm.calls
    theme = exhibition.choose(ctx, xid, seeds=seeds, use_model=False)
    assert vlm.calls == calls and not prov.calls  # similarity alone needs no model
    assert (theme["kind"], theme["title"], theme["pinned"], theme["photo_ids"][:2]) == ("seeds", "我預選的系列", seeds, seeds)
    rest = theme["photo_ids"][2:]
    spec = exhibition.get(ctx, xid)["spec"]
    ok = {p["id"] for p in exhibition.eligible(ctx, spec)}
    assert len(theme["photo_ids"]) == 10 and len(set(theme["photo_ids"])) == 10 and set(rest) <= ok - set(seeds)
    s = exhibition.state(ctx, xid)
    assert [p["pinned"] for p in s["pool"]] == [True, True] + [False] * 8 and s["theme"]["kind"] == "seeds"
    # the on-device model reads the chosen photos: one look, a name, and descriptions of what else would belong
    seen = {}

    def reads(system, user, images=None, schema=None, **kw):
        seen.update(user=user, images=len(images or []))
        return {"title": "靜水", "pitch": "停下來的時刻", "thread": "冷色調與水平線", "probes": [{"zh": "安靜的水面", "en": "calm still water"}, {"zh": "", "en": ""}]}

    vlm.chat_json = reads
    named = exhibition.choose(ctx, xid, seeds=seeds)
    assert seen["images"] == 1 and "我想要的就是這種感覺" in seen["user"] and "展出 6 張作品" in seen["user"]
    assert (named["title"], named["pitch"], named["probes"]) == ("靜水", "停下來的時刻", ["安靜的水面"]) and named["photo_ids"][:2] == seeds
    assert len(named["photo_ids"]) == 10 and not prov.calls
    # a photo the user insists on stays in, even one that will not print well; that is shown, not hidden
    small = exhibition.choose(ctx, xid, seeds=[ids["KS.jpg"], ids["K2.jpg"], ids["K2.jpg"], 99999], use_model=False)
    assert small["pinned"] == [ids["KS.jpg"], ids["K2.jpg"]]
    assert exhibition.state(ctx, xid)["pool"][0]["print"]["level"] == "low"
    with pytest.raises(ValueError, match="請先選"):
        exhibition.choose(ctx, xid, seeds=[99999], use_model=False)


def test_the_curator_is_told_which_photos_are_the_users_own(show, fakes):
    ctx, xid, ids, _ = show
    vlm, prov = fakes
    seeds = [ids["K2.jpg"], ids["K5.jpg"], ids["T1.jpg"]]
    exhibition.choose(ctx, xid, seeds=seeds, use_model=False)
    exhibition.curate(ctx, xid, "cloud")
    sent = prov.calls[-1]["text"]
    assert sent.count("｜攝影者預選") == 3 and "標示「攝影者預選」的 3 張是他自己挑出來定義這個系列的" in sent and "請保留" in sent
    lines = [ln for ln in sent.split("\n") if ln.endswith("攝影者預選")]
    assert [ln.split(".")[0] for ln in lines] == ["1", "2", "3"]  # and they come first on the sheet
    exhibition.choose(ctx, xid, text="旅途")
    exhibition.curate(ctx, xid, "cloud")
    assert "攝影者預選" not in prov.calls[-1]["text"]


def test_starting_points_offer_library_picks_first(show):
    ctx, xid, ids, _ = show
    ctx.db.run("UPDATE photos SET flag='pick' WHERE filename IN ('T3.jpg', 'KS.jpg')")
    ctx.db.run("UPDATE photos SET rating=5 WHERE filename='K4.jpg'")
    got = exhibition.choices(ctx, xid)
    assert {got[0]["photo_id"], got[1]["photo_id"]} == {ids["T3.jpg"], ids["KS.jpg"]} and got[0]["picked"] and got[1]["picked"]
    assert got[2]["photo_id"] == ids["K4.jpg"] and not got[2]["picked"]  # then what was rated, then the rest by quality
    small = next(p for p in got if p["photo_id"] == ids["KS.jpg"])
    assert small["print"]["level"] == "low"  # offered because the user picked it, with the problem visible
    assert ids["KX.jpg"] not in [p["photo_id"] for p in got] and len(got) == 15


@pytest.fixture
def curated(show, fakes):
    ctx, xid, ids, root = show
    exhibition.propose(ctx, xid, use_model=False)
    exhibition.choose(ctx, xid, text="旅途")
    out = exhibition.outgoing(ctx, xid)
    assert (len(out["photo_ids"]), out["sheets"], out["count"]) == (10, 1, 6) and out["est_tokens"] > 0 and not fakes[1].calls
    summary = exhibition.curate(ctx, xid, "cloud")
    return ctx, xid, ids, root, summary, out["photo_ids"]


def test_the_curator_sees_contact_sheets_and_its_answer_is_repaired(curated, fakes):
    ctx, xid, ids, _, summary, shown = curated
    vlm, prov = fakes
    assert len(prov.calls) == 1 and prov.calls[0]["images"] == 1  # ten candidates, one sheet
    sent = prov.calls[0]["text"]
    assert "選出剛好 6 張" in sent and "15×10 公分" in sent and "每個章節另外選最多 1 張候補" in sent and "dpi" in sent
    assert "不是挑出最好的單張" in exhibition.CURATOR_SYSTEM
    call = dict(ctx.db.one("SELECT purpose, images, photos FROM cloud_calls ORDER BY id DESC LIMIT 1"))
    assert call == {"purpose": "exhibit_curate", "images": 1, "photos": 10}
    s = exhibition.state(ctx, xid)
    cur = s["curation"]
    assert (cur["title"], [c["title"] for c in cur["chapters"]], cur["gaps"]) == ("靜水流年", ["開場", "收尾"], ["少一張收尾的大景"])
    # the stand-in curator chose five, repeated one and named a photo that was not on the sheet
    assert summary["works"] == 6 and len(s["works"]) == 6 and len({w["photo_id"] for w in s["works"]}) == 6
    assert [w["no"] for w in s["works"]] == [1, 2, 3, 4, 5, 6]
    assert [w["chapter"] for w in s["works"]] == sorted(w["chapter"] for w in s["works"])  # a chapter hangs together
    assert sum("依地端排名補上" in w["why"] for w in s["works"]) == 1  # the missing sixth is filled in, and says so
    assert len(s["reserves"]) == 1 and s["reserves"][0]["chapter"] == 0  # one per chapter was the limit
    assert len(cur["weak"]) == 1 and cur["weak"][0]["photo_id"] in {w["photo_id"] for w in s["works"]}
    assert {w["photo_id"] for w in s["works"]} | {r["photo_id"] for r in s["reserves"]} <= set(shown)
    # curating again keeps the words already written for photos that stay
    keep = s["works"][0]["photo_id"]
    exhibition.update_work(ctx, xid, keep, {"title": "我取的名字", "description": "我寫的"})
    exhibition.curate(ctx, xid, "local")  # and it can be done on-device
    again = exhibition.state(ctx, xid)
    kept = next(w for w in again["works"] + again["reserves"] if w["photo_id"] == keep)
    assert kept["title"] == "我取的名字" and again["curation"]["provider"] == "local" and len(again["works"]) == 6 and len(prov.calls) == 1


def test_rearranging_the_wall_and_using_reserves(curated):
    ctx, xid, *_ = curated

    def wall():
        s = exhibition.state(ctx, xid)
        return [(w["photo_id"], w["chapter"]) for w in s["works"]], [r["photo_id"] for r in s["reserves"]]

    seq, reserves = wall()
    a, b = seq[0], seq[1]
    assert a[1] == b[1] == 0
    exhibition.move(ctx, xid, a[0], 1)  # within a chapter two works change places
    assert wall()[0][:2] == [b, a]
    exhibition.move(ctx, xid, a[0], -1)
    assert wall()[0] == seq
    last_of_first = [p for p, ch in seq if ch == 0][-1]
    exhibition.move(ctx, xid, last_of_first, 1)  # at the edge it joins the next chapter rather than leaping over a work
    now, _ = wall()
    assert [p for p, _ in now] == [p for p, _ in seq] and dict(now)[last_of_first] == 1
    exhibition.move(ctx, xid, seq[0][0], -1)  # already first: nothing happens
    assert wall()[0][0] == seq[0]
    # a reserve steps in exactly where the work was
    out_id = seq[1][0]
    exhibition.swap(ctx, xid, out_id, reserves[0])
    now, res = wall()
    assert now[1] == (reserves[0], seq[1][1]) and res == [out_id] and len(now) == 6
    with pytest.raises(ValueError, match="候補"):
        exhibition.swap(ctx, xid, out_id, reserves[0])  # roles are now the other way round
    exhibition.set_role(ctx, xid, now[0][0], "reserve")
    assert len(wall()[0]) == 5 and len(wall()[1]) == 2
    assert any("需要剛好 6 張" in c["text"] for c in exhibition.state(ctx, xid)["checks"])
    exhibition.set_role(ctx, xid, now[0][0], "work")
    exhibition.set_role(ctx, xid, out_id, "none")
    assert len(wall()[0]) == 6 and wall()[1] == []


def test_words_are_written_from_text_alone(curated, fakes):
    ctx, xid, *_ = curated
    vlm, prov = fakes
    s = exhibition.state(ctx, xid)
    pid = s["works"][0]["photo_id"]
    qs = exhibition.ask_background(ctx, xid, pid)  # the on-device model looks at the work and asks
    assert len(qs) == 2 and len(prov.calls) == 1
    qs[0]["a"] = "那天只有我一個人"
    exhibition.update_work(ctx, xid, pid, {"questions": qs, "title": "已經有名字"})
    out = exhibition.write_copy(ctx, xid)
    sent = prov.calls[-1]
    assert sent["images"] == 0 and out["written"] == 7 and out["tokens"] > 0  # works and the reserve, no photo uploaded
    assert "那天只有我一個人" in sent["text"] and "策展人選它的理由" in sent["text"] and "所屬章節：開場" in sent["text"] and "60 個字以內" in sent["text"]
    s = exhibition.state(ctx, xid)
    assert all(w["description"] and len(w["description"]) <= 60 and w["title"] for w in s["works"] + s["reserves"])
    assert s["works"][0]["title"] == "已經有名字" and s["works"][1]["title"].startswith("名稱")
    n = len(prov.calls)
    assert exhibition.write_copy(ctx, xid, only_empty=True)["written"] == 0 and len(prov.calls) == n
    assert ctx.db.val("SELECT COUNT(*) FROM cloud_calls WHERE purpose='exhibit_copy' AND images=0") == 1


def test_the_biography_comes_from_an_interview_not_from_imagination(curated, fakes):
    ctx, xid, *_ = curated
    vlm, prov = fakes
    qs = exhibition.ask_profile(ctx, xid)
    assert len(qs) == 2 and all(q["a"] == "" for q in qs)
    blank = exhibition.write_profile(ctx, xid)
    assert blank["answered"] == 0 and "沒有回答" in blank["statement"]
    assert "攝影者沒有回答任何問題" in prov.calls[-1]["text"] and prov.calls[-1]["images"] == 0
    assert "不可以編造" in exhibition.PROFILE_SYSTEM and "職業" in exhibition.PROFILE_SYSTEM
    qs[0]["a"] = "大學時借了朋友的相機"
    exhibition.set_profile(ctx, xid, {"questions": qs})
    exhibition.update(ctx, xid, spec={"author": "王小明"})
    out = exhibition.write_profile(ctx, xid)
    sent = prov.calls[-1]["text"]
    assert out["answered"] == 1 and "有回答" in out["statement"] and "大學時借了朋友的相機" in sent and "攝影者姓名：王小明" in sent
    assert "展出 6 張作品" in sent and "拍攝於 2026-" in sent and "器材：TestCam T-1" in sent  # what the files themselves say
    p = exhibition.set_profile(ctx, xid, {"bio": "我自己改的簡介"})
    assert p["bio"] == "我自己改的簡介" and p["statement"] == out["statement"]
    local = exhibition.write_profile(ctx, xid, use_cloud=False)
    assert local["tokens"] == 0 and local["model"] == "fake-vlm"


@pytest.fixture
def ready(curated, fakes):
    ctx, xid, ids, root, _, _ = curated
    exhibition.update(ctx, xid, spec={"author": "王小明"})
    exhibition.write_copy(ctx, xid)
    exhibition.set_profile(ctx, xid, {"bio": "王小明的簡介。", "statement": "我的創作自述。"})
    return ctx, xid, ids, root


def test_checks_before_handing_in(curated, ready):
    ctx, xid, ids, _ = ready
    s = exhibition.state(ctx, xid)
    levels = {c["level"] for c in s["checks"]}
    assert "error" not in levels
    odd = [w for w in s["works"] if w["print"]["trim"] > 0.01]
    assert all(any(f"第 {w['no']} 張" in c["text"] and "置中裁掉" in c["text"] for c in s["checks"]) for w in odd)
    pid = s["works"][0]["photo_id"]
    exhibition.update_work(ctx, xid, pid, {"description": "字" * 61, "title": s["works"][1]["title"]})
    exhibition.update(ctx, xid, spec={"author": ""})
    texts = [c["text"] for c in exhibition.state(ctx, xid)["checks"]]
    assert any("超過 60 字" in t for t in texts) and any("創作者姓名" in t for t in texts) and any("都叫" in t for t in texts)
    exhibition.update(ctx, xid, spec={"author": "王小明", "frame_w": 75, "frame_h": 50})  # the real frame: these test files are far too small
    assert sum("會明顯模糊" in c["text"] for c in exhibition.state(ctx, xid)["checks"]) == 6


def test_the_hand_in_folder(ready, tmp_path):
    ctx, xid, ids, root = ready
    before = {p: p.read_bytes() for p in root.iterdir()}
    on_wall = [w["photo_id"] for w in exhibition.state(ctx, xid)["works"]]
    if ids["T4.jpg"] not in on_wall:  # make sure the 4:3 photo hangs, to see it cut to the frame
        exhibition.set_role(ctx, xid, on_wall[-1], "none")
        exhibition.set_role(ctx, xid, ids["T4.jpg"], "work")
    exhibition.update_work(ctx, xid, ids["T4.jpg"], {"title": "四比三", "description": "會被裁成畫框的比例。"})
    no = next(w["no"] for w in exhibition.state(ctx, xid)["works"] if w["photo_id"] == ids["T4.jpg"])
    out = exhibition.build(ctx, xid, str(tmp_path / "out"), make_zip=True)
    folder = Path(out["folder"])
    assert folder.name == "2026_員工攝影展_展覽交件" and out["count"] == 6
    files = sorted(p.name for p in (folder / "作品").iterdir())
    assert len(files) == 6 and [f[:3] for f in files] == ["01_", "02_", "03_", "04_", "05_", "06_"] and f"{no:02d}_四比三.jpg" in files
    cut = Image.open(folder / "作品" / f"{no:02d}_四比三.jpg")
    assert cut.size == (800, 533)  # the frame's shape, at the photo's own resolution
    first = Image.open(folder / "作品" / files[0])
    assert ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(first.info["icc_profile"]))).startswith("sRGB")
    assert first.getexif().get_ifd(0x8769)[0x9003].startswith("2026:")  # capture information is kept
    # the caption cards: a cover, the biography, a divider per chapter, then one slide per work
    prs = Presentation(str(folder / out["pptx"]))
    s = exhibition.state(ctx, xid)
    titles = [sl.shapes.title.text_frame.text for sl in prs.slides]
    assert len(prs.slides) == 2 + 2 + 6 and titles[0] == "靜水流年" and titles[1] == "個人簡介" and titles[2] == "開場"
    assert [t for t in titles if t in {w["title"] for w in s["works"]}] == [w["title"] for w in s["works"]]  # in hanging order
    slide = next(sl for sl in prs.slides if sl.shapes.title.text_frame.text == "四比三")
    card = "\n".join(sh.text_frame.text for sh in slide.shapes if sh.has_text_frame)
    assert "四比三" in card and "王小明" in card and "15×10 公分（800×533 像素）" in card and "會被裁成畫框的比例。" in card and "TestCam T-1" in card
    bio = "\n".join(sh.text_frame.text for sh in prs.slides[1].shapes if sh.has_text_frame)
    assert "王小明的簡介。" in bio and "我的創作自述。" in bio
    text = (folder / "個人簡介與作品清單.txt").read_text("utf-8")
    assert "【個人簡介】" in text and f"{no:02d}　四比三" in text
    with zipfile.ZipFile(out["zip"]) as z:
        assert len(z.namelist()) == 8 and all(n.startswith(folder.name + "/") for n in z.namelist())
    assert {p: p.read_bytes() for p in root.iterdir()} == before  # originals untouched
    assert exhibition.state(ctx, xid)["packages"][0]["exists"] and exhibition.package_folder(ctx, xid, out["at"]) == folder
    assert Path(exhibition.build(ctx, xid, str(tmp_path / "out"))["folder"]).name.endswith("_2")  # a second run does not overwrite


def test_the_deck_is_ordinary_editable_slides(tmp_path):
    wide, tall = Image.new("RGB", (900, 600), (40, 80, 120)), Image.new("RGB", (600, 900), (120, 80, 40))
    slides = [{"no": i + 1, "title": f"作品{i + 1}", "gear": "TestCam T-1", "author": "王小明", "size": "75×50 公分（6000×4000 像素）",
               "description": "說明" * 20, "chapter": 0, "image": imaging.jpeg_bytes(tall if i == 1 else wide, 80)} for i in range(3)]  # fmt: skip
    path = tmp_path / "d.pptx"
    deck.build(path, exhibition="攝影展", series="系列", author="王小明", statement="自述", bio="簡介", chapters=[{"title": "唯一", "idea": ""}], slides=slides)
    prs = Presentation(str(path))
    assert len(prs.slides) == 2 + 3  # a single chapter needs no divider
    for sl in prs.slides:
        assert sl.shapes.title.text_frame.text  # every slide has a real title
        for sh in sl.shapes:
            assert sh.left >= 0 and sh.top >= 0 and sh.left + sh.width <= prs.slide_width + 1 and sh.top + sh.height <= prs.slide_height + 1
    work = prs.slides[3]  # the upright one
    pic = next(sh for sh in work.shapes if sh.shape_type == 13)
    assert pic.height > pic.width and pic._element.xpath("./p:nvPicPr/p:cNvPr")[0].get("descr") == "作品2"
    runs = [r for sh in work.shapes if sh.has_text_frame for p in sh.text_frame.paragraphs for r in p.runs]
    assert runs and all(r._r.rPr.find(qn("a:ea")).get("typeface") == "Microsoft JhengHei" and r.font.name == "Calibri" for r in runs)
    assert all(r.font.size.pt >= 10 for r in runs)


# ------------------------------------------------------------------- over http


def test_exhibition_over_http(show, fakes, tmp_path):
    ctx, _, ids, _ = show
    with TestClient(create_app(ctx)) as client:
        made = client.post("/api/exhibitions", json={"name": "我的個展", "spec": {"count": 5, "frame_w": 15, "frame_h": 10, "pool": 8, "reserves": 1}}).json()
        xid = made["id"]
        assert made["spec"]["count"] == 5 and made["ideas"] is None and len(client.get("/api/exhibitions").json()) == 2
        assert client.get("/api/exhibitions/999").status_code == 404
        client.post(f"/api/exhibitions/{xid}/propose")
        j = wait(client)
        assert j["status"] == "done" and j["result"]["ideas"] >= 2
        s = client.get(f"/api/exhibitions/{xid}").json()
        key = s["ideas"]["ideas"][0]["key"]
        s = client.post(f"/api/exhibitions/{xid}/theme", json={"key": key}).json()
        assert s["theme"]["key"] == key and len(s["pool"]) >= 5 and "print" in s["pool"][0]
        assert client.post(f"/api/exhibitions/{xid}/theme", json={"key": "nope"}).status_code == 400
        offered = client.get(f"/api/exhibitions/{xid}/choices").json()
        mine = [offered[0]["photo_id"], offered[3]["photo_id"]]
        by_example = client.post(f"/api/exhibitions/{xid}/theme", json={"seeds": mine}).json()
        assert by_example["theme"]["kind"] == "seeds" and by_example["theme"]["pinned"] == mine
        assert [p["photo_id"] for p in by_example["pool"]][:2] == mine and by_example["pool"][0]["pinned"]
        s = client.post(f"/api/exhibitions/{xid}/theme", json={"key": key}).json()
        drop = s["pool"][-1]["photo_id"]
        s = client.put(f"/api/exhibitions/{xid}/pool", json={"excluded": [drop]}).json()
        assert drop not in [p["photo_id"] for p in s["pool"]]
        out = client.get(f"/api/exhibitions/{xid}/outgoing").json()
        assert out["provider"] == "Gemini" and out["sheets"] == 1 and out["count"] == 5
        assert client.post(f"/api/exhibitions/{xid}/curate", json={"mode": "psychic"}).status_code == 400
        client.post(f"/api/exhibitions/{xid}/curate", json={"mode": "cloud"})
        j = wait(client)
        assert j["status"] == "done" and j["result"]["works"] == 5 and j["result"]["title"] == "靜水流年"
        s = client.get(f"/api/exhibitions/{xid}").json()
        a, b = s["works"][0]["photo_id"], s["works"][1]["photo_id"]
        s = client.post(f"/api/exhibitions/{xid}/works/{a}/arrange", json={"by": 1}).json()
        assert [w["photo_id"] for w in s["works"][:2]] == [b, a]
        if s["reserves"]:
            r = s["reserves"][0]["photo_id"]
            s = client.post(f"/api/exhibitions/{xid}/works/{a}/arrange", json={"swap_with": r}).json()
            assert r in [w["photo_id"] for w in s["works"]] and a in [w["photo_id"] for w in s["reserves"]]
        first = s["works"][0]["photo_id"]
        qs = client.post(f"/api/exhibitions/{xid}/works/{first}/questions").json()["questions"]
        s = client.patch(f"/api/exhibitions/{xid}/works/{first}", json={"title": "第一張", "questions": qs}).json()
        assert s["works"][0]["title"] == "第一張" and len(s["works"][0]["questions"]) == 2
        s = client.post(f"/api/exhibitions/{xid}/copy", json={}).json()
        assert s["written"] >= 5 and all(w["description"] for w in s["works"])
        s = client.post(f"/api/exhibitions/{xid}/profile/questions").json()
        assert len(s["profile"]["questions"]) == 2
        s = client.post(f"/api/exhibitions/{xid}/profile/write", json={}).json()
        assert s["written"]["bio"] and s["profile"]["bio"] == s["written"]["bio"]
        s = client.put(f"/api/exhibitions/{xid}/profile", json={"bio": "改過的"}).json()
        assert s["profile"]["bio"] == "改過的"
        s = client.put(f"/api/exhibitions/{xid}", json={"name": "改名的個展", "spec": {"author": "王小明"}}).json()
        assert s["name"] == "改名的個展" and s["spec"]["author"] == "王小明" and s["spec"]["count"] == 5
        client.post(f"/api/exhibitions/{xid}/package", json={"dest": str(tmp_path / "x")})
        j = wait(client)
        assert j["status"] == "done" and j["result"]["count"] == 5 and (Path(j["result"]["folder"]) / j["result"]["pptx"]).exists()
        assert client.post(f"/api/exhibitions/{xid}/reveal", json={"at": 1.0}).status_code == 404
        assert client.delete(f"/api/exhibitions/{xid}").json() == {"ok": True} and client.get(f"/api/exhibitions/{xid}").status_code == 404
