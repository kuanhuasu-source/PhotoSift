"""Decoding (JPEG / HEIC / RAW), EXIF and resized copies. Originals are only ever read."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # HEIC support is optional at import time
    pillow_heif = None

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}
RAW_EXTS = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".raf", ".rw2", ".orf", ".dng", ".pef", ".srw"}
ALL_EXTS = IMAGE_EXTS | RAW_EXTS

Image.MAX_IMAGE_PIXELS = 400_000_000


def is_raw(path: Path) -> bool:
    return path.suffix.lower() in RAW_EXTS


def _open_raw(path: Path, max_side: int | None) -> Image.Image:
    import rawpy

    with rawpy.imread(str(path)) as raw:
        im = None
        try:
            t = raw.extract_thumb()
            if t.format == rawpy.ThumbFormat.JPEG:
                im = Image.open(io.BytesIO(t.data))
                im.load()
            elif t.format == rawpy.ThumbFormat.BITMAP:
                im = Image.fromarray(t.data)
        except Exception:
            im = None
        # Some cameras only embed a postage stamp; develop the sensor data instead.
        if im is None or max(im.size) < min(max_side or 1600, 1600):
            rgb = raw.postprocess(half_size=True, use_camera_wb=True, no_auto_bright=False)
            im = Image.fromarray(rgb)
    return im


def load_image(path: Path, max_side: int | None = None) -> tuple[Image.Image, tuple[int, int]]:
    """Returns (upright RGB image no larger than max_side, original upright size)."""
    path = Path(path)
    if is_raw(path):
        im = _open_raw(path, max_side)
        full = im.size
    else:
        im = Image.open(path)
        full = im.size
        if max_side and im.format == "JPEG":
            # Let libjpeg decode at 1/2, 1/4 or 1/8 scale: far faster for big files.
            im.draft("RGB", (max_side, max_side))
    orientation = 1
    try:
        orientation = im.getexif().get(0x0112, 1)
    except Exception:
        pass
    im = ImageOps.exif_transpose(im)
    if orientation in (5, 6, 7, 8) and not is_raw(path):
        full = (full[1], full[0])
    if is_raw(path):
        full = im.size
    if im.mode != "RGB":
        im = im.convert("RGB")
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return im, full


def resized(im: Image.Image, max_side: int) -> Image.Image:
    if max(im.size) <= max_side:
        return im
    out = im.copy()
    out.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return out


def jpeg_bytes(im: Image.Image, quality: int = 85) -> bytes:
    """Re-encode without any metadata (no EXIF, no GPS)."""
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


# ----------------------------------------------------------------------- EXIF


def _f(v) -> float | None:
    try:
        if isinstance(v, tuple) and len(v) == 2:
            return float(v[0]) / float(v[1]) if v[1] else None
        x = float(v)
        return x if x == x else None
    except Exception:
        return None


def _dms(v, ref) -> float | None:
    try:
        d, m, s = (_f(x) or 0.0 for x in v)
        deg = d + m / 60 + s / 3600
        if str(ref).upper() in ("S", "W"):
            deg = -deg
        return deg if -180 <= deg <= 180 else None
    except Exception:
        return None


def _parse_dt(s) -> datetime | None:
    if not s:
        return None
    s = str(s).strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            return dt if dt.year > 1990 else None
        except ValueError:
            continue
    return None


def _shutter(v) -> str | None:
    x = _f(v)
    if not x or x <= 0:
        return None
    if x >= 1:
        return f"{x:g}s"
    return f"1/{round(1 / x)}s"


def _clean(s) -> str | None:
    if s is None:
        return None
    s = str(s).replace("\x00", "").strip()
    return s or None


def _exif_pillow(path: Path) -> dict:
    with Image.open(path) as im:
        ex = im.getexif()
        sub = ex.get_ifd(0x8769) or {}
        gps = ex.get_ifd(0x8825) or {}
    make, model = _clean(ex.get(0x010F)), _clean(ex.get(0x0110))
    camera = model if (model and make and model.lower().startswith(make.split()[0].lower())) else " ".join(
        x for x in (make, model) if x
    )
    iso = sub.get(0x8827)
    if isinstance(iso, (tuple, list)):
        iso = iso[0] if iso else None
    return {
        "dt": _parse_dt(sub.get(0x9003)) or _parse_dt(sub.get(0x9004)) or _parse_dt(ex.get(0x0132)),
        "camera": camera or None,
        "lens": _clean(sub.get(0xA434)),
        "focal": _f(sub.get(0x920A)),
        "aperture": _f(sub.get(0x829D)),
        "shutter": _shutter(sub.get(0x829A)),
        "iso": int(iso) if _f(iso) else None,
        "lat": _dms(gps.get(2), gps.get(1)) if gps.get(2) else None,
        "lon": _dms(gps.get(4), gps.get(3)) if gps.get(4) else None,
    }


def _exif_exifread(path: Path) -> dict:
    import exifread

    with open(path, "rb") as f:
        tags = exifread.process_file(f, details=False)

    def val(name):
        t = tags.get(name)
        return t.values if t is not None else None

    def first(name):
        v = val(name)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if hasattr(v, "num"):
            return float(Fraction(v.num, v.den)) if v.den else None
        return v

    def dms(name, refname):
        v, ref = val(name), val(refname)
        if not v:
            return None
        parts = [float(Fraction(x.num, x.den)) if getattr(x, "den", 0) else 0.0 for x in v]
        return _dms(parts, ref)

    make, model = _clean(val("Image Make")), _clean(val("Image Model"))
    return {
        "dt": _parse_dt(val("EXIF DateTimeOriginal")) or _parse_dt(val("Image DateTime")),
        "camera": " ".join(x for x in (make, model) if x) or None,
        "lens": _clean(val("EXIF LensModel")),
        "focal": _f(first("EXIF FocalLength")),
        "aperture": _f(first("EXIF FNumber")),
        "shutter": _shutter(first("EXIF ExposureTime")),
        "iso": int(first("EXIF ISOSpeedRatings")) if _f(first("EXIF ISOSpeedRatings")) else None,
        "lat": dms("GPS GPSLatitude", "GPS GPSLatitudeRef"),
        "lon": dms("GPS GPSLongitude", "GPS GPSLongitudeRef"),
    }


def read_meta(path: Path) -> dict:
    """Capture time, camera and GPS. Falls back to the file's mtime when EXIF has no date."""
    path = Path(path)
    meta: dict = {}
    for reader in ((_exif_exifread,) if is_raw(path) else (_exif_pillow, _exif_exifread)):
        try:
            meta = reader(path)
        except Exception:
            meta = {}
        if meta.get("dt"):
            break
    dt = meta.pop("dt", None)
    if dt is not None:
        meta["time_source"] = "exif"
    else:
        dt = datetime.fromtimestamp(path.stat().st_mtime)
        meta["time_source"] = "mtime"
    meta["taken_at"] = dt.isoformat(timespec="seconds")
    # Naive local time read as UTC: stable ordering and gaps without guessing time zones.
    meta["taken_ts"] = dt.replace(tzinfo=timezone.utc).timestamp()
    return meta
