"""Where the sun was when a photo was taken, from capture time and GPS. No network, no model.

Photographers care about this more than almost any other fact about light:
the hour after sunrise and before sunset, and the blue minutes around them.
"""

from __future__ import annotations

import math

LABELS = {"night": "夜晚", "blue": "藍調時刻", "golden": "黃金時刻", "day": "白天"}


def elevation(utc_ts: float, lat: float, lon: float) -> float:
    """Sun's height above the horizon in degrees. Good to about half a degree."""
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
    return math.degrees(math.asin(math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(ha)))


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
