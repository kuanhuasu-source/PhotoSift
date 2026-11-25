"""Build a small demo library from freely licensed Wikimedia Commons photos.

Development helper only. It downloads web-sized copies, then fabricates what a
real camera roll has: capture times, GPS, a few burst near-duplicates and a few
deliberately bad frames (blurred, over- and under-exposed), so every stage of
the funnel has something to do.

    uv run python scripts/fetch_samples.py samples
"""

from __future__ import annotations

import io
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter
from PIL.TiffImagePlugin import IFDRational as R

API = "https://commons.wikimedia.org/w/api.php"
UA = "PhotoSiftSampleFetcher/0.1 (development sample script)"

# (folder, Commons category, how many, base time, GPS, camera)
SETS = [
    ("2026-04 日本旅行", "Featured pictures of Kyoto", 9, datetime(2026, 4, 3, 8, 10), (35.0116, 135.7681), "SONY ILCE-7M4"),
    ("2026-04 日本旅行", "Quality images of Nara", 6, datetime(2026, 4, 5, 9, 30), (34.6851, 135.8048), "SONY ILCE-7M4"),
    ("2026-07 冰島", "Featured pictures of Iceland", 10, datetime(2026, 7, 14, 6, 45), (64.1466, -21.9426), "FUJIFILM X-T5"),
    ("2026-07 冰島", "Featured pictures of waterfalls", 5, datetime(2026, 7, 16, 11, 5), (63.6156, -19.9886), "FUJIFILM X-T5"),
    ("手機備份/美食", "Quality images of food", 8, datetime(2026, 9, 20, 12, 15), (25.0330, 121.5654), "Apple iPhone 17 Pro"),
    ("手機備份/台北街頭", "Quality images of Taipei", 9, datetime(2026, 10, 10, 17, 20), (25.0478, 121.5319), "Apple iPhone 17 Pro"),
    ("生態", "Featured pictures of birds", 8, datetime(2026, 5, 2, 6, 20), (24.7737, 121.0166), "Canon EOS R5"),
    ("生態", "Featured pictures of flowers", 5, datetime(2026, 5, 2, 9, 40), (24.7737, 121.0166), "Canon EOS R5"),
    ("人像", "Featured pictures of people", 6, datetime(2026, 8, 8, 15, 0), None, "Canon EOS R5"),
    ("夜景", "Night photographs of cities", 6, datetime(2026, 11, 1, 19, 30), (22.2855, 114.1577), "SONY ILCE-7M4"),
]


def fetch(url: str, tries: int = 6) -> bytes:
    """GET with polite back-off: Wikimedia rate-limits by address and says how long to wait."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == tries - 1:
                raise
            time.sleep(float(e.headers.get("retry-after") or 20) + 2)
    raise RuntimeError("unreachable")


def api(params: dict) -> dict:
    time.sleep(1.5)
    return json.loads(fetch(API + "?" + urllib.parse.urlencode({**params, "format": "json"})))


def category_images(cat: str, n: int, rng: random.Random) -> list[dict]:
    d = api({
        "action": "query", "generator": "categorymembers", "gcmtitle": f"Category:{cat}", "gcmtype": "file",
        "gcmlimit": 120, "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600,
    })  # fmt: skip
    pages = [p for p in d.get("query", {}).get("pages", {}).values() if p.get("imageinfo")]
    pages = [
        p for p in pages
        if p["imageinfo"][0].get("mime") == "image/jpeg" and 1200 <= p["imageinfo"][0]["width"] <= 12000
        and 0.45 < p["imageinfo"][0]["height"] / p["imageinfo"][0]["width"] < 2.0
    ]  # fmt: skip
    pages.sort(key=lambda p: p["title"])
    rng.shuffle(pages)
    return pages[:n]


def exif_bytes(dt: datetime, gps, camera: str) -> bytes:
    ex = Image.Exif()
    make, _, model = camera.partition(" ")
    ex[0x010F], ex[0x0110] = make, model
    ex[0x0132] = dt.strftime("%Y:%m:%d %H:%M:%S")
    sub = ex.get_ifd(0x8769)
    sub[0x9003] = dt.strftime("%Y:%m:%d %H:%M:%S")
    sub[0x8827], sub[0x829D], sub[0x829A], sub[0x920A] = 200, R(28, 10), R(1, 250), R(35, 1)
    if gps:
        lat, lon = gps

        def dms(v):
            v = abs(v)
            d, m = int(v), int((v - int(v)) * 60)
            s = round(((v - d) * 60 - m) * 60 * 100)
            return (R(d, 1), R(m, 1), R(s, 100))

        g = ex.get_ifd(0x8825)
        g[1], g[2] = ("N" if lat >= 0 else "S"), dms(lat)
        g[3], g[4] = ("E" if lon >= 0 else "W"), dms(lon)
    return ex.tobytes()


def main(dest: str) -> None:
    out = Path(dest)
    rng = random.Random(7)
    credits = []
    n = 0
    for folder, cat, count, t0, gps, camera in SETS:
        d = out / folder
        d.mkdir(parents=True, exist_ok=True)
        try:
            pages = category_images(cat, count, rng)
        except Exception as e:
            print(f"!! {cat}: {e}")
            continue
        t = t0
        for i, p in enumerate(pages):
            info = p["imageinfo"][0]
            try:
                # The same thumbnail is also served from upload.wikimedia.org.
                url = info["thumburl"].split("?")[0].replace("//thumb.wikimedia.org/", "//upload.wikimedia.org/")
                im = Image.open(io.BytesIO(fetch(url))).convert("RGB")
            except Exception as e:
                print(f"!! {p['title']}: {e}")
                continue
            n += 1
            t += timedelta(minutes=rng.randint(2, 40))
            g = (gps[0] + rng.uniform(-0.02, 0.02), gps[1] + rng.uniform(-0.02, 0.02)) if gps else None
            name = f"IMG_{n:04d}.jpg"
            im.save(d / name, "JPEG", quality=92, exif=exif_bytes(t, g, camera))
            meta = info.get("extmetadata", {})
            credits.append({
                "file": f"{folder}/{name}", "title": p["title"], "page": info.get("descriptionurl"),
                "license": meta.get("LicenseShortName", {}).get("value"),
                "artist": meta.get("Artist", {}).get("value"),
            })  # fmt: skip

            roll = rng.random()
            if roll < 0.22:  # burst: the same scene a second or two later, slightly reframed
                for k in range(rng.randint(1, 2)):
                    n += 1
                    w, h = im.size
                    dx, dy = rng.randint(0, w // 40), rng.randint(0, h // 40)
                    v = im.crop((dx, dy, w - (w // 40 - dx), h - (h // 40 - dy)))
                    if rng.random() < 0.5:
                        v = v.filter(ImageFilter.GaussianBlur(rng.uniform(1.5, 3.0)))  # the soft frame of the burst
                    v.save(d / f"IMG_{n:04d}.jpg", "JPEG", quality=90, exif=exif_bytes(t + timedelta(seconds=k + 1), g, camera))
            elif roll < 0.30:  # a throw-away frame
                n += 1
                kind = rng.choice(["blur", "over", "under"])
                if kind == "blur":
                    v = im.filter(ImageFilter.GaussianBlur(7))
                elif kind == "over":
                    v = ImageEnhance.Brightness(im).enhance(2.6)
                else:
                    v = ImageEnhance.Brightness(im).enhance(0.12)
                t += timedelta(minutes=1)
                v.save(d / f"IMG_{n:04d}.jpg", "JPEG", quality=88, exif=exif_bytes(t, g, camera))
            time.sleep(0.5)
        print(f"{folder:28s} {cat:36s} {len(pages)} photos")
    (out / "CREDITS.json").write_text(json.dumps(credits, ensure_ascii=False, indent=1), "utf-8")
    print(f"{n} files in {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples")
