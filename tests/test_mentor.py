from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photosift import geocode, imaging, ingest, mentor, pipeline, retouch, shooting, shootplan, sun
from photosift.jobs import Job
from photosift.server import create_app

from .conftest import make_photo
from .test_server import analyse, wait

KYOTO = (35.01, 135.68)


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# ------------------------------------------------------------------- the sun


def test_light_timetable_matches_an_almanac():
    d = sun.day_events("2026-04-03", *KYOTO, 540)  # Kyoto, early April: sunrise 05:42, sunset 18:20, sun due south at noon
    assert abs(d["sunrise"] - minutes("05:42")) <= 3 and abs(d["sunset"] - minutes("18:20")) <= 3
    assert abs(d["noon"] - minutes("12:01")) <= 3 and d["noon_az"] == 180 and 58 < d["noon_elev"] < 62
    assert 80 <= d["sunrise_az"] <= 86 and 274 <= d["sunset_az"] <= 280  # a little north of east and west in spring
    # the windows come in order and the golden hour straddles sunrise and sunset
    mb, mg, eg, eb = d["morning_blue"], d["morning_golden"], d["evening_golden"], d["evening_blue"]
    assert mb[0] < mb[1] == mg[0] < d["sunrise"] < mg[1] < d["harsh"][0] < d["harsh"][1] < eg[0] < d["sunset"] < eg[1] == eb[0] < eb[1]
    assert 40 <= mg[1] - mg[0] <= 60 and 8 <= eb[1] - eb[0] <= 14
    assert (sun.hhmm(d["sunrise"]), sun.hhmm(None), sun.compass(247), sun.compass(359)) == ("05:42", None, "西南", "北")


def test_light_timetable_at_the_edges_of_the_map():
    south = sun.day_events("2026-12-21", -33.87, 151.21, 660)  # Sydney at midsummer: the noon sun is in the north
    assert south["noon_az"] in (0, 360, 359, 1) and south["noon_elev"] > 78 and 110 < south["sunrise_az"] < 125
    north = sun.day_events("2026-06-21", 64.15, -21.94, 0)  # Reykjavik at midsummer: golden light runs past midnight
    assert north["evening_golden"] and north["evening_golden"][1] == 1440 and north["evening_blue"] is None
    polar = sun.day_events("2026-12-21", 78.2, 15.6, 60)  # Svalbard in the polar night
    assert polar["sunrise"] is None and polar["morning_golden"] is None and polar["harsh"] is None
    assert sun.day_events("2026-04-03", *KYOTO)["tz_exact"] is False  # guessed from longitude when the zone is unknown


def test_where_the_light_comes_from():
    assert sun.relative_light(250, 250)[0] == "逆光" and sun.relative_light(250, 70)[0] == "順光"
    assert sun.relative_light(250, 160) == ("側光", "太陽在你的右手邊") and sun.relative_light(250, 310) == ("側逆光", "太陽在左前方")
    elev, az = sun.position(datetime(2026, 4, 3, 8, 0).timestamp() - time.timezone - 9 * 3600 + time.timezone, *KYOTO)
    assert 25 < elev < 32 and 95 < az < 110  # eight in the morning: low in the east-south-east


# ------------------------------------------------------------------ settings


def test_changing_one_setting_keeps_the_exposure():
    cases = [
        dict(t_new=1 / 125, n_min=1.8), dict(t_new=1 / 500), dict(n_new=8.0, t_slowest=1 / 40, open_first=False),
        dict(n_new=2.0), dict(iso_new=100, t_slowest=1 / 30), dict(n_new=11.0),
    ]  # fmt: skip
    for n, t, iso in ((4.0, 1 / 30, 200), (2.8, 1 / 2000, 100), (8.0, 1 / 250, 800), (16.0, 1 / 60, 1600)):
        before = shooting.ev100(n, t, iso)
        for kw in cases:
            n2, t2, iso2, short = shooting.rebalance(n, t, iso, **kw)
            after = shooting.ev100(n2, t2, iso2)
            assert abs(after - before - short) < 0.4, (n, t, iso, kw)  # within a third of a stop, apart from what could not be made up
            assert n2 in shooting.APERTURES and iso2 in shooting.ISOS  # values a camera can actually be set to
    # the lens cannot open further and the ISO is capped: the missing light is reported, not hidden
    n2, t2, iso2, short = shooting.rebalance(2.8, 1 / 15, 3200, t_new=1 / 125, n_min=2.8)
    assert (n2, iso2) == (2.8, 6400) and short == pytest.approx(2.1, abs=0.15)
    assert shooting.safe_shutter(85) == 1 / 80 and shooting.safe_shutter(16) == 1 / 30 and shooting.safe_shutter(35, "animal") == 1 / 500
    assert shooting.fmt(8, 1 / 250, 100) == "1/250s　f/8　ISO 100" and shooting.fmt_shutter(2) == "2 秒"
    assert shooting.scene_light(shooting.ev100(16, 1 / 100, 100)) == "晴天的直射陽光"


BASE = dict(camera="SONY ILCE-7M4", lens="FE 35mm F1.8", width=6000, height=4000, flags=[], sharp=70, focal=35, focal35=35)


def keys(d):
    return [f["key"] for f in d["findings"]]


def test_blur_from_a_slow_shutter_gets_settings_that_fix_it():
    p = {**BASE, "focal": 85, "focal35": 85, "aperture": 4, "shutter_s": 1 / 30, "iso": 200, "category": "portrait", "sharp": 8,
         "exp_program": "auto"}  # fmt: skip
    d = shooting.diagnose(p, lens_max=1.8)
    assert keys(d) == ["slow_shutter", "auto_mode"] and d["findings"][0]["level"] == "fix"
    assert (d["now"], d["next"], d["shortfall"]) == ("1/30s　f/4　ISO 200", "1/125s　f/2　ISO 200", 0.0)
    assert "1/125s" in d["findings"][0]["detail"] and "光圈開到 f/2" in d["findings"][0]["detail"]
    # without knowing the lens can open up, the light comes from ISO instead
    assert shooting.diagnose(p)["next"] == "1/125s　f/4　ISO 800"
    # a landscape keeps its aperture and raises ISO; what cannot be made up is said out loud
    dim = shooting.diagnose({**BASE, "aperture": 2.8, "shutter_s": 1 / 8, "iso": 3200, "category": "landscape", "sharp": 9}, lens_max=2.8)
    assert dim["next"] == "1/40s　f/2.8　ISO 6400" and dim["shortfall"] > 1 and "腳架" in dim["findings"][0]["detail"]
    # sharp at a slow speed is praised, not corrected
    steady = shooting.diagnose({**BASE, "aperture": 8, "shutter_s": 1 / 8, "iso": 100, "category": "city", "sharp": 80})
    assert "steady" in keys(steady) and steady["next"] is None


def test_several_problems_give_one_suggestion():
    d = shooting.diagnose({**BASE, "focal": 24, "focal35": 24, "aperture": 22, "shutter_s": 1 / 500, "iso": 3200, "category": "city"})
    assert keys(d) == ["diffraction", "iso_high"]
    assert d["next"] == "1/60s　f/11　ISO 100"  # both fixes folded into one set of numbers
    assert shooting.ev100(11, 1 / 60, 100) == pytest.approx(shooting.ev100(22, 1 / 500, 3200), abs=0.4)
    # each finding names only its own change, so neither quotes a number the other overrides
    assert "把光圈開到 f/11" in d["findings"][0]["detail"] and "ISO 降到 100" in d["findings"][1]["detail"]
    assert "ISO 降到 800" not in d["findings"][0]["detail"]
    wide = shooting.diagnose({**BASE, "aperture": 1.8, "shutter_s": 1 / 4000, "iso": 400, "category": "landscape"})
    assert keys(wide) == ["wide_open_scene"] and wide["next"] == "1/200s　f/8　ISO 400"
    fine = shooting.diagnose({**BASE, "aperture": 8, "shutter_s": 1 / 250, "iso": 100, "category": "landscape"})
    assert keys(fine) == ["good_aperture"] and fine["next"] is None


def test_exposure_and_composition_come_from_measurement_and_the_edit():
    shot = {**BASE, "aperture": 5.6, "shutter_s": 1 / 250, "iso": 200, "category": "street"}
    over = shooting.diagnose({**shot, "flags": ["overexposed"], "exp_bias": 1.0})
    assert keys(over) == ["overexposed"] and "+1.0 EV" in over["findings"][0]["detail"] and "-0.7 EV" in over["findings"][0]["detail"]
    under = shooting.diagnose({**shot, "flags": ["underexposed"], "exp_program": "manual"})
    assert "直接調整快門或 ISO" in under["findings"][0]["detail"]
    # what was fixed afterwards is what should have been done at the time
    edited = shooting.diagnose({**shot, "exp_bias": -0.3, "tilt": 0.2,
                                "recipe": {"exposure": 0.7, "rotate": -1.8, "crop": {"left": 0.2, "top": 0.1, "right": 1, "bottom": 0.9}}})  # fmt: skip
    assert keys(edited) == ["post_exposure", "tilt", "heavy_crop"]
    by = {f["key"]: f for f in edited["findings"]}
    assert "+0.3 EV" in by["post_exposure"]["detail"] and "1.8°" in by["tilt"]["title"]
    assert "36%" in by["heavy_crop"]["title"] and "44mm" in by["heavy_crop"]["detail"] and "24 百萬" in by["heavy_crop"]["detail"]
    measured = shooting.diagnose({**shot, "tilt": -2.4})
    assert keys(measured) == ["tilt"] and "量到水平歪了 2.4°" in measured["findings"][0]["title"]
    assert keys(shooting.diagnose({**shot, "tilt": 0.6})) == []  # under a degree is not worth a remark


def test_a_phone_gets_advice_it_can_act_on():
    p = {"camera": "Apple iPhone 15 Pro", "focal": 6.8, "focal35": 24, "aperture": 1.78, "shutter_s": 1 / 8, "iso": 2000,
         "category": "night", "sharp": 10, "flags": ["underexposed"], "tilt": 2.2}  # fmt: skip
    d = shooting.diagnose(p)
    assert keys(d) == ["slow_shutter", "underexposed", "tilt"] and d["next"] is None  # no dials to turn on a phone
    text = " ".join(f["detail"] for f in d["findings"])
    assert "夜間模式" in text and "格線" in text and "曝光補償" not in text and "光圈先決" not in text
    assert shooting.summary(p)["device"] == "phone" and shooting.summary(p)["safe"] is None
    assert shooting.is_phone("Google Pixel 9") and not shooting.is_phone("FUJIFILM X-T5") and not shooting.is_phone(None)


def test_light_advice_uses_the_days_timetable():
    day = sun.day_events("2026-04-03", *KYOTO, 540)
    shot = {**BASE, "aperture": 8, "shutter_s": 1 / 250, "iso": 100, "category": "landscape", "tz_offset": 540}
    noon = shooting.diagnose({**shot, "sun_elev": 60, "light": "day", "taken_at": "2026-04-03T12:10:00"}, day=day)
    f = next(x for x in noon["findings"] if x["key"] == "midday")
    assert "17:46–18:35" in f["detail"] and "05:26–06:16" in f["detail"] and "再晚 5 小時 36 分" in f["detail"]
    near = shooting.diagnose({**shot, "sun_elev": 14, "light": "day", "taken_at": "2026-04-03T17:00:00", "heading": 270, "sun_az": 268}, day=day)
    by = {x["key"]: x for x in near["findings"]}
    assert by["near_golden"]["title"] == "再等 46 分鐘就是黃金時刻" and by["light_direction"]["title"] == "這張是逆光"
    early = shooting.diagnose({**shot, "sun_elev": 12, "light": "day", "taken_at": "2026-04-03T07:00:00"}, day=day)
    assert next(x for x in early["findings"] if x["key"] == "near_golden")["title"] == "早 44 分鐘到就趕得上黃金時刻"
    gold = shooting.diagnose({**shot, "sun_elev": 3, "light": "golden", "taken_at": "2026-04-03T18:05:00"}, day=day)
    assert "golden" in keys(gold) and "midday" not in keys(gold)
    # without a recorded time zone the clock may be an hour off, so no times are quoted
    unsure = shooting.diagnose({**shot, "tz_offset": None, "sun_elev": 60, "light": "day", "taken_at": "2026-04-03T12:10:00"}, day=day)
    assert "midday" in keys(unsure) and "17:46" not in unsure["findings"][-1]["detail"] + unsure["findings"][0]["detail"]
    indoors = shooting.diagnose({**shot, "category": "indoor", "sun_elev": 60, "light": "day", "taken_at": "2026-04-03T12:10:00"}, day=day)
    assert "midday" not in keys(indoors)
    # when a day has only one golden window, the sentence still reads properly
    one = {**day, "morning_golden": None, "evening_golden": [1300.0, 1380.0]}
    high = shooting.diagnose({**shot, "sun_elev": 55, "light": "day", "taken_at": "2026-06-21T12:00:00"}, day=one)
    assert "當天的黃金時刻是傍晚 21:40–23:00，也就是再晚 9 小時 40 分。" in next(x for x in high["findings"] if x["key"] == "midday")["detail"]


# ------------------------------------------------------- a library to coach


@pytest.fixture
def outing(ctx, tmp_path, fakes):
    """One afternoon in Kyoto with a camera that records everything, plus an earlier morning with a phone."""
    root = tmp_path / "shoot"
    cam = {"camera": ("FUJIFILM", "X-T5"), "lens": "XF23mmF2", "focal": 23, "focal35": 35, "tz": "+09:00"}
    t = datetime(2026, 4, 3, 12, 0)
    # four at noon: wide open, one blurred at a slow shutter, one tilted is not something a synthetic frame can show
    make_photo(root / "N0.jpg", 1, t, KYOTO, shot={**cam, "aperture": 2.0, "shutter": (1, 4000), "iso": 160, "program": 3, "bias": 0.0, "heading": 180})
    make_photo(root / "N1.jpg", 2, t + timedelta(minutes=9), KYOTO, shot={**cam, "aperture": 8.0, "shutter": (1, 500), "iso": 3200, "program": 3})
    make_photo(root / "N2.jpg", 3, t + timedelta(minutes=18), KYOTO, blur=7, shot={**cam, "aperture": 8.0, "shutter": (1, 8), "iso": 160, "program": 2})
    make_photo(root / "N3.jpg", 4, t + timedelta(minutes=27), KYOTO, shot={**cam, "aperture": 8.0, "shutter": (1, 250), "iso": 160, "program": 3})
    # a burst of two at the same framing: one sharp, one shaken
    make_photo(root / "B0.jpg", 60, t + timedelta(minutes=40), KYOTO, shot={**cam, "aperture": 5.6, "shutter": (1, 250), "iso": 400, "program": 3})
    make_photo(root / "B1.jpg", 60, t + timedelta(minutes=40, seconds=2), KYOTO, variant=2, blur=8,
               shot={**cam, "aperture": 5.6, "shutter": (1, 15), "iso": 160, "program": 3})  # fmt: skip
    # two in the evening golden hour
    g = datetime(2026, 4, 3, 18, 0)
    make_photo(root / "G0.jpg", 7, g, KYOTO, shot={**cam, "aperture": 8.0, "shutter": (1, 125), "iso": 200, "program": 1})
    make_photo(root / "G1.jpg", 8, g + timedelta(minutes=10), KYOTO, shot={**cam, "aperture": 11.0, "shutter": (1, 60), "iso": 200, "program": 1})
    ctx.db.run("INSERT INTO folders(path, added_at) VALUES (?,?)", (str(root), time.time()))
    pipeline.analyze(ctx, Job(1, "a", "t"))
    ids = {r["filename"]: r["id"] for r in ctx.db.all("SELECT id, filename FROM photos")}
    ctx.db.run("UPDATE photos SET category='landscape'")  # the fake embedder has no opinion about subject matter
    return ctx, ids


def test_camera_records_are_read_and_older_rows_catch_up(outing):
    ctx, ids = outing
    r = dict(ctx.db.one("SELECT * FROM photos WHERE id=?", (ids["N0.jpg"],)))
    assert (r["aperture"], r["iso"], r["focal"], r["focal35"], r["lens"]) == (2.0, 160, 23.0, 35.0, "XF23mmF2")
    assert r["shutter_s"] == pytest.approx(1 / 4000) and (r["exp_program"], r["exp_bias"], r["heading"], r["tz_offset"]) == ("aperture", 0.0, 180.0, 540)
    assert [ctx.db.val("SELECT exp_program FROM photos WHERE id=?", (ids[n],)) for n in ("N2.jpg", "G0.jpg")] == ["program", "manual"]
    # a library analysed before these fields existed is filled in without decoding a single image
    ctx.db.run("UPDATE photos SET meta_v=2, shutter_s=NULL, focal35=NULL, exp_program=NULL, heading=NULL")
    assert ingest.refresh_meta(ctx) == 8 and ingest.refresh_meta(ctx) == 0
    assert ctx.db.val("SELECT COUNT(*) FROM photos WHERE shutter_s IS NOT NULL AND focal35=35 AND exp_program IS NOT NULL") == 8
    raw_like = imaging._shooting(None, 1, "x", 9, None, 1, 0, 725.0, 0)  # odd values from odd cameras do not break the reader
    assert raw_like == {"shutter_s": None, "focal35": None, "exp_program": "manual", "exp_bias": None, "metering": None, "flash": None,
                        "wb": "manual", "heading": 5.0}  # fmt: skip


def test_one_photo_what_the_panel_shows(outing, fakes):
    ctx, ids = outing
    vlm, prov = fakes
    calls = vlm.calls
    d = mentor.photo(ctx, ids["N0.jpg"])
    assert vlm.calls == calls and not prov.calls  # looking costs nothing: no model at all
    assert d["summary"]["settings"] == "1/4000s　f/2　ISO 160" and d["summary"]["mode"] == "光圈先決 (A/Av)" and d["summary"]["safe"] == "1/40s"
    assert d["now"] == "1/4000s　f/2　ISO 160" and d["next"] == "1/250s　f/8　ISO 160"
    assert {f["key"] for f in d["findings"]} >= {"wide_open_scene", "midday", "light_direction"}
    light = d["light"]
    assert light["exact"] and light["time"] == "12:00" and light["compass"] == "南" and light["day"]["evening_golden"] == ["17:46", "18:35"]
    assert next(f for f in d["findings"] if f["key"] == "light_direction")["title"] == "這張是逆光"  # pointing south at noon
    slow = mentor.photo(ctx, ids["N2.jpg"])
    assert slow["findings"][0]["key"] == "slow_shutter" and slow["next"] == "1/40s　f/8　ISO 800"
    gold = mentor.photo(ctx, ids["G0.jpg"])
    assert "golden" in {f["key"] for f in gold["findings"]} and gold["light"]["phase"] == "golden" and gold["summary"]["mode"] == "手動 (M)"
    assert ctx.db.val("SELECT COUNT(*) FROM photos WHERE tilt IS NOT NULL") >= 3  # measured once, then remembered
    with pytest.raises(KeyError):
        mentor.photo(ctx, 9999)


def test_the_local_model_talks_about_the_picture_not_the_dials(outing, fakes):
    ctx, ids = outing
    vlm, prov = fakes
    seen = {}
    real = vlm.chat_json

    def spy(system, user, images=None, schema=None, **kw):
        seen.update(system=system, user=user, images=len(images or []))
        return real(system, user, images, schema, **kw)

    vlm.chat_json = spy
    advice = mentor.advise(ctx, ids["N0.jpg"])
    assert set(advice) == {"intent", "works", "composition", "position", "light", "one_thing"} and len(advice["composition"]) == 2
    assert seen["images"] == 1 and not prov.calls
    assert "不要提任何快門、光圈、ISO 的數字" in seen["system"]
    assert "1/4000s　f/2　ISO 160" in seen["user"] and "風景用 f/2 拍" in seen["user"] and "逆光" in seen["user"]
    d = mentor.photo(ctx, ids["N0.jpg"])
    assert d["advice"] == advice and d["advice_model"] == "fake-vlm"


def test_two_frames_of_one_scene_teach_the_difference(outing, fakes):
    ctx, ids = outing
    vlm, prov = fakes
    rows = mentor.enrich(ctx, mentor.load(ctx))
    found = mentor.pairs(ctx, rows)
    assert len(found) == 1
    p = found[0]
    assert (p["better"], p["worse"]) == (ids["B0.jpg"], ids["B1.jpg"])
    assert any(x.startswith("快門 1/250s 對 1/15s（差 4 級）") for x in p["diffs"]) and any(x.startswith("清晰度") for x in p["diffs"])
    assert p["lesson"] == "同一個畫面，快門快了 4 級就清楚了。"
    # the panel of either frame shows the same comparison
    assert mentor.photo(ctx, ids["B1.jpg"])["pair"]["better"] == ids["B0.jpg"] and mentor.photo(ctx, ids["B0.jpg"])["pair"]["worse"] == ids["B1.jpg"]
    assert mentor.photo(ctx, ids["N0.jpg"])["pair"] is None
    note = mentor.explain_pair(ctx, ids["B0.jpg"], ids["B1.jpg"])  # the on-device model looks at both side by side
    assert note["with"] == ids["B0.jpg"] and note["difference"] and note["lesson"] and not prov.calls
    assert mentor.pairs(ctx, rows)[0]["explained"] == note and mentor.photo(ctx, ids["B1.jpg"])["pair"]["explained"] == note


def test_the_lesson_fits_what_actually_differed():
    def frame(i, **kw):
        return {"id": i, "flags": [], "sharp": 90, "aperture": 8, "shutter_s": 1 / 250, "iso": 100, "focal35": 35, "tilt": 0.0, **kw}

    sharp = frame(1)
    # the blurred frame had the faster shutter: shake is ruled out, so it was focus
    c = mentor.compare(sharp, frame(2, sharp=5, shutter_s=1 / 2000, iso=800), 22)
    assert "對焦沒有落在主體上" in c["lesson"] and "手震" in c["lesson"] and "設定幾乎一樣" not in c["lesson"]
    soft = mentor.compare(sharp, frame(7, sharp=40, shutter_s=1 / 4000, aperture=2.8), 22)  # softer, not blurred, and faster: not shake either
    assert soft["lesson"].startswith("比較軟的那張快門其實更快") and "加快一級" not in soft["lesson"]
    same = mentor.compare(sharp, frame(3, sharp=5), 22)
    assert same["lesson"].startswith("設定幾乎一樣") and same["diffs"] == ["清晰度 90 對 5"]
    lean = mentor.compare(sharp, frame(4, tilt=2.5), 22)
    assert lean["diffs"] == ["水平 0.0° 對 2.5°"] and "水平拉正" in lean["lesson"]
    burnt = mentor.compare(sharp, frame(5, flags=["overexposed"]), 22)
    assert burnt["diffs"] == ["曝光：正常 對 過曝"] and "直方圖" in burnt["lesson"]
    assert mentor.compare(sharp, frame(6, sharp=88), 22) is None  # nothing to tell apart


def test_statistics_need_no_model(outing, fakes):
    ctx, ids = outing
    vlm, prov = fakes
    calls = vlm.calls
    rows = mentor.load(ctx)
    mentor.ensure_tilt(ctx, rows)
    s = mentor.stats(ctx, mentor.enrich(ctx, rows))
    assert vlm.calls == calls and not prov.calls
    assert s["count"] == 8 and s["span"] == ["2026-04-03", "2026-04-03"] and s["flaws"]["blurry"] == 2
    assert s["hours"][12] == 6 and s["hours"][18] == 2 and s["light"]["golden"] == 2
    assert [x["name"] for x in s["cameras"]] == ["FUJIFILM X-T5"] and s["focal"] == [{"name": "標準（35–70mm）", "n": 8, "share": 1.0}]
    assert {x["name"]: x["n"] for x in s["program"]} == {"光圈先決 (A/Av)": 5, "手動 (M)": 2, "程式自動 (P)": 1}
    m = s["metrics"]
    assert (m["blur_rate"]["value"], m["blur_rate"]["n"]) == (0.25, 8)
    assert m["slow_shutter_rate"]["value"] == 0.25 and m["golden_share"]["value"] == 0.25 and m["midday_share"]["value"] == 0.75
    assert m["high_iso_rate"]["value"] == 0.125 and m["aperture_variety"]["value"] == 4.0 and m["center_share"]["value"] is None
    assert mentor.show_metric(m["blur_rate"]) == "25%" and mentor.show_metric(m["center_share"]) == "—"
    top = {x["key"]: x for x in s["problems"]}
    assert top["midday"]["n"] == 6 and top["slow_shutter"]["n"] == 2 and top["iso_high"]["examples"] == [ids["N1.jpg"]]
    assert {x["key"] for x in s["good"]} >= {"golden", "good_aperture"}
    assert any("快門慢於安全快門" in x for x in s["insights"]) is False  # only two slow frames: too few to generalise from
    text = mentor.stats_text(s)
    assert "共 8 張" in text and "模糊 2 張" in text and "在正午的頂光下拍 6" in text and "黃金時刻 2 張" in text


def test_comparisons_inside_the_users_own_pictures():
    def row(i, **kw):
        base = {"id": i, "flags": [], "vlm": {}, "recipe": {}, "is_best": 1, "camera": "X", "category": "landscape", "sharp": 70,
                "_blurry": False, "_phone": False, "_slow": -2.0, "_keys": set(), "_d": {"findings": []}, "shutter_s": 1 / 250,
                "aperture": 8, "iso": 100, "focal35": 35, "light": None, "aesthetic": None, "vlm_score": None, "exp_program": "aperture"}  # fmt: skip
        return {**base, **kw}

    rows = [row(i, light="golden", aesthetic=70) for i in range(5)] + [row(10 + i, light="day", aesthetic=48) for i in range(6)]
    rows += [row(20 + i, _slow=1.5, _blurry=i < 3, shutter_s=1 / 15) for i in range(4)]
    rows += [row(30 + i, vlm={"techniques": ["中央構圖"], "composition": 5, "story": 4, "color": 8, "setting": "室外"}, vlm_score=55) for i in range(6)]
    rows += [row(40 + i, vlm={"techniques": ["引導線"], "composition": 8, "story": 5, "color": 8, "setting": "室外"}, vlm_score=74) for i in range(4)]
    rows += [row(50 + i, recipe={"exposure": 0.6, "rotate": 1.5 if i < 3 else 0}, exp_program="auto") for i in range(5)]
    text = "\n".join(mentor._insights(rows))
    assert "黃金或藍調時刻拍的 5 張，美感初評平均 70 分；白天拍的 6 張平均 48 分" in text
    assert "快門慢於安全快門的 4 張裡有 75% 模糊" in text
    assert "最常用的構圖是「中央構圖」（6 張，平均 55 分），但分數最高的是用了「引導線」的照片（4 張，平均 74 分）" in text
    assert "修過的 5 張裡有 5 張是往上提亮，平均 +0.6 級" in text and "有 3 張轉正過水平" in text
    assert "最強的是「色彩」（平均 8.0），最弱的是「故事性與意境」（平均 4.4）" in text
    assert mentor._insights(rows[:3]) == []  # three photos prove nothing
    m = mentor.measure(rows)
    assert m["center_share"]["value"] == 0.6 and m["technique_variety"]["value"] == 2.0 and m["score_composition"]["value"] == 6.2


def test_a_review_shows_the_external_model_one_sheet(outing, fakes):
    ctx, ids = outing
    vlm, prov = fakes
    out = mentor.outgoing(ctx, "library", None)
    assert out["count"] == 8 and len(out["photo_ids"]) == 8 and out["est_tokens"] > 0 and not prov.calls  # looking first costs nothing
    summary = mentor.review(ctx, "library", None, "cloud")
    assert len(prov.calls) == 1 and prov.calls[0]["images"] == 1  # every photo on one contact sheet
    sent = prov.calls[0]["text"]
    assert "共 8 張" in sent and "blur_rate：模糊照片的比例，目前 25%（8 張）" in sent and "建議設定：1/250s　f/8　ISO 160" in sent
    assert "不要自己發明數字" in mentor.MENTOR_SYSTEM
    call = dict(ctx.db.one("SELECT purpose, images, photos, contest_id FROM cloud_calls ORDER BY id DESC LIMIT 1"))
    assert call == {"purpose": "mentor_library", "images": 1, "photos": 8, "contest_id": None}
    r = mentor.get_review(ctx, "library", None)
    assert r["id"] == summary["id"] and r["provider"] == "fake" and r["stats"]["count"] == 8 and r["photo_ids"] == out["photo_ids"]
    rv = r["review"]
    assert rv["headline"] == "構圖穩定，但常在正午拍" and rv["next_time"] == ["黃金時刻再來", "帶腳架"]
    # the model speaks in sheet numbers; they are turned back into photos, and numbers that were not on the sheet are dropped
    assert rv["problems"][0]["photos"] == [out["photo_ids"][-1]] and rv["photo_notes"] == [{"photo_id": out["photo_ids"][0], "note": "這張最好"}]
    assert [x["metric"] for x in rv["exercises"]] == ["blur_rate", "manual"]  # an unknown measurement becomes a self-check
    ex = mentor.exercises(ctx)
    assert summary["exercises"] == 2 and {e["metric"]: e["baseline"] for e in ex} == {"blur_rate": 0.25, "manual": None}
    # asking again replaces the review but does not pile up the same exercise
    again = mentor.review(ctx, "library", None, "cloud")
    assert again["exercises"] == 1 and ctx.db.val("SELECT COUNT(*) FROM reviews") == 1 and len(mentor.exercises(ctx)) == 3
    assert ctx.db.val("SELECT COUNT(*) FROM exercises WHERE metric='blur_rate'") == 1


def test_reviews_without_the_external_model(outing, fakes):
    ctx, ids = outing
    vlm, prov = fakes
    eid = ctx.db.val("SELECT id FROM events LIMIT 1")
    only = mentor.review(ctx, "event", eid, "stats")  # numbers only
    assert only["headline"] is None and mentor.get_review(ctx, "event", eid)["review"] is None and not prov.calls
    calls = vlm.calls
    local = mentor.review(ctx, "event", eid, "local")  # the on-device model reads the same sheet
    assert vlm.calls == calls + 1 and not prov.calls and local["provider"] == "local"
    assert mentor.get_review(ctx, "event", eid)["review"]["headline"] and ctx.db.val("SELECT COUNT(*) FROM cloud_calls") == 0
    assert mentor.get_review(ctx, "library", None) is None
    with pytest.raises(KeyError):
        mentor.review(ctx, "event", 999, "stats")
    with pytest.raises(ValueError):
        mentor.review(ctx, "galaxy", None, "stats")


def test_the_sheet_holds_the_best_and_one_of_each_problem(outing):
    ctx, ids = outing
    rows = mentor.enrich(ctx, mentor.load(ctx))
    assert [p["id"] for p in mentor.pick_sheet(rows)] == [p["id"] for p in rows]  # few enough to show them all
    six = mentor.pick_sheet(rows, 6)
    names = {next(k for k, v in ids.items() if v == p["id"]) for p in six}
    assert len(six) == 6 and len({p["group_id"] for p in six}) == 6  # never two frames of the same burst
    assert names & {"N2.jpg", "B1.jpg"} and names & {"N1.jpg"} and names & {"G0.jpg", "G1.jpg", "N3.jpg"}


def test_exercises_are_checked_against_later_photos(outing, fakes):
    ctx, ids = outing
    noon = datetime(2026, 4, 3, 12, 30).replace(tzinfo=None)
    since = time.mktime(noon.timetuple()) - time.timezone  # the library's clock: local time read as UTC
    add = "INSERT INTO exercises(title, how, why, metric, baseline, baseline_n, since_ts, created_at) VALUES (?,?,?,?,?,?,?,?)"
    ctx.db.run(add, ("全部", "", "", "blur_rate", 0.6, 20, 0, 1))
    ctx.db.run(add, ("變差", "", "", "blur_rate", 0.0, 20, 0, 2))
    ctx.db.run(add, ("太少", "", "", "blur_rate", 0.6, 20, since, 3))
    ctx.db.run(add, ("多拍黃金時刻", "", "", "golden_share", 0.1, 20, 0, 4))
    ctx.db.run(add, ("自己確認", "", "", "manual", None, None, 0, 5))
    ex = {e["title"]: e for e in mentor.exercises(ctx)}
    assert (ex["全部"]["current"], ex["全部"]["n"], ex["全部"]["state"]) == (0.25, 8, "better")  # 60% blurred before, 25% since
    assert ex["變差"]["state"] == "worse" and ex["多拍黃金時刻"]["state"] == "better" and ex["多拍黃金時刻"]["current"] == 0.25
    assert ex["太少"]["n"] == 4 and ex["太少"]["state"] == "waiting"  # four new photos are not enough to judge
    assert ex["自己確認"]["state"] == "manual" and ex["全部"]["label"] == "模糊照片的比例"
    mentor.set_exercise(ctx, ex["全部"]["id"], "done")
    done = next(e for e in mentor.exercises(ctx) if e["title"] == "全部")
    assert done["status"] == "done" and done["current"] is None
    with pytest.raises(ValueError):
        mentor.set_exercise(ctx, 1, "banana")


# ---------------------------------------------------------- the next outing


def test_places_come_from_the_library_and_a_bundled_city_list(outing):
    ctx, _ = outing
    p = shootplan.places(ctx)
    assert p["cities"] == [] and len(p["past"]) == 1 and p["past"][0]["photos"] == 8 and "日本" in p["past"][0]["label"]
    kyoto = shootplan.places(ctx, "京都")["cities"][0]
    assert kyoto["label"] == "京都（日本）" and abs(kyoto["lat"] - 35.02) < 0.1 and kyoto["tz"] == "Asia/Tokyo"
    assert shootplan.places(ctx, "reykjav")["cities"][0]["tz"] == "Atlantic/Reykjavik"
    typed = shootplan.places(ctx, "48.8566, 2.3522")["cities"][0]
    assert (typed["lat"], typed["lon"]) == (48.8566, 2.3522) and "法國" in typed["label"]
    assert shootplan.places(ctx, "沒有這個地方xyz") == {"past": [], "cities": []}
    assert geocode.tz_offset(48.85, 2.35, "2026-07-01") == (120, True) and geocode.tz_offset(48.85, 2.35, "2026-01-15") == (60, True)  # summer time
    assert geocode.tz_offset(0.0, -150.0, "2026-01-01") == (-600, False)  # open ocean: guessed from longitude, and said so
    kit = shootplan.gear(ctx)
    assert kit == [{"camera": "FUJIFILM X-T5", "lens": "XF23mmF2", "photos": 8, "widest": 2.0, "focal35": 35, "phone": False}]


def test_a_plan_is_worked_out_offline(outing, fakes):
    ctx, ids = outing
    vlm, prov = fakes
    calls = vlm.calls
    plan = shootplan.make(ctx, lat=KYOTO[0], lon=KYOTO[1], place="嵐山", day="2026-11-20", genre="landscape", subject="楓葉和渡月橋",
                          camera="FUJIFILM X-T5", lens="XF23mmF2", use_model=False)  # fmt: skip
    assert vlm.calls == calls and not prov.calls
    assert (plan["sunrise"], plan["sunset"], plan["tz_offset"], plan["tz_exact"]) == ("06:36", "16:49", 540, True)
    tt = {w["key"]: w for w in plan["timetable"]}
    assert list(tt) == ["morning_blue", "morning_golden", "evening_golden", "evening_blue"]  # the November sun never gets overhead
    assert tt["evening_golden"]["best"] and tt["morning_golden"]["best"] and not tt["morning_blue"]["best"]
    assert "日落 16:49" in tt["evening_golden"]["note"] and "西南" in tt["evening_golden"]["note"]
    assert plan["directions"][0] == "想拍日出要面向東南方，想拍日落要面向西南方。"
    first = plan["settings"][0]
    assert (first["aperture"], first["iso"], first["mode"]) == ("f/8", "100", "光圈先決 (A/Av)") and "1/40s" in first["shutter"]
    assert plan["settings"][-1]["aperture"] == "f/2"  # hand-held at night: as wide as this lens is known to go
    # what this photographer tends to get wrong, from their own landscape pictures
    assert any("避開正午" in r and "出現過 6 次" in r for r in plan["reminders"]) and any("安全快門" in r for r in plan["reminders"])
    visit = plan["history"][0]
    assert visit["day"] == "2026-04-03" and visit["photos"] == 8 and "17:46–18:35" in visit["note"] and "其中 2 張趕上了好光線" in visit["note"]
    assert len(visit["best"]) == 3
    assert plan["shots"]["source"] == "builtin" and len(plan["shots"]["shots"]) == 4
    assert shootplan.get(ctx, plan["id"])["place"] == "嵐山" and shootplan.listing(ctx)[0]["genre_zh"] == "風景"


def test_the_shot_list_is_written_on_device(outing, fakes):
    ctx, _ = outing
    vlm, prov = fakes
    seen = {}
    real = vlm.chat_json

    def spy(system, user, images=None, schema=None, **kw):
        seen.update(user=user, images=images)
        return real(system, user, images, schema, **kw)

    vlm.chat_json = spy
    plan = shootplan.make(ctx, lat=KYOTO[0], lon=KYOTO[1], place="嵐山", day="2026-11-20", genre="street", subject="市場")
    assert plan["shots"]["source"] == "fake-vlm" and len(plan["shots"]["shots"]) == 2 and not prov.calls
    assert seen["images"] is None and "嵐山" in seen["user"] and "市場" in seen["user"] and "16:" in seen["user"]
    assert "不要編造具體的地標" in seen["user"] and "不要提快門、光圈、ISO 的數字" in seen["user"]

    def down(*a, **k):
        from photosift.local_vlm import LocalModelError

        raise LocalModelError("連不上地端模型")

    vlm.chat_json = down
    plan = shootplan.make(ctx, lat=KYOTO[0], lon=KYOTO[1], place="嵐山", day="2026-11-20", genre="portrait")
    assert plan["shots"]["source"] == "builtin" and plan["shots"]["error"] == "連不上地端模型" and plan["shots"]["shots"][0]["title"] == "環境人像"
    with pytest.raises(ValueError, match="題材"):
        shootplan.make(ctx, lat=0, lon=0, place="", day="2026-11-20", genre="selfie")
    with pytest.raises(ValueError, match="日期"):
        shootplan.make(ctx, lat=0, lon=0, place="", day="20 Nov", genre="city")


def test_a_phone_plan_has_no_dials(outing, fakes):
    ctx, _ = outing
    plan = shootplan.make(ctx, lat=64.15, lon=-21.94, place="", day="2026-06-21", genre="night", camera="Apple iPhone 15 Pro", use_model=False)
    assert plan["phone"] and plan["place"].startswith("雷克") and all(not s["aperture"] and not s["iso"] for s in plan["settings"])
    assert any("夜間模式" in s["note"] for s in plan["settings"]) and any("數位變焦" in s["note"] for s in plan["settings"])
    # the midnight sun: golden light from before midnight straight through to the small hours
    assert [(w["key"], w["start"], w["end"]) for w in plan["timetable"]] == [("morning_golden", "00:00", "04:52"), ("evening_golden", "22:07", "00:00")]


def test_the_cheat_sheet_is_one_self_contained_page(outing, fakes, tmp_path):
    ctx, ids = outing
    ctx.db.run("INSERT INTO exercises(title, how, why, metric, since_ts, created_at) VALUES ('同一場景拍三種光圈', 'f/2.8、f/5.6、f/11 各一張', '', 'manual', 0, 1)")
    plan = shootplan.make(ctx, lat=KYOTO[0], lon=KYOTO[1], place="嵐山 <渡月橋>", day="2026-11-20", genre="landscape", subject="楓葉",
                          camera="FUJIFILM X-T5", lens="XF23mmF2")  # fmt: skip
    page = shootplan.cheatsheet(ctx, plan["id"])
    assert "<script" not in page and "http" not in page and "嵐山 &lt;渡月橋&gt;" in page  # nothing loads, nothing runs, text is escaped
    assert "16:12–17:06" in page and "★ 最適合這次的題材" in page and "f/8" in page and page.count("type='checkbox'") == 2
    assert "練習：同一場景拍三種光圈" in page and "上次來這裡　2026-04-03" in page and page.count("data:image/jpeg;base64,") == 3
    assert "width=device-width" in page and "@media print" in page  # reads on a phone, prints on paper
    out = shootplan.export(ctx, plan["id"], str(tmp_path / "sheets"))
    assert Path(out["path"]).name == "拍攝小抄_嵐山_渡月橋_2026-11-20.html" and Path(out["path"]).read_text("utf-8") == page
    shootplan.delete(ctx, plan["id"])
    with pytest.raises(KeyError):
        shootplan.cheatsheet(ctx, plan["id"])


# ----------------------------------------------------------------- over http


@pytest.fixture
def client(ctx, library, fakes):
    app = create_app(ctx)
    with TestClient(app) as c:
        c.app_ctx, c.library, c.prov, c.vlm = ctx, library, fakes[1], fakes[0]
        yield c


def test_mentor_over_http(client, tmp_path):
    analyse(client)
    o = client.get("/api/mentor/overview").json()
    assert o["photos"] == 13 and o["library"] is None and len(o["events"]) >= 2 and o["exercises"] == [] and o["genres"]["landscape"] == "風景"
    assert all(e["review"] is None for e in o["events"]) and o["gear"][0]["camera"] == "TestCam T-1"
    pid = client.get("/api/photos?limit=1&sort=time").json()["items"][0]["id"]
    d = client.get(f"/api/mentor/photo/{pid}").json()
    assert d["summary"]["settings"] == "1/125s　f/2.8　ISO 400" and isinstance(d["findings"], list) and d["advice"] is None
    assert client.get("/api/mentor/photo/99999").status_code == 404
    a = client.post(f"/api/mentor/photo/{pid}/advise").json()
    assert a["model"] == "fake-vlm" and client.get(f"/api/mentor/photo/{pid}").json()["advice"] == a["advice"]

    out = client.get("/api/mentor/outgoing?scope=library").json()
    assert out["provider"] == "Gemini" and len(out["photo_ids"]) == 12 and out["via"] == "api"
    assert client.get("/api/mentor/review?scope=library").json()["id"] is None
    assert client.post("/api/mentor/review", json={"scope": "library", "mode": "telepathy"}).status_code == 400
    assert client.post("/api/mentor/review", json={"scope": "event"}).status_code == 400
    client.post("/api/mentor/review", json={"scope": "library", "mode": "cloud"})
    j = wait(client)
    assert j["status"] == "done" and j["result"]["headline"] == "構圖穩定，但常在正午拍" and j["result"]["exercises"] == 2
    r = client.get("/api/mentor/review?scope=library").json()
    assert r["review"]["problems"][0]["fix"] == "把快門加快到安全快門" and r["stats"]["count"] == 13 and len(client.prov.calls) == 1
    eid = o["events"][0]["id"]
    client.post("/api/mentor/review", json={"scope": "event", "id": eid, "mode": "stats"})
    assert wait(client)["status"] == "done" and len(client.prov.calls) == 1
    o = client.get("/api/mentor/overview").json()
    assert o["library"]["headline"] == "構圖穩定，但常在正午拍" and o["events"][0]["review"] == {**o["events"][0]["review"], "headline": None}
    ex = o["exercises"]
    assert len(ex) == 2 and ex[0]["status"] == "open"
    ex = client.patch(f"/api/mentor/exercises/{ex[0]['id']}", json={"status": "dropped"}).json()
    assert [e["status"] for e in ex].count("dropped") == 1
    assert client.patch(f"/api/mentor/exercises/{ex[0]['id']}", json={"status": "eaten"}).status_code == 400

    # a burst in the fixture: the comparison can be explained on demand
    pair = r["stats"]["pairs"][0]
    note = client.post("/api/mentor/pair", json={"better": pair["better"], "worse": pair["worse"]}).json()
    assert note["with"] == pair["better"] and note["lesson"]
    assert client.post("/api/mentor/pair", json={"better": 1, "worse": 99999}).status_code == 404

    places = client.get("/api/mentor/places").json()
    assert len(places["past"]) == 2 and client.get("/api/mentor/places?q=taipei").json()["cities"][0]["label"].startswith("台北")
    body = {"lat": 25.03, "lon": 121.56, "place": "", "day": "2026-12-01", "genre": "city", "subject": "101"}
    plan = client.post("/api/mentor/plan", json=body).json()
    assert plan["place"] and plan["sunset"] == "17:04" and plan["history"][0]["photos"] == 8 and plan["shots"]["source"] == "fake-vlm"
    assert client.post("/api/mentor/plan", json={**body, "genre": "selfie"}).status_code == 400
    sheet = client.get(f"/api/mentor/plan/{plan['id']}/sheet")
    assert sheet.headers["content-type"].startswith("text/html") and "光線時刻表" in sheet.text
    saved = client.post(f"/api/mentor/plan/{plan['id']}/export", json={"dest": str(tmp_path / "s")}).json()
    assert Path(saved["path"]).exists() and client.get("/api/mentor/overview").json()["plans"][0]["id"] == plan["id"]
    assert client.delete(f"/api/mentor/plan/{plan['id']}").json() == {"ok": True}
    assert client.get(f"/api/mentor/plan/{plan['id']}").status_code == 404 and client.get(f"/api/mentor/plan/{plan['id']}/sheet").status_code == 404
    assert json.dumps(client.get("/api/mentor/overview").json())  # everything the page loads is plain JSON
