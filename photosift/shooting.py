"""What the camera settings did to a picture, and what to set next time.

No model is involved: this is arithmetic and photographers' rules of thumb.
Models are good at saying which way to go and poor at numbers, so every number
a user is told to dial in comes from here.

A finding is a small dict:
    key     stable name, used to count how often something happens
    level   fix (it hurt this picture) | tip (would be better) | good | info
    area    設定 | 構圖 | 光線
    title, detail   what to tell the photographer

Each photo gets at most one suggested set of settings. Every rule that wants a
change moves the same aperture/shutter/ISO triple, so two findings can never
ask for settings that contradict each other.
"""

from __future__ import annotations

import math

from . import sun

PHONE_MAKERS = ("apple", "iphone", "samsung", "google", "pixel", "xiaomi", "huawei", "oppo", "vivo", "oneplus", "realme", "zenfone")

# Third-stop scales, as printed on cameras.
APERTURES = [1.0, 1.1, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.5, 2.8, 3.2, 3.5, 4.0, 4.5, 5.0, 5.6, 6.3, 7.1, 8.0, 9.0, 10, 11, 13, 14, 16, 18, 20, 22, 25, 29, 32]
SHUTTERS = [30, 25, 20, 15, 13, 10, 8, 6, 5, 4, 3.2, 2.5, 2, 1.6, 1.3, 1, 0.8, 0.6, 0.5, 0.4, 0.3,
            *(1 / d for d in (4, 5, 6, 8, 10, 13, 15, 20, 25, 30, 40, 50, 60, 80, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800,
                              1000, 1250, 1600, 2000, 2500, 3200, 4000, 5000, 6400, 8000))]  # fmt: skip
ISOS = [50, 64, 80, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 2000, 2500, 3200, 4000, 5000, 6400,
        8000, 10000, 12800, 16000, 20000, 25600, 51200]  # fmt: skip

# The slowest shutter that freezes what is usually in such a picture.
MOVING = {"street": 1 / 250, "portrait": 1 / 125, "group": 1 / 125, "animal": 1 / 500, "event": 1 / 250, "vehicle": 1 / 500}
DEEP = {"landscape", "water", "city", "sunset"}             # usually wants everything sharp, front to back
SOFT = {"portrait", "food", "plant", "macro", "animal"}     # a soft background usually helps
OUTDOOR = {"landscape", "water", "city", "street", "sunset", "plant", "animal", "vehicle"}
PROGRAM_ZH = {"manual": "手動 (M)", "aperture": "光圈先決 (A/Av)", "shutter": "快門先決 (S/Tv)", "program": "程式自動 (P)", "auto": "全自動或場景模式"}
METERING_ZH = {"average": "平均測光", "center": "中央重點測光", "spot": "點測光", "matrix": "矩陣（評價）測光", "partial": "局部測光"}
ISO_CAP = 6400
LEVELS = {"fix": 0, "tip": 1, "info": 2, "good": 3}


def is_phone(camera: str | None) -> bool:
    c = (camera or "").lower()
    return any(m in c for m in PHONE_MAKERS)


def stops(a: float, b: float) -> float:
    """How many stops `a` is above `b`."""
    return math.log2(a / b)


def snap(x: float, scale: list[float]) -> float:
    return min(scale, key=lambda v: abs(math.log2(v / x)))


def ev100(aperture: float, shutter_s: float, iso: float) -> float:
    """Brightness of the scene as the camera exposed it, on the usual EV scale at ISO 100."""
    return math.log2(aperture * aperture / shutter_s) - math.log2(iso / 100.0)


def scene_light(ev: float) -> str:
    if ev >= 14:
        return "晴天的直射陽光"
    if ev >= 12:
        return "晴天或薄雲"
    if ev >= 9.5:
        return "陰天或陰影下"
    if ev >= 7:
        return "日出日落前後，或明亮的室內"
    if ev >= 4:
        return "一般室內或入夜的街道"
    return "夜景或昏暗的室內"


def fmt_shutter(t: float) -> str:
    if t >= 0.3:
        return f"{t:g} 秒"
    return f"1/{round(1 / t)}s"


def fmt_aperture(n: float) -> str:
    return f"f/{n:g}"


def fmt(aperture: float, shutter_s: float, iso: float) -> str:
    return f"{fmt_shutter(shutter_s)}　{fmt_aperture(aperture)}　ISO {int(iso)}"


def safe_shutter(focal35: float | None, category: str | None = None) -> float:
    """Slowest hand-held shutter: one over the focal length, and fast enough for what moves in this kind of picture."""
    hand = 1.0 / max(focal35 or 50.0, 30.0)
    move = MOVING.get(category or "")
    return snap(min(hand, move) if move else hand, SHUTTERS)


def rebalance(aperture: float, shutter_s: float, iso: float, *, t_new: float | None = None, n_new: float | None = None,
              iso_new: float | None = None, n_min: float | None = None, t_slowest: float | None = None,
              open_first: bool = True, iso_cap: int = ISO_CAP) -> tuple[float, float, int, float]:  # fmt: skip
    """Change one setting and keep the same exposure by moving the others.

    Returns (aperture, shutter, iso, shortfall). Shortfall is the stops of light that could not be made up
    within the lens's widest aperture, the slowest safe shutter and the ISO cap. It is negative in the rare
    case of too much light to get rid of (a slow shutter in full sun: that needs an ND filter).
    """
    n2, t2, iso2 = aperture, shutter_s, float(iso)
    need = 0.0  # stops of light to get back; negative means there is light to spare
    if t_new is not None:
        need += stops(shutter_s, t_new)
        t2 = t_new
    if n_new is not None:
        need += 2 * stops(n_new, aperture)
        n2 = n_new
    if iso_new is not None:
        need += stops(iso, iso_new)
        iso2 = float(iso_new)

    def open_up(need: float) -> float:
        nonlocal n2
        if n_new is None and n_min and n2 > n_min and need > 0:
            gain = min(need, 2 * stops(n2, n_min))
            n2 /= 2 ** (gain / 2)
            need -= gain
        return need

    def slow_down(need: float) -> float:
        nonlocal t2
        if t_new is None and t_slowest and t2 < t_slowest and need > 0:
            gain = min(need, stops(t_slowest, t2))
            t2 *= 2**gain
            need -= gain
        return need

    def raise_iso(need: float) -> float:
        nonlocal iso2
        if iso_new is None and iso2 < iso_cap and need > 0:
            gain = min(need, stops(iso_cap, iso2))
            iso2 *= 2**gain
            need -= gain
        return need

    if need > 0:
        for step in ((open_up, slow_down, raise_iso) if open_first else (slow_down, raise_iso, open_up)):
            need = step(need)
    elif need < 0:
        spare = -need
        if iso_new is None and iso2 > 100:  # cleanest first: give the light back as lower ISO
            g = min(spare, stops(iso2, 100))
            iso2 /= 2**g
            spare -= g
        if t_new is None and spare > 0 and t2 > SHUTTERS[-1]:
            g = min(spare, stops(t2, SHUTTERS[-1]))
            t2 /= 2**g
            spare -= g
        if n_new is None and spare > 0 and n2 < 16:  # last: stop down, but not into diffraction
            g = min(spare, 2 * stops(16, n2))
            n2 *= 2 ** (g / 2)
            spare -= g
        need = -spare
    return snap(n2, APERTURES), snap(t2, SHUTTERS), int(snap(iso2, ISOS)), round(need, 1)


def _f(key: str, level: str, area: str, title: str, detail: str) -> dict:
    return {"key": key, "level": level, "area": area, "title": title, "detail": detail}


def _how(n: float, t: float, iso: float, n2: float, t2: float, iso2: int, short: float) -> str:
    """Says in words what moved between two settings."""
    moves = []
    if abs(stops(t2, t)) >= 0.3:
        moves.append(f"快門{'放慢' if t2 > t else '加快'}到 {fmt_shutter(t2)}")
    if abs(stops(n2, n)) >= 0.15:
        moves.append(f"光圈{'縮' if n2 > n else '開'}到 {fmt_aperture(n2)}")
    if abs(stops(iso2, iso)) >= 0.3:
        moves.append(f"ISO {'提高' if iso2 > iso else '降'}到 {iso2}")
    text = "、".join(moves)
    if short >= 0.4:
        text += f"。這樣還差大約 {short:g} 級的光，需要腳架、倚靠固定物或補光"
    return text


def _thirds(ev: float) -> str:
    v = round(ev * 3) / 3
    return f"{v:+.1f}".replace("+0.0", "0").replace("-0.0", "0")


def _between(m: float, w: list[float] | None) -> bool:
    return bool(w) and w[0] <= m <= w[1]


def _span(w: list[float] | None) -> str:
    return f"{sun.hhmm(w[0])}–{sun.hhmm(w[1])}" if w else ""


def _gap(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60} 小時 {m % 60} 分" if m >= 60 else f"{m} 分鐘"


def summary(p: dict) -> dict:
    """The settings as shot, in the form a photographer reads them."""
    n, t, iso = p.get("aperture"), p.get("shutter_s"), p.get("iso")
    f35 = p.get("focal35") or p.get("focal")
    phone = is_phone(p.get("camera"))
    out = {
        "device": "phone" if phone else "camera" if p.get("camera") else None,
        "camera": p.get("camera"),
        "lens": p.get("lens"),
        "focal": p.get("focal"),
        "focal35": f35,
        "focal35_exact": bool(p.get("focal35")),
        "settings": fmt(n, t, iso) if n and t and iso else None,
        "mode": PROGRAM_ZH.get(p.get("exp_program") or ""),
        "bias": p.get("exp_bias"),
        "metering": METERING_ZH.get(p.get("metering") or ""),
        "flash": bool(p.get("flash")),
        "ev": None,
        "scene": None,
        "safe": None,
    }
    if n and t and iso:
        out["ev"] = round(ev100(n, t, iso), 1)
        out["scene"] = scene_light(out["ev"])
        if f35 and not phone:
            out["safe"] = fmt_shutter(safe_shutter(f35, p.get("category")))
    return out


def diagnose(p: dict, *, lens_max: float | None = None, blur_below: float = 22.0, day: dict | None = None) -> dict:
    """Findings for one photo, and the settings to try next time: {"findings", "now", "next", "shortfall"}.

    `p` is a photo row with `flags` as a list, plus optional `vlm` (annotation), `recipe` (the saved edit)
    and `tilt` (measured lean). `lens_max` is the widest aperture this lens is known to reach at this focal
    length. `day` is the light timetable for where and when it was taken.
    """
    out: list[dict] = []
    n, t, iso = p.get("aperture"), p.get("shutter_s"), p.get("iso")
    cat = p.get("category")
    flags = p.get("flags") or []
    vlm = p.get("vlm") or {}
    recipe = p.get("recipe") or {}
    phone = is_phone(p.get("camera"))
    f35 = p.get("focal35") or p.get("focal")
    blurry = p.get("sharp") is not None and p["sharp"] < blur_below
    sharp = p.get("sharp") or 0
    outdoor = vlm.get("setting") == "室外" if vlm.get("setting") else cat in OUTDOOR
    have = bool(n and t and iso)
    now = fmt(n, t, iso) if have else None
    auto = p.get("exp_program") in ("auto", "program")
    manual = p.get("exp_program") == "manual"
    bias = p.get("exp_bias") or 0.0

    # ------------------------------------------------------------ settings
    # One suggestion per photo: each rule moves the same running triple, so the advice never contradicts itself.
    cur = [n, t, iso]
    shortfall = 0.0

    steps: list[tuple[str, str]] = []  # (everything this step moved, the one change the rule is about)

    def move(primary: str, **kw) -> str:
        """Applies one rule's change. Returns a placeholder that is filled in once all rules have run."""
        nonlocal cur, shortfall
        n2, t2, iso2, short = rebalance(*cur, **kw)
        steps.append((_how(*cur, n2, t2, iso2, short), primary))
        cur, shortfall = [n2, t2, iso2], max(shortfall, short)
        return f"§{len(steps) - 1}§"

    setting_issue = False
    if have and phone:
        if blurry and t >= 1 / 30:
            out.append(_f("slow_shutter", "fix", "設定", f"光線不足，手機把快門拉長到 {fmt_shutter(t)}，畫面晃到了",
                          "手機會自己拉長曝光時間來換亮度。用夜間模式並雙手握穩、手肘靠著身體或倚著欄杆；"
                          "也可以點一下主體後把亮度往下滑，手機就會改用比較快的快門。"))  # fmt: skip
        elif blurry:
            out.append(_f("focus_miss", "fix", "設定", "畫面不夠清楚，可能是對焦沒有落在主體上",
                          "拍之前先點一下螢幕上的主體；離得太近時手機會對不到焦，退後一點再拍。"))  # fmt: skip
    elif have:
        safe = safe_shutter(f35, cat)
        slow_by = stops(t, safe)
        why = f"等效 {f35:.0f}mm 手持的安全快門大約是 {fmt_shutter(safe)}" if f35 else f"手持的安全快門大約是 {fmt_shutter(safe)}"
        if cat in MOVING and MOVING[cat] <= safe * 1.01:
            why = f"畫面裡有會動的主體，要凝結動作大約需要 {fmt_shutter(safe)}"
        scene_ev = ev100(n, t, iso)
        fixed_aperture = False  # a rule chose the aperture on purpose, so later steps must not reopen it

        # ----------------------------------------------------------- aperture
        if n >= 16 and t < 1:
            target = 11.0 if cat in DEEP else 8.0
            how = move(f"把光圈開到 {fmt_aperture(target)}", n_new=target)
            fixed_aperture = setting_issue = True
            out.append(_f("diffraction", "tip", "設定", f"光圈 {fmt_aperture(n)} 縮得太小，畫面反而變軟",
                          "光圈小到 f/16 以後，繞射會讓整張照片的細節變模糊。大多數鏡頭在 f/5.6–f/11 最銳利，"
                          f"景深也已經夠拍風景。下次{how}。"))  # fmt: skip
        elif n >= 16 and iso <= 200:
            out.append(_f("nd_filter", "tip", "設定", "想拉長曝光時間，用減光鏡比縮光圈好",
                          f"為了讓快門慢到 {fmt_shutter(t)} 把光圈縮到 {fmt_aperture(n)}，繞射會讓畫面變軟。"
                          "加一片 ND 減光鏡，就能把光圈留在 f/8–f/11 並得到同樣長的曝光。"))  # fmt: skip
        elif cat in DEEP and n <= 2.8 and (f35 or 50) >= 24 and scene_ev >= 8:
            how = move("把光圈縮到 f/8", n_new=8.0, t_slowest=safe, open_first=False)
            fixed_aperture = setting_issue = True
            out.append(_f("wide_open_scene", "tip", "設定", f"風景用 {fmt_aperture(n)} 拍，前後景可能不夠清楚",
                          f"大光圈的景深淺，鏡頭邊緣也比較軟。這個亮度下可以{how}，從前景到遠方都會清楚。"))  # fmt: skip
        elif cat in DEEP and 5.6 <= n <= 11 and not blurry:
            out.append(_f("good_aperture", "good", "設定", f"{fmt_aperture(n)} 是拍這種場景的好選擇",
                          "景深足夠，也落在鏡頭最銳利的範圍。"))  # fmt: skip
        elif cat == "portrait" and n >= 8 and lens_max and lens_max <= 4:
            how = move(f"把光圈開到 {fmt_aperture(max(lens_max, 2.0))}", n_new=max(lens_max, 2.0))
            fixed_aperture = setting_issue = True
            out.append(_f("stopped_down_portrait", "tip", "設定", f"人像用 {fmt_aperture(n)} 拍，背景會和人一樣清楚",
                          f"這顆鏡頭可以開到 {fmt_aperture(lens_max)}。開大光圈讓背景柔和，人就會從背景裡跳出來：{how}。"))  # fmt: skip

        # ------------------------------------------------------------ shutter
        widest = None if fixed_aperture else lens_max
        if t >= 0.5:  # a long exposure: either on purpose with support, or an accident
            if blurry:
                out.append(_f("long_shake", "fix", "設定", f"{fmt_shutter(t)} 的長曝光晃到了",
                              "這麼長的曝光一定要用腳架，並用 2 秒自拍或快門線觸發，避免按快門的震動。"
                              "有防手震的鏡頭上腳架時記得關掉防手震。"))  # fmt: skip
        elif slow_by >= 0.5 and blurry:
            how = move(f"把快門加快到 {fmt_shutter(safe)}", t_new=safe, n_min=widest, open_first=cat not in DEEP) if cur[1] > safe else ""
            setting_issue = True
            out.append(_f("slow_shutter", "fix", "設定", f"快門 {fmt_shutter(t)} 太慢，畫面糊了",
                          f"{why}，這張慢了 {slow_by:.1f} 級。" + (f"下次{how}。" if how else "")))  # fmt: skip
        elif slow_by >= 1.5 and sharp >= 55:
            out.append(_f("steady", "good", "設定", f"{fmt_shutter(t)} 手持還是清楚",
                          f"{why}，你比這個慢了 {slow_by:.1f} 級還能拍清楚，握持很穩（或防手震幫了忙）。"))  # fmt: skip
        elif slow_by >= 1.0:
            how = move(f"把快門加快到 {fmt_shutter(safe)}", t_new=safe, n_min=widest, open_first=cat not in DEEP) if cur[1] > safe else ""
            setting_issue = True
            out.append(_f("slow_shutter_risk", "tip", "設定", f"快門 {fmt_shutter(t)} 低於安全快門",
                          f"{why}。這張沒有糊，但同樣的設定多拍幾張就會有失敗的。" + (f"比較保險的做法是{how}。" if how else "")))  # fmt: skip
        elif blurry and slow_by < 0:
            shallow = f"光圈 {fmt_aperture(n)} 的景深很淺，你或主體稍微前後移動就會脫焦；" if n <= 2.8 else ""
            out.append(_f("focus_miss", "fix", "設定", "快門夠快但畫面還是不清楚，可能是對焦沒有落在主體上",
                          f"{shallow}改用單點對焦，把對焦點放在主體（人像放在靠近鏡頭的那隻眼睛）上，半按快門確認後再拍。"))  # fmt: skip

        # ---------------------------------------------------------------- iso
        headroom = stops(safe, cur[1])
        if iso >= 800 and cur[2] >= 400 and headroom >= 2 and t < 0.5:
            drop = int(min(headroom - 1, stops(cur[2], 100)))
            if drop >= 1:
                spare = f"快門 {fmt_shutter(cur[1])} 比安全快門 {fmt_shutter(safe)} 快了 {headroom:.0f} 級"
                lower = int(snap(cur[2] / 2**drop, ISOS))
                how = move(f"ISO 降到 {lower}", iso_new=lower, t_slowest=safe, open_first=False)
                setting_issue = True
                out.append(_f("iso_high", "tip", "設定", f"ISO {iso} 比需要的高",
                              f"{spare}，這些餘裕可以拿來降 ISO：{how}，雜訊更少、暗部更乾淨。"
                              + ("如果是上一個場景留下的設定，換場景時記得檢查 ISO。" if not auto else "")))  # fmt: skip

        if p.get("flash") and cat in DEEP | {"night"}:
            out.append(_f("flash_far", "tip", "設定", "閃光燈照不到遠處的景物",
                          "內建閃光燈只能打亮幾公尺內的東西，拍遠景時只會照亮前景的雜物並讓背景更暗。"
                          "關掉閃光燈，改用腳架或提高 ISO。"))  # fmt: skip

    # ------------------------------------------------------------ exposure
    edit_ev = float(recipe.get("exposure") or 0)
    how_bias = ("直接調整快門或 ISO" if manual else "用曝光補償")
    if "overexposed" in flags:
        if phone:
            detail = "亮部已經沒有細節，後製救不回來。拍之前點一下畫面裡最亮的地方，再把亮度往下滑。"
        else:
            target = _thirds(min(bias - 1.0, -0.7))
            detail = (f"亮部已經沒有細節，後製救不回來。下次{how_bias}減 1 級左右（補償設在 {target} EV），"
                      "並打開相機的高光警告，拍完馬上就能看到哪裡過曝。")  # fmt: skip
            if bias >= 0.6:
                detail += f"這張的曝光補償是 {bias:+.1f} EV，可能是上一個場景留下來的。"
        out.append(_f("overexposed", "fix", "設定", "亮部過曝", detail))
    elif "underexposed" in flags:
        if phone:
            detail = "整張偏暗。點一下主體讓手機對它測光，或把亮度往上滑；太暗的場景改用夜間模式。"
        else:
            detail = f"整張偏暗，後製提亮會帶出雜訊。下次{how_bias}加 1 級左右。"
            if bias <= -0.6:
                detail += f"這張的曝光補償是 {bias:+.1f} EV，可能是上一個場景留下來的，拍完一個場景記得歸零。"
        out.append(_f("underexposed", "fix", "設定", "整體曝光不足", detail))
    elif abs(edit_ev) >= 0.3 and not phone:
        word = "提亮" if edit_ev > 0 else "壓暗"
        out.append(_f("post_exposure", "tip", "設定", f"你在後製{word}了 {abs(edit_ev):g} 級",
                      f"拍的時候就{how_bias}{'加' if edit_ev > 0 else '減'} {abs(edit_ev):g} 級（補償設在 {_thirds(bias + edit_ev)} EV），"
                      + ("暗部的雜訊會更少。" if edit_ev > 0 else "亮部能保留更多細節。")))  # fmt: skip

    if auto and setting_issue and not phone:
        out.append(_f("auto_mode", "tip", "設定", f"這張是用{PROGRAM_ZH[p['exp_program']]}拍的",
                      "上面這些設定是相機替你決定的。改用光圈先決 (A/Av)：你決定光圈，也就是景深；"
                      "再到選單裡設定自動 ISO 的「最低快門速度」，相機就不會選出會手震的快門。"))  # fmt: skip

    # --------------------------------------------------------- composition
    tilt = p.get("tilt")
    turned = float(recipe.get("rotate") or 0)
    lean = turned if abs(turned) >= 0.7 else (tilt if tilt is not None and abs(tilt) >= 1.0 else 0.0)
    if lean:
        src = "你在後製轉正了" if abs(turned) >= 0.7 else "量到水平歪了"
        out.append(_f("tilt", "tip", "構圖", f"{src} {abs(lean):.1f}°",
                      ("打開相機 app 的格線，" if phone else "打開觀景窗的格線或電子水平儀，")
                      + "拍之前把地平線或建築的垂直線對齊。後製轉正會裁掉四個邊，等於白白損失畫素和原本的構圖。"))  # fmt: skip
    crop = recipe.get("crop") or {}
    if crop:
        kept = max((crop.get("right", 1) - crop.get("left", 0)) * (crop.get("bottom", 1) - crop.get("top", 0)), 0.01)
        if kept < 0.8:
            mp = (p.get("width") or 0) * (p.get("height") or 0) / 1e6
            lens = f"相當於把 {f35:.0f}mm 換成 {f35 / math.sqrt(kept):.0f}mm 來拍。" if f35 else ""
            pixels = f"原本 {mp:.0f} 百萬畫素，裁完只剩 {mp * kept:.0f} 百萬。" if mp else ""
            out.append(_f("heavy_crop", "tip", "構圖", f"後製裁掉了 {round((1 - kept) * 100)}% 的畫面",
                          f"{lens}{pixels}下次往前走幾步，或換長一點的焦段，在現場就把畫面填滿。"))  # fmt: skip

    # --------------------------------------------------------------- light
    elev = p.get("sun_elev")
    if elev is not None and outdoor and cat not in ("night", "indoor", "document", "food"):
        exact = p.get("tz_offset") is not None
        minute = None
        try:
            hh, mm = p["taken_at"][11:16].split(":")
            minute = int(hh) * 60 + int(mm)
        except Exception:
            pass
        if p.get("light") in ("golden", "blue"):
            out.append(_f("golden", "good", "光線", f"你在{sun.LABELS[p['light']]}拍的",
                          f"太陽高度 {elev:.0f}°，光線低而柔和、帶著顏色，是一天裡最好拍的時段。"))  # fmt: skip
        elif elev > sun.HARSH:
            detail = f"太陽高度 {elev:.0f}°，光從正上方來，陰影又短又硬，色彩和立體感都比較平。"
            if day and exact and minute is not None and (day.get("evening_golden") or day.get("morning_golden")):
                mg, eg = day.get("morning_golden"), day.get("evening_golden")
                detail += "當天的黃金時刻是" + " 和".join(x for x in (f"清晨 {_span(mg)}" if mg else "", f"傍晚 {_span(eg)}" if eg else "") if x)
                if eg and eg[0] - minute > 0:
                    detail += f"，也就是再晚 {_gap(eg[0] - minute)}"
                detail += "。"
            detail += "一定要在正午拍的話，找陰影下的主體、拍細節和紋理，或利用硬光拍高反差的畫面。"
            out.append(_f("midday", "tip", "光線", "正午的頂光", detail))
        elif day and exact and minute is not None and 6 <= elev <= 25:
            eg, mg = day.get("evening_golden"), day.get("morning_golden")
            if eg and 0 < eg[0] - minute <= 90:
                out.append(_f("near_golden", "tip", "光線", f"再等 {_gap(eg[0] - minute)}就是黃金時刻",
                              f"你在 {sun.hhmm(minute)} 拍，當天傍晚的黃金時刻是 {_span(eg)}。到時候太陽更低，光線會從側面掃過景物，"
                              "顏色變暖、陰影拉長，同一個場景會有更多層次。"))  # fmt: skip
            elif mg and 0 < minute - mg[1] <= 90:
                out.append(_f("near_golden", "tip", "光線", f"早 {_gap(minute - mg[1])}到就趕得上黃金時刻",
                              f"你在 {sun.hhmm(minute)} 拍，當天清晨的黃金時刻是 {_span(mg)}。"
                              "那時候光線更低更暖，通常人也比較少。"))  # fmt: skip
        if p.get("heading") is not None and elev > 3 and p.get("sun_az") is not None:
            kind, where = sun.relative_light(p["sun_az"], p["heading"])
            detail = f"{where}（太陽方位 {sun.compass(p['sun_az'])} {p['sun_az']:.0f}°，鏡頭朝 {sun.compass(p['heading'])}）。"
            if kind == "逆光" and (cat in ("portrait", "group") or (vlm.get("people") or 0) > 0):
                detail += "逆光拍人，臉會落在陰影裡：加 1 級左右的曝光補償、用反光板或閃燈補光，或者乾脆壓暗拍成剪影。"
            elif kind == "順光":
                detail += "順光的顏色飽和、容易曝光，但景物比較沒有立體感；想要層次可以繞到讓光從側面來的位置。"
            elif kind == "逆光":
                detail += "逆光時天空和主體的亮度差很大：想留住天空就減曝光拍成剪影，想留住主體就對主體測光，或用包圍曝光兩邊都拍。"
            elif kind == "側逆光":
                detail += "側逆光會替主體勾出一圈亮邊，很適合拍有輪廓的東西。留意鏡頭耀光，裝上遮光罩或用手擋一下。"
            elif kind == "順光":
                detail += "順光的顏色飽和、容易曝光，但景物比較沒有立體感；想要層次可以繞到讓光從側面來的位置。"
            elif kind == "側光":
                detail += "側光最能表現質感和立體感。"
            out.append(_f("light_direction", "info", "光線", f"這張是{kind}", detail))

    # With one change, say exactly what moves. With several, each finding names its own change and the
    # combined result is given once, so no finding quotes a number a later step overrides.
    for f in out:
        for i, (full, primary) in enumerate(steps):
            f["detail"] = f["detail"].replace(f"§{i}§", full if len(steps) == 1 else primary)
    out.sort(key=lambda f: LEVELS[f["level"]])
    changed = have and not phone and fmt(*cur) != now
    return {"findings": out, "now": now, "next": fmt(*cur) if changed else None, "shortfall": shortfall if changed else 0.0}
