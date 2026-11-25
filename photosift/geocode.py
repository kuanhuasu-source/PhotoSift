"""Offline reverse geocoding: nearest city from a bundled list. No network, no service."""

from __future__ import annotations

import math
import threading

import numpy as np

_lock = threading.Lock()
_cities = None  # (lat_rad, lon_rad, names, countries)

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
            _cities = (lat, lon, names, [c["countrycode"] for c in cs])
    return _cities


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def place_name(lat: float, lon: float, max_km: float = 150.0) -> str | None:
    try:
        clat, clon, names, countries = _load()
    except Exception:
        return None
    la, lo = math.radians(lat), math.radians(lon)
    a = np.sin((clat - la) / 2) ** 2 + np.cos(la) * np.cos(clat) * np.sin((clon - lo) / 2) ** 2
    d = 6371.0 * 2 * np.arcsin(np.sqrt(a))
    i = int(d.argmin())
    if d[i] > max_km:
        return None
    country = COUNTRY_ZH.get(countries[i], countries[i])
    return f"{names[i]}（{country}）" if country not in names[i] else names[i]
