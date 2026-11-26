"""Planning the next outing: when the light will be good, what to set, what to shoot.

Everything about time and direction is computed offline from the place and
date. Starting settings come from a table of working photographers' defaults,
adjusted to the lens the user owns. What the user personally tends to get
wrong comes from their own library. Only the shot list is written by a model,
the on-device one, and there is a built-in list for when it is not running.
"""

from __future__ import annotations

import base64
import html
import json
import re
import time
from collections import Counter
from pathlib import Path

from . import geocode, local_vlm, mentor, shooting, sun
from .context import Ctx

GENRES = {
    "landscape": "風景", "water": "水岸與海景", "city": "城市與建築", "sunset": "日出日落", "night": "夜景", "street": "街拍",
    "portrait": "人像", "event": "活動與表演", "animal": "動物", "plant": "花卉與植物", "food": "美食",
}  # fmt: skip
# Which library categories tell us how the user does in a genre.
RELATED = {"landscape": ("landscape", "water", "sunset"), "water": ("water", "landscape"), "city": ("city", "street"),
           "sunset": ("sunset", "landscape", "water"), "night": ("night", "city"), "street": ("street", "city"),
           "portrait": ("portrait", "group"), "event": ("event", "group"), "animal": ("animal",), "plant": ("plant", "macro"),
           "food": ("food",)}  # fmt: skip
BEST_LIGHT = {
    "landscape": ("morning_golden", "evening_golden", "evening_blue"), "water": ("morning_golden", "evening_golden", "evening_blue"),
    "city": ("evening_golden", "evening_blue"), "sunset": ("morning_golden", "evening_golden", "evening_blue"),
    "night": ("evening_blue",), "street": ("morning_golden", "evening_golden"), "portrait": ("morning_golden", "evening_golden"),
    "event": (), "animal": ("morning_golden", "evening_golden"), "plant": ("morning_golden",), "food": (),
}  # fmt: skip
WINDOWS = [
    ("morning_blue", "清晨的藍調時刻", "天還沒亮透，天空是深藍色，路燈和建築的燈還亮著。"),
    ("morning_golden", "清晨的黃金時刻", "光線低而暖，從側面掃過景物；人最少的時候。"),
    ("harsh", "正午的頂光", "光從正上方來，陰影短而硬。適合找陰影、拍細節，或拍高反差的畫面。"),
    ("evening_golden", "傍晚的黃金時刻", "一天裡最好拍的光：暖、低、有方向。提早到，把位置找好。"),
    ("evening_blue", "傍晚的藍調時刻", "日落後天空轉成深藍，和城市的暖色燈光最搭。只有十分鐘左右。"),
]
DEFAULT_FOCAL = {"landscape": 24, "water": 24, "city": 28, "sunset": 35, "night": 28, "street": 35, "portrait": 85, "event": 70,
                 "animal": 200, "plant": 90, "food": 50}  # fmt: skip
REMEDY = {
    "slow_shutter": "快門不要低於安全快門；光線不夠就提高 ISO，雜訊比模糊好救。",
    "slow_shutter_risk": "快門不要低於安全快門；光線不夠就提高 ISO。",
    "focus_miss": "用單點對焦，確認對焦點落在主體上再按快門。",
    "long_shake": "長曝光一定用腳架，加上 2 秒自拍。",
    "iso_high": "每換一個場景，先看一眼 ISO 有沒有調回來。",
    "diffraction": "光圈不要小於 f/11。",
    "wide_open_scene": "拍風景把光圈縮到 f/8 左右。",
    "overexposed": "拍完看高光警告，過曝就把曝光補償往下調。",
    "underexposed": "拍完看直方圖，太暗就把曝光補償往上調。",
    "post_exposure": "在現場就把曝光調對，不要留給後製。",
    "auto_mode": "改用光圈先決，自己決定景深。",
    "tilt": "按快門前看一眼格線，把水平對齊。",
    "heavy_crop": "往前走幾步，在現場就把畫面填滿。",
    "midday": "避開正午，照著時刻表在黃金時刻到。",
    "near_golden": "早一點到或多等一下，趕上黃金時刻。",
    "flash_far": "拍遠景時關掉閃光燈。",
}
FALLBACK_SHOTS = {
    "scenic": [("交代環境的大景", "黃金時刻", "找一個前景（石頭、欄杆、花草）放在畫面下方三分之一，讓畫面有深度。"),
               ("有引導線的中景", "黃金時刻", "找道路、河岸或橋，讓線條從畫面一角帶向主體。"),
               ("細節與紋理", "任何時間", "靠近拍一個只有這裡才有的細節，之後和大景放在一起才有故事。"),
               ("藍調時刻的同一個構圖", "藍調時刻", "把黃金時刻拍過的最好構圖再拍一次，兩種光線可以比較。")],
    "street": [("等人走進光裡", "黃金時刻", "先找到一塊好看的光或背景，站定等人走進來，不要追著人拍。"),
               ("一個環境裡的人", "白天", "退後一步，把人放小，讓環境說明他在做什麼。"),
               ("手和物件的特寫", "任何時間", "攤販的手、門把、招牌的一角，補足大場景沒說的事。"),
               ("入夜後的燈光", "藍調時刻", "找櫥窗或招牌的光當主光源，拍經過的人的輪廓。")],
    "people": [("環境人像", "黃金時刻", "讓人站在光從側面來的位置，背景留一點環境。"),
               ("半身近照", "黃金時刻", "開大光圈，對焦在靠近鏡頭的眼睛，背景離人越遠越好。"),
               ("沒有看鏡頭的瞬間", "任何時間", "請對方做自己的事，連拍幾張，挑最自然的。"),
               ("手或側臉的細節", "任何時間", "換一個角度拍局部，一組照片才有變化。")],
    "close": [("主體清楚、背景乾淨", "柔和的光", "蹲低或繞一圈，找到一個背景單純的角度再拍。"),
              ("逆光的透亮感", "黃金時刻", "讓光從主體後方穿過來，花瓣和葉子會發亮。"),
              ("一組三張：遠、中、近", "任何時間", "同一個主體用三種距離各拍一張。")],
}
SHOT_KIND = {"landscape": "scenic", "water": "scenic", "city": "scenic", "sunset": "scenic", "night": "scenic", "street": "street",
             "portrait": "people", "event": "people", "animal": "close", "plant": "close", "food": "close"}  # fmt: skip


def places(ctx: Ctx, query: str = "") -> dict:
    """Where the user has been (from the library) and cities matching what they typed. Nothing leaves the machine."""
    q = query.strip()
    m = re.match(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[,，\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$", q)
    if m and abs(float(m.group(1))) <= 90 and abs(float(m.group(2))) <= 180:
        lat, lon = float(m.group(1)), float(m.group(2))
        return {"past": [], "cities": [{"label": geocode.place_name(lat, lon) or f"{lat:.3f}, {lon:.3f}", "lat": lat, "lon": lon}]}
    rows = ctx.db.all(
        "SELECT e.place, AVG(p.lat) lat, AVG(p.lon) lon, COUNT(*) n, MAX(p.taken_at) last, COUNT(DISTINCT e.id) visits "
        "FROM photos p JOIN events e ON e.id=p.event_id WHERE p.lat IS NOT NULL AND e.place IS NOT NULL GROUP BY e.place ORDER BY last DESC"
    )
    past = [{"label": r["place"], "lat": round(r["lat"], 4), "lon": round(r["lon"], 4), "photos": r["n"], "visits": r["visits"],
             "last": r["last"][:10]} for r in rows if not q or q.lower() in r["place"].lower()]  # fmt: skip
    return {"past": past[:12], "cities": geocode.search(q) if q else []}


def gear(ctx: Ctx) -> list[dict]:
    """Cameras and lenses seen in the library, most used first, with what each lens can open up to."""
    rows = ctx.db.all(
        "SELECT camera, lens, COUNT(*) n, MIN(aperture) widest, AVG(COALESCE(focal35, focal)) focal FROM photos "
        "WHERE camera IS NOT NULL AND error IS NULL GROUP BY camera, lens ORDER BY n DESC LIMIT 12"
    )
    return [{"camera": r["camera"], "lens": r["lens"], "photos": r["n"], "widest": r["widest"], "focal35": round(r["focal"]) if r["focal"] else None,
             "phone": shooting.is_phone(r["camera"])} for r in rows]  # fmt: skip


def _span(w) -> str:
    return f"{sun.hhmm(w[0])}–{sun.hhmm(w[1])}"


def timetable(day: dict, genre: str) -> list[dict]:
    out = []
    best = BEST_LIGHT.get(genre, ())
    for key, label, note in WINDOWS:
        w = day.get(key)
        if not w:
            continue
        extra = ""
        if key == "morning_golden" and day["sunrise"] is not None:
            extra = f"日出 {sun.hhmm(day['sunrise'])}，太陽從{sun.compass(day['sunrise_az'])}方（{day['sunrise_az']}°）升起。"
        if key == "evening_golden" and day["sunset"] is not None:
            extra = f"日落 {sun.hhmm(day['sunset'])}，太陽往{sun.compass(day['sunset_az'])}方（{day['sunset_az']}°）落下。"
        if key == "harsh":
            extra = f"正午太陽高度 {day['noon_elev']:.0f}°。"
        out.append({"key": key, "label": label, "start": sun.hhmm(w[0]), "end": sun.hhmm(w[1]), "minutes": [round(w[0]), round(w[1])],
                    "note": extra + note, "best": key in best})  # fmt: skip
    return out


def directions(day: dict) -> list[str]:
    """Which way to face, said the way a photographer plans it."""
    out = []
    if day["sunrise"] is not None and day["sunset"] is not None:
        rise, down = sun.compass(day["sunrise_az"]), sun.compass(day["sunset_az"])
        out.append(f"想拍日出要面向{rise}方，想拍日落要面向{down}方。")
        out.append(f"要讓景物被暖光照亮（順光）：清晨拍面向{rise}方的那一面，也就是你站在它的{rise}邊往{sun.compass(day['sunrise_az'] + 180)}拍；"
                   f"傍晚拍面向{down}方的那一面。")  # fmt: skip
        noon = "南" if day["noon_az"] and 90 < day["noon_az"] < 270 else "北"
        out.append(f"正午太陽在{noon}方，朝{noon}拍是逆光，朝{'北' if noon == '南' else '南'}拍天空最藍。")
    return out


def starting_settings(genre: str, phone: bool, widest: float | None, focal35: float | None) -> list[dict]:
    """Where to set the camera before the first frame. A starting point to adjust from, not a rule."""
    w = shooting.fmt_aperture(widest) if widest else "最大光圈"
    f = focal35 or DEFAULT_FOCAL.get(genre, 35)
    safe = shooting.fmt_shutter(shooting.safe_shutter(f))

    def card(when, mode, aperture, shutter, iso, note):
        return {"when": when, "mode": mode, "aperture": aperture, "shutter": shutter, "iso": iso, "note": note}

    if phone:
        tips = [card("每一張", "", "", "", "", "打開相機的格線。點一下主體讓手機對它對焦和測光，再上下滑動調整亮度。"),
                card("取景", "", "", "", "", "用手機實際有的鏡頭倍率（0.5×、1×、2×、3×），不要用兩指放大：數位變焦只是裁切。")]  # fmt: skip
        if genre in ("night", "city", "sunset", "landscape", "water"):
            tips.append(card("天色暗的時候", "", "", "", "", "用夜間模式，手肘靠著身體或把手機靠在欄杆上，倒數結束前不要動。"))
        if genre in ("portrait", "food", "plant", "animal"):
            tips.append(card("想要柔和的背景", "", "", "", "", "用人像模式，或讓主體離背景遠一點、自己靠近主體。"))
        if genre in ("street", "event", "animal"):
            tips.append(card("會動的主體", "", "", "", "", "按住快門連拍，事後挑最清楚、時機最好的一張。"))
        return tips
    tripod = card("藍調時刻與夜晚・有腳架", "手動 (M) 或光圈先決", "f/8", "2–15 秒", "100", "用 2 秒自拍或快門線觸發，上腳架時關掉防手震。")
    hand = card("藍調時刻與夜晚・手持", "光圈先決 (A/Av)", w, f"不低於 {safe}", "自動，上限 6400", "靠著欄杆或牆，同一個畫面連拍三張，挑最清楚的。")
    if genre in ("landscape", "water", "city", "sunset"):
        out = [card("白天與黃金時刻", "光圈先決 (A/Av)", "f/8", f"相機決定；低於 {safe} 就上腳架或提高 ISO", "100",
                    "曝光補償設 −0.3 EV 保住天空的細節。")]  # fmt: skip
        if genre == "sunset":
            out.append(card("太陽在畫面裡", "光圈先決 (A/Av)", "f/8–f/11", "相機決定", "100", "曝光補償 −0.7 到 −1 EV，天空的顏色才會濃；再用包圍曝光 ±1 EV 各拍一張。"))
        if genre == "water":
            out.append(card("絲絹般的水流・有腳架", "手動 (M)", "f/11", "0.5–4 秒", "100", "白天需要 ND 減光鏡才慢得下來。想凝結浪花則反過來用 1/1000s。"))
        return out + [tripod, hand]
    if genre == "night":
        return [tripod, hand]
    if genre == "street":
        return [card("白天", "光圈先決或快門先決", "f/5.6–f/8", "1/250s 以上", "自動，上限 3200", "預先把焦對在 3 公尺左右，看到畫面直接按，不必等對焦。"),
                card("傍晚到入夜", "光圈先決 (A/Av)", w, "1/125s 以上", "自動，上限 6400", "找有光的地方站定，等人走進光裡。")]  # fmt: skip
    if genre == "portrait":
        wide = w if widest and widest <= 2.8 else ("f/2.8" if not widest else w)
        return [card("戶外", "光圈先決 (A/Av)", wide, "1/200s 以上", "100–400", "對焦在靠近鏡頭的那隻眼睛。逆光時曝光補償加 0.7 EV。"),
                card("室內或陰影下", "光圈先決 (A/Av)", w, "1/125s 以上", "自動，上限 3200", "讓人靠近窗邊，光從側面來，臉才有立體感。")]  # fmt: skip
    if genre == "event":
        return [card("活動現場", "快門先決 (S/Tv)", w, "1/250s 以上", "自動，上限 6400", "用連續對焦 (AF-C) 加連拍；舞台燈光變化大，多拍幾張。")]
    if genre == "animal":
        return [card("會動的動物", "快門先決 (S/Tv)", w, "1/1000s 以上", "自動，上限 6400", "連續對焦加連拍。蹲到和牠的眼睛一樣高再拍。")]
    if genre == "plant":
        return [card("花草與近拍", "光圈先決 (A/Av)", "f/4–f/5.6", "1/250s 以上（有風時）", "100–400", "先找乾淨的背景再對焦；太靠近時景深很淺，光圈不要開到最大。")]
    return [card("餐桌上", "光圈先決 (A/Av)", "f/2.8–f/4", "1/100s 以上", "400–800", "坐到窗邊，讓光從側面或斜後方來。曝光補償加 0.3 EV，食物看起來更新鮮。")]


def history(ctx: Ctx, lat: float, lon: float, km: float = 25.0) -> list[dict]:
    """Earlier outings to the same place, with when the user was there against when the light was good."""
    out = []
    for r in ctx.db.all(
        "SELECT p.event_id id, e.title, AVG(p.lat) lat, AVG(p.lon) lon, COUNT(*) n, MIN(p.taken_at) a, MAX(p.taken_at) b, MAX(p.tz_offset) tz "
        "FROM photos p JOIN events e ON e.id=p.event_id WHERE p.lat IS NOT NULL AND p.time_source='exif' GROUP BY p.event_id ORDER BY a DESC"
    ):
        if geocode.haversine_km(lat, lon, r["lat"], r["lon"]) > km:
            continue
        best = [x["id"] for x in ctx.db.all(
            "SELECT id FROM photos WHERE event_id=? AND is_best=1 ORDER BY COALESCE(vlm_score, aesthetic, 0) DESC LIMIT 3", (r["id"],))]  # fmt: skip
        light = Counter(x["light"] for x in ctx.db.all("SELECT light FROM photos WHERE event_id=? AND light IS NOT NULL", (r["id"],)))
        day = sun.day_events(r["a"][:10], r["lat"], r["lon"], r["tz"])
        good = sum(light[k] for k in ("golden", "blue"))
        note = f"你在 {r['a'][11:16]}–{r['b'][11:16]} 拍了 {r['n']} 張"
        if day.get("evening_golden") and day.get("morning_golden") and r["tz"] is not None:
            note += f"；當天的黃金時刻是 {_span(day['morning_golden'])} 和 {_span(day['evening_golden'])}"
            note += f"，其中 {good} 張趕上了好光線。" if good else "，都沒有趕上。"
        else:
            note += "。"
        rev = mentor.get_review(ctx, "event", r["id"])
        out.append({"event_id": r["id"], "title": r["title"], "day": r["a"][:10], "photos": r["n"], "best": best, "note": note,
                    "headline": rev["review"]["headline"] if rev and rev["review"] else None})  # fmt: skip
        if len(out) >= 3:
            break
    return out


def reminders(ctx: Ctx, genre: str) -> list[str]:
    """What this user in particular should keep in mind, from their own pictures."""
    out = []
    cats = RELATED.get(genre, (genre,))
    marks = ",".join("?" * len(cats))
    rows = mentor.enrich(ctx, mentor.load(ctx, f"p.category IN ({marks})", cats)[-400:])
    counts = Counter(k for p in rows for f in p["_d"]["findings"] if f["level"] in ("fix", "tip") for k in [f["key"]])
    for key, n in counts.most_common():
        if n >= 2 and key in REMEDY and len(out) < 2:
            out.append(f"{REMEDY[key]}（你拍這類照片時，「{mentor.KEY_ZH.get(key, key)}」出現過 {n} 次）")
    rev = mentor.get_review(ctx, "library", None)
    if rev and rev["review"]:
        out += [f"{x['title']}：{x['fix']}" for x in rev["review"]["problems"][:2]]
    return out[:4]


SHOTS_SCHEMA = {
    "type": "object",
    "properties": {
        "concept": {"type": "string"},
        "shots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "when": {"type": "string"}, "how": {"type": "string"}},
                "required": ["title", "when", "how"],
                "additionalProperties": False,
            },
        },
        "avoid": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["concept", "shots", "avoid"],
    "additionalProperties": False,
}


def shot_list(ctx: Ctx, plan: dict) -> dict:
    """A short list of pictures to come home with. Text only, on-device."""
    light = "；".join(f"{w['label']} {w['start']}–{w['end']}" for w in plan["timetable"])
    user = (
        f"學生要去「{plan['place']}」拍照，日期 {plan['day']}，想拍的題材是{GENRES.get(plan['genre'], plan['genre'])}"
        + (f"，他說：「{plan['subject']}」" if plan["subject"] else "")
        + f"。\n當天的光線：{light or '（無法計算）'}\n"
        + ("方位：" + " ".join(plan["directions"]) + "\n" if plan["directions"] else "")
        + ("他過去常犯的問題：" + "；".join(plan["reminders"]) + "\n" if plan["reminders"] else "")
        + "\n請幫他列一份拍攝清單：\n"
        "- concept：這次拍攝可以圍繞的一個想法，一句話\n"
        "- shots：5 張要帶回來的照片，要有遠景、中景和細節的變化。title 是這張要拍什麼；when 是上面哪個時段最適合；"
        "how 是怎麼拍（站在哪裡、前景放什麼、等什麼），一兩句\n"
        "- avoid：這種題材最常見的 2 個失誤\n"
        "你沒有去過現場，不要編造具體的地標或店名，用「找一個…」這種說法。不要提快門、光圈、ISO 的數字。"
    )
    vlm = local_vlm.LocalVLM(ctx.settings.local, role="judge")
    d = vlm.chat_json("你是一位有經驗的攝影老師，正在幫學生準備一次外拍。只輸出 JSON，用繁體中文。", user, None, SHOTS_SCHEMA, max_tokens=1200)
    shots = [{"title": str(x.get("title", "")).strip()[:80], "when": str(x.get("when", "")).strip(" 、，,。")[:40], "how": str(x.get("how", "")).strip()[:300]}
             for x in (d.get("shots") or [])[:6] if str(x.get("title", "")).strip()]  # fmt: skip
    if not shots:
        raise local_vlm.LocalModelError("地端模型沒有列出拍攝清單。")
    return {"concept": str(d.get("concept", "")).strip()[:200], "shots": shots,
            "avoid": [str(x).strip()[:200] for x in (d.get("avoid") or [])[:3] if str(x).strip()], "source": vlm.model}  # fmt: skip


def make(ctx: Ctx, *, lat: float, lon: float, place: str, day: str, genre: str, subject: str = "", camera: str = "",
         lens: str = "", use_model: bool = True) -> dict:  # fmt: skip
    """Builds and stores a plan for one outing."""
    if genre not in GENRES:
        raise ValueError("不支援的題材")
    try:
        time.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise ValueError("日期格式要是 2026-12-31 這樣") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("地點的座標不正確")
    offset, exact = geocode.tz_offset(lat, lon, day)
    events = sun.day_events(day, lat, lon, offset)
    kit = next((g for g in gear(ctx) if g["camera"] == camera and (g["lens"] or "") == (lens or "")), None)
    phone = shooting.is_phone(camera) if camera else False
    plan = {
        "place": place.strip()[:80] or geocode.place_name(lat, lon) or f"{lat:.3f}, {lon:.3f}",
        "lat": round(lat, 4), "lon": round(lon, 4), "day": day, "genre": genre, "subject": subject.strip()[:300],
        "camera": camera, "lens": lens, "phone": phone,
        "tz_offset": offset, "tz_exact": exact,
        "sunrise": sun.hhmm(events["sunrise"]), "sunset": sun.hhmm(events["sunset"]), "noon_elev": events["noon_elev"],
        "timetable": timetable(events, genre),
        "directions": directions(events),
        "settings": starting_settings(genre, phone, kit["widest"] if kit and kit["lens"] else None, kit["focal35"] if kit else None),
        "history": history(ctx, lat, lon),
        "reminders": reminders(ctx, genre),
        "exercises": [{"title": e["title"], "how": e["how"]} for e in mentor.exercises(ctx) if e["status"] == "open"][:3],
    }  # fmt: skip
    kind = FALLBACK_SHOTS[SHOT_KIND.get(genre, "scenic")]
    plan["shots"] = {"concept": "", "avoid": [], "source": "builtin", "shots": [{"title": t, "when": w, "how": h} for t, w, h in kind]}
    if use_model:
        try:
            plan["shots"] = shot_list(ctx, plan)
        except local_vlm.LocalModelError as e:
            plan["shots"]["error"] = str(e)
    cur = ctx.db.run("INSERT INTO plans(created_at, place, lat, lon, day, data) VALUES (?,?,?,?,?,?)",
                     (time.time(), plan["place"], lat, lon, day, json.dumps(plan, ensure_ascii=False)))  # fmt: skip
    return {"id": cur.lastrowid, **plan}


def get(ctx: Ctx, pid: int) -> dict:
    r = ctx.db.one("SELECT id, created_at, data FROM plans WHERE id=?", (pid,))
    if r is None:
        raise KeyError(pid)
    return {"id": r["id"], "created_at": r["created_at"], **json.loads(r["data"])}


def listing(ctx: Ctx) -> list[dict]:
    out = []
    for r in ctx.db.all("SELECT id, created_at, place, day, data FROM plans ORDER BY day DESC, id DESC LIMIT 50"):
        d = json.loads(r["data"])
        out.append({"id": r["id"], "place": r["place"], "day": r["day"], "genre": d["genre"], "genre_zh": GENRES.get(d["genre"], d["genre"]),
                    "subject": d.get("subject", "")})  # fmt: skip
    return out


def delete(ctx: Ctx, pid: int) -> None:
    ctx.db.run("DELETE FROM plans WHERE id=?", (pid,))


# ---------------------------------------------------------------- cheat sheet


def cheatsheet(ctx: Ctx, pid: int) -> str:
    """The plan as one page for a phone screen or a sheet of paper. Self-contained: no scripts, no links, no network."""
    p = get(ctx, pid)
    x = html.escape
    parts = [
        "<!doctype html><html lang='zh-Hant'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>拍攝小抄　{x(p['place'])}　{x(p['day'])}</title>",
        "<style>body{font:17px/1.65 -apple-system,'PingFang TC','Noto Sans TC','Microsoft JhengHei',sans-serif;max-width:640px;margin:0 auto;"
        "padding:18px 16px 40px;color:#161616;background:#fff}h1{font-size:23px;margin:0 0 2px;line-height:1.3}h2{font-size:17px;margin:26px 0 8px;"
        "padding-bottom:5px;border-bottom:2px solid #161616}.m{color:#5c5c5c;font-size:14px}.w{display:flex;gap:12px;padding:9px 0;"
        "border-bottom:1px solid #e3e3e3}.w .t{font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap;min-width:108px}"
        ".w.best .t{color:#a85e00}.w small{display:block;color:#5c5c5c;font-size:14px;line-height:1.5}.star{display:block;color:#a85e00;font-weight:700}"
        ".c{border:1px solid #cfcfcf;border-radius:10px;padding:11px 13px;margin:9px 0}.c b.h{display:block;margin-bottom:4px}"
        ".k{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:6px 0}.k div{background:#f2f2f2;border-radius:7px;padding:5px 8px;"
        "font-size:14px}.k span{display:block;color:#6a6a6a;font-size:12px}.k b{font-size:16px}ul{padding-left:20px;margin:6px 0}li{margin:5px 0}"
        "label{display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-bottom:1px solid #e3e3e3}input{width:22px;height:22px;"
        "margin:3px 0 0;flex:none}label small{display:block;color:#5c5c5c;font-size:14px;line-height:1.5}"
        ".ph{display:flex;gap:6px;margin:8px 0}.ph img{width:32%;aspect-ratio:3/2;object-fit:cover;border-radius:6px}"
        "@media print{body{font-size:12pt;max-width:none;padding:0}h2{page-break-after:avoid}.c,.w,label{page-break-inside:avoid}}</style>",
        f"<h1>{x(p['place'])}</h1>",
        f"<div class='m'>{x(p['day'])}　{x(GENRES.get(p['genre'], p['genre']))}" + (f"　{x(p['subject'])}" if p["subject"] else "") + "</div>",
    ]
    if p["shots"].get("concept"):
        parts.append(f"<p><b>這次的想法：</b>{x(p['shots']['concept'])}</p>")
    parts.append("<h2>光線時刻表</h2>")
    if p["sunrise"] and p["sunset"]:
        parts.append(f"<div class='m'>日出 {p['sunrise']}　日落 {p['sunset']}" + ("" if p["tz_exact"] else "　（時區是依經度估計的，可能差一小時）") + "</div>")
    for w in p["timetable"]:
        star = "<span class='star'>★ 最適合這次的題材</span>" if w["best"] else ""
        parts.append(f"<div class='w{' best' if w['best'] else ''}'><div class='t'>{w['start']}–{w['end']}</div>"
                     f"<div><b>{x(w['label'])}</b><small>{star}{x(w['note'])}</small></div></div>")  # fmt: skip
    if not p["timetable"]:
        parts.append("<p class='m'>這一天在這個緯度沒有一般的日出日落，無法列出時刻表。</p>")
    if p["directions"]:
        parts.append("<ul>" + "".join(f"<li>{x(d)}</li>" for d in p["directions"]) + "</ul>")
    parts.append("<h2>起始設定</h2>")
    if p["camera"]:
        parts.append(f"<div class='m'>{x(p['camera'])}" + (f"　{x(p['lens'])}" if p["lens"] else "") + "</div>")
    for s in p["settings"]:
        parts.append(f"<div class='c'><b class='h'>{x(s['when'])}</b>")
        if s["aperture"] or s["shutter"] or s["iso"]:
            parts.append("<div class='k'>" + "".join(f"<div><span>{k}</span><b>{x(v)}</b></div>" for k, v in
                                                       (("光圈", s["aperture"]), ("快門", s["shutter"]), ("ISO", s["iso"])) if v) + "</div>")  # fmt: skip
        parts.append((f"<div class='m'>{x(s['mode'])}</div>" if s["mode"] else "") + f"<div>{x(s['note'])}</div></div>")
    parts.append("<h2>拍攝清單</h2>")
    for s in p["shots"]["shots"]:
        parts.append(f"<label><input type='checkbox'><div><b>{x(s['title'])}</b>" + (f"　<span class='m'>{x(s['when'])}</span>" if s["when"] else "")
                     + f"<small>{x(s['how'])}</small></div></label>")  # fmt: skip
    if p["shots"].get("avoid"):
        parts.append("<p class='m'>容易失誤的地方：" + "；".join(x(a) for a in p["shots"]["avoid"]) + "</p>")
    if p["reminders"] or p["exercises"]:
        parts.append("<h2>給你自己的提醒</h2><ul>" + "".join(f"<li>{x(r)}</li>" for r in p["reminders"])
                     + "".join(f"<li><b>練習：{x(e['title'])}</b>　{x(e['how'] or '')}</li>" for e in p["exercises"]) + "</ul>")  # fmt: skip
    for h in p["history"]:
        parts.append(f"<h2>上次來這裡　{x(h['day'])}</h2><div>{x(h['note'])}</div>")
        if h.get("headline"):
            parts.append(f"<div class='m'>當時的講評：{x(h['headline'])}</div>")
        imgs = []
        for photo_id in h["best"]:
            tp = ctx.thumb_path(photo_id)
            if tp.exists():
                imgs.append("<img alt='' src='data:image/jpeg;base64," + base64.b64encode(tp.read_bytes()).decode() + "'>")
        if imgs:
            parts.append("<div class='m'>那次拍得最好的幾張：</div><div class='ph'>" + "".join(imgs) + "</div>")
    return "\n".join(parts)


def export(ctx: Ctx, pid: int, dest: str) -> dict:
    p = get(ctx, pid)
    folder = Path(dest).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", p["place"]).strip("_")[:40] or "拍攝計畫"
    path = folder / f"拍攝小抄_{safe}_{p['day']}.html"
    path.write_text(cheatsheet(ctx, pid), "utf-8")
    return {"path": str(path), "bytes": path.stat().st_size}
