"""Where the sun was when a photo was taken, from capture time and GPS. No network, no model.

Photographers care about this more than almost any other fact about light:
the hour after sunrise and before sunset, and the blue minutes around them.
"""

from __future__ import annotations

import math

LABELS = {"night": "夜晚", "blue": "藍調時刻", "golden": "黃金時刻", "day": "白天"}


def position(utc_ts: float, lat: float, lon: float) -> tuple[float, float]:
    """Sun's height above the horizon and its compass bearing (degrees clockwise from north). Good to about half a degree."""
    d = utc_ts / 86400.0 + 2440587.5 - 2451545.0  # days since J2000
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    lam = math.radians((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360)
    eps = math.radians(23.439 - 0.00000036 * d)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24
    ha = math.radians((gmst * 15 + lon) % 360) - ra
    phi = math.radians(lat)
    elev = math.degrees(math.asin(math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(ha)))
    from_south = math.atan2(math.sin(ha), math.cos(ha) * math.sin(phi) - math.tan(dec) * math.cos(phi))
    return elev, (math.degrees(from_south) + 180.0) % 360.0


def elevation(utc_ts: float, lat: float, lon: float) -> float:
    """Sun's height above the horizon in degrees."""
    return position(utc_ts, lat, lon)[0]


def phase(elev: float, exact: bool) -> str | None:
    """Light phase. Without a recorded time zone the clock may be an hour off, so only the clear cases are named."""
    if not exact:
        return "night" if elev < -18 else "day" if elev > 22 else None
    if elev < -6:
        return "night"
    if elev < -4:
        return "blue"
    if elev < 6:
        return "golden"
    return "day"


def light_for(taken_ts: float, lat: float, lon: float, tz_offset_min: int | None) -> tuple[float, str | None]:
    """`taken_ts` is the camera's local clock read as UTC, which is how the library stores it."""
    exact = tz_offset_min is not None
    offset = tz_offset_min if exact else round(lon / 15.0) * 60  # nearest whole zone when the camera did not say
    e = elevation(taken_ts - offset * 60, lat, lon)
    return round(e, 1), phase(e, exact)


# ------------------------------------------------- a whole day at one place

COMPASS = ["北", "東北", "東", "東南", "南", "西南", "西", "西北"]
HARSH = 50.0  # above this the light comes from overhead: short hard shadows, flat colour


def compass(az: float) -> str:
    return COMPASS[int((az % 360 + 22.5) // 45) % 8]


def guess_offset(lon: float) -> int:
    """Minutes east of UTC from longitude alone, for when nothing better is known."""
    return round(lon / 15.0) * 60


def hhmm(minute: float | None) -> str | None:
    if minute is None:
        return None
    m = int(round(minute)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def day_events(day: str, lat: float, lon: float, tz_offset_min: int | None = None) -> dict:
    """The light timetable for one local calendar day: when the blue and golden light starts and ends, and where the sun is.

    Times are minutes after local midnight. A window is None when it does not happen that day (high latitudes).
    """
    from datetime import datetime, timezone

    offset = guess_offset(lon) if tz_offset_min is None else tz_offset_min
    midnight = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() - offset * 60
    pos = [position(midnight + m * 60, lat, lon) for m in range(1441)]
    elev = [p[0] for p in pos]
    noon = max(range(1441), key=lambda m: elev[m])

    def cross(level: float, rising: bool) -> float | None:
        span = range(1, noon + 1) if rising else range(noon + 1, 1441)
        for m in span:
            a, b = elev[m - 1], elev[m]
            if (a < level <= b) if rising else (a >= level > b):
                return m - 1 + (level - a) / (b - a)  # linear within the minute
        return None

    def window(start: float | None, end: float | None) -> list[float] | None:
        return [start, end] if start is not None and end is not None and end > start else None

    def morning(lo: float, hi: float) -> list[float] | None:
        start, end = cross(lo, True), cross(hi, True)
        if start is None and end is not None and lo <= elev[0] < hi:
            start = 0.0  # already in this light at midnight (summer at high latitudes)
        return window(start, end)

    def evening(hi: float, lo: float) -> list[float] | None:
        start, end = cross(hi, False), cross(lo, False)
        if end is None and start is not None and lo <= elev[1440] < hi:
            end = 1440.0  # still in this light at midnight
        return window(start, end)

    rise, sset = cross(-0.833, True), cross(-0.833, False)
    harsh = window(cross(HARSH, True), cross(HARSH, False))
    return {
        "day": day,
        "tz_offset": offset,
        "tz_exact": tz_offset_min is not None,
        "sunrise": rise,
        "sunset": sset,
        "sunrise_az": round(pos[int(rise)][1]) if rise is not None else None,
        "sunset_az": round(pos[int(sset)][1]) if sset is not None else None,
        "noon": noon,
        "noon_elev": round(elev[noon], 1),
        "noon_az": round(pos[noon][1]),
        "morning_blue": morning(-6, -4),
        "morning_golden": morning(-4, 6),
        "harsh": harsh,
        "evening_golden": evening(6, -4),
        "evening_blue": evening(-4, -6),
        "day_minutes": round(sset - rise) if rise is not None and sset is not None else None,
    }


def relative_light(sun_az: float, heading: float) -> tuple[str, str]:
    """How the sun falls on what the camera is pointed at: (kind, plain description)."""
    diff = (sun_az - heading + 540) % 360 - 180  # -180..180, positive = sun to the right of the lens axis
    side = "右" if diff > 0 else "左"
    if abs(diff) <= 40:
        return "逆光", "太陽在鏡頭前方"
    if abs(diff) >= 140:
        return "順光", "太陽在你背後"
    if abs(diff) < 90:
        return "側逆光", f"太陽在{side}前方"
    return "側光", f"太陽在你的{side}手邊"
