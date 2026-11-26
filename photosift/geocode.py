"""Offline reverse geocoding: nearest city from a bundled list. No network, no service."""

from __future__ import annotations

import math
import threading

import numpy as np

_lock = threading.Lock()
_cities = None  # (lat_rad, lon_rad, names, countries, time zones, populations, searchable text)

COUNTRY_ZH = {
    "TW": "台灣", "JP": "日本", "KR": "韓國", "CN": "中國", "HK": "香港", "MO": "澳門", "TH": "泰國", "VN": "越南",
    "SG": "新加坡", "MY": "馬來西亞", "ID": "印尼", "PH": "菲律賓", "IN": "印度", "US": "美國", "CA": "加拿大",
    "GB": "英國", "FR": "法國", "DE": "德國", "IT": "義大利", "ES": "西班牙", "PT": "葡萄牙", "NL": "荷蘭",
    "CH": "瑞士", "AT": "奧地利", "IS": "冰島", "NO": "挪威", "SE": "瑞典", "FI": "芬蘭", "DK": "丹麥",
    "AU": "澳洲", "NZ": "紐西蘭", "TR": "土耳其", "GR": "希臘", "CZ": "捷克", "EG": "埃及", "AE": "阿聯",
}  # fmt: skip


def _is_cjk(s: str) -> bool:
    return bool(s) and all("一" <= ch <= "鿿" for ch in s)


def _load():
    global _cities
    with _lock:
        if _cities is None:
            import geonamescache

            cs = list(geonamescache.GeonamesCache().get_cities().values())
            lat = np.radians(np.array([c["latitude"] for c in cs], dtype=np.float64))
            lon = np.radians(np.array([c["longitude"] for c in cs], dtype=np.float64))
            names = []
            for c in cs:
                # Prefer a short CJK name where the dataset has one, else the English name.
                cjk = sorted((a for a in c.get("alternatenames", []) if _is_cjk(a) and len(a) <= 6), key=len)
                names.append(cjk[0] if cjk else c["name"])
            text = ["|".join([c["name"], *c.get("alternatenames", [])]).lower() for c in cs]
            _cities = (lat, lon, names, [c["countrycode"] for c in cs], [c.get("timezone") for c in cs],
                       [c.get("population") or 0 for c in cs], text)  # fmt: skip
    return _cities


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _label(name: str, code: str) -> str:
    country = COUNTRY_ZH.get(code, code)
    return f"{name}（{country}）" if country not in name else name


def nearest(lat: float, lon: float, max_km: float = 150.0) -> dict | None:
    """The closest city in the bundled list: its name and time zone."""
    try:
        clat, clon, names, countries, tzs, _, _ = _load()
    except Exception:
        return None
    la, lo = math.radians(lat), math.radians(lon)
    a = np.sin((clat - la) / 2) ** 2 + np.cos(la) * np.cos(clat) * np.sin((clon - lo) / 2) ** 2
    d = 6371.0 * 2 * np.arcsin(np.sqrt(a))
    i = int(d.argmin())
    if d[i] > max_km:
        return None
    return {"label": _label(names[i], countries[i]), "tz": tzs[i], "km": float(d[i])}


def place_name(lat: float, lon: float, max_km: float = 150.0) -> str | None:
    c = nearest(lat, lon, max_km)
    return c["label"] if c else None


def search(query: str, limit: int = 8) -> list[dict]:
    """Cities whose name, in any language the list knows, contains the query. Larger cities first."""
    q = query.strip().lower()
    if not q:
        return []
    try:
        clat, clon, names, countries, tzs, pops, text = _load()
    except Exception:
        return []
    hits = [i for i, t in enumerate(text) if q in t]
    # A whole-name match beats a substring match, then size decides.
    hits.sort(key=lambda i: (0 if q in text[i].split("|") else 1, -pops[i]))
    return [{"label": _label(names[i], countries[i]), "lat": round(math.degrees(clat[i]), 4), "lon": round(math.degrees(clon[i]), 4),
             "tz": tzs[i]} for i in hits[:limit]]  # fmt: skip


def tz_offset(lat: float, lon: float, day: str) -> tuple[int, bool]:
    """Minutes east of UTC at a place on a date, with daylight saving. (offset, whether it is exact rather than guessed from longitude)."""
    c = nearest(lat, lon, 300.0)
    if c and c["tz"]:
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            at = datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=ZoneInfo(c["tz"]))
            return int(at.utcoffset().total_seconds() // 60), True
        except Exception:
            pass
    return round(lon / 15.0) * 60, False
