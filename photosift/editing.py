"""Non-destructive editing: a recipe of global adjustments rendered onto a copy.

Only whole-image adjustments, cropping and straightening. Nothing is removed
from or added to the picture, which is what most photo contests allow.

A recipe is a small dict. Missing keys mean "leave alone", so {} is the original.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageFilter

# name: (min, max, neutral)
PARAMS: dict[str, tuple[float, float, float]] = {
    "exposure": (-1.5, 1.5, 0.0),     # stops
    "contrast": (-100, 100, 0),
    "highlights": (-100, 100, 0),
    "shadows": (-100, 100, 0),
    "temperature": (-100, 100, 0),    # blue .. yellow
    "tint": (-100, 100, 0),           # green .. magenta
    "vibrance": (-100, 100, 0),
    "saturation": (-100, 100, 0),
    "clarity": (-100, 100, 0),
    "sharpen": (0, 100, 0),
    "vignette": (-100, 100, 0),       # negative darkens the corners
    "rotate": (-10.0, 10.0, 0.0),     # degrees, positive turns the picture clockwise
}  # fmt: skip
CROP_KEYS = ("left", "top", "right", "bottom")  # fractions 0..1 of the kept area's edges
MIN_CROP = 0.3  # never keep less than this share of either side


def clean(recipe: dict | None) -> dict:
    """Clamp to legal ranges and drop everything that is neutral, so equal edits compare equal."""
    out: dict = {}
    recipe = recipe or {}
    for k, (lo, hi, neutral) in PARAMS.items():
        try:
            v = float(recipe.get(k, neutral))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        v = min(max(v, lo), hi)
        v = round(v, 2) if k in ("exposure", "rotate") else round(v)
        if v != neutral:
            out[k] = v
    if recipe.get("bw"):
        out["bw"] = True
    c = recipe.get("crop")
    if isinstance(c, dict):
        try:
            l, t, r, b = (min(max(float(c.get(k, d)), 0.0), 1.0) for k, d in zip(CROP_KEYS, (0, 0, 1, 1)))
        except (TypeError, ValueError):
            l, t, r, b = 0.0, 0.0, 1.0, 1.0
        if r - l < MIN_CROP:  # a box this small is a model mistake, not a crop
            l, r = 0.0, 1.0
        if b - t < MIN_CROP:
            t, b = 0.0, 1.0
        if (l, t, r, b) != (0.0, 0.0, 1.0, 1.0) and (l > 0.005 or t > 0.005 or r < 0.995 or b < 0.995):
            out["crop"] = {"left": round(l, 4), "top": round(t, 4), "right": round(r, 4), "bottom": round(b, 4)}
    return out


def is_identity(recipe: dict | None) -> bool:
    return not clean(recipe)


# ------------------------------------------------------------------ geometry


def straighten(im: Image.Image, degrees: float) -> Image.Image:
    """Rotate, then trim to the largest upright rectangle of the same shape with no empty corners."""
    if abs(degrees) < 0.01:
        return im
    w, h = im.size
    rot = im.rotate(-degrees, resample=Image.Resampling.BICUBIC, expand=False)
    a = math.radians(abs(degrees))
    # Largest same-aspect rectangle inside the rotated frame.
    scale = min(w / (w * math.cos(a) + h * math.sin(a)), h / (w * math.sin(a) + h * math.cos(a)))
    cw, ch = int(w * scale), int(h * scale)
    x, y = (w - cw) // 2, (h - ch) // 2
    return rot.crop((x, y, x + cw, y + ch))


def crop_box(size: tuple[int, int], crop: dict) -> tuple[int, int, int, int]:
    w, h = size
    return (round(crop["left"] * w), round(crop["top"] * h), round(crop["right"] * w), round(crop["bottom"] * h))


def _profile_score(H: Image.Image, V: Image.Image, disc: np.ndarray, angle: float) -> float:
    """How well level and plumb edges line up with the pixel grid after turning by `angle`."""
    hr = np.asarray(H.rotate(angle, resample=Image.Resampling.BILINEAR)) * disc
    vr = np.asarray(V.rotate(angle, resample=Image.Resampling.BILINEAR)) * disc
    return float((hr.sum(axis=1) ** 2).sum() + (vr.sum(axis=0) ** 2).sum())


def detect_tilt(im: Image.Image, span: float = 7.0) -> tuple[float, float]:
    """Estimated correction in degrees (what `rotate` should be set to) and a 0..1 confidence.

    Works like document deskewing: the right angle is the one where horizons and walls
    collapse into sharp rows and columns. Reliable for architecture and clear horizons,
    a guess for anything else, so callers should only act on a confident answer.
    """
    g = im.convert("L")
    g.thumbnail((300, 300), Image.Resampling.BILINEAR)
    a = np.asarray(g.filter(ImageFilter.GaussianBlur(0.8)), dtype=np.float32)
    H = Image.fromarray(np.abs(a[2:, 1:-1] - a[:-2, 1:-1]))
    V = Image.fromarray(np.abs(a[1:-1, 2:] - a[1:-1, :-2]))
    h, w = a.shape[0] - 2, a.shape[1] - 2
    yy, xx = np.mgrid[0:h, 0:w]
    rad = min(h, w) / 2 - 2
    disc = ((xx - w / 2) ** 2 + (yy - h / 2) ** 2) <= rad * rad  # every angle sees the same pixels

    # Half-step offsets: no candidate is exactly 0, where the lack of resampling blur would win unfairly.
    coarse = np.arange(-span + 0.25, span, 0.5)
    scores = np.array([_profile_score(H, V, disc, x) for x in coarse])
    i = int(scores.argmax())
    fine = np.arange(coarse[i] - 0.375, coarse[i] + 0.376, 0.125)
    fscores = np.array([_profile_score(H, V, disc, x) for x in fine])
    tilt = float(fine[int(fscores.argmax())])

    base = float(np.median(scores))
    peak = float(fscores.max())
    away = np.abs(coarse - coarse[i]) > 1.5
    runner_up = float(scores[away].max()) if away.any() else base
    # Confident only when the peak is both tall and alone.
    height = (peak - base) / (base + 1e-9)
    alone = (peak - runner_up) / (peak - base + 1e-9)
    conf = float(np.clip(height / 0.12, 0, 1) * np.clip(alone / 0.5, 0, 1))
    return round(-tilt, 2), round(conf, 2)


# ----------------------------------------------------------------------- tone


# Lookup tables: the sRGB curve is the slowest part of a render when computed per pixel.
_LIN = np.arange(256, dtype=np.float32) / 255.0
_LIN = np.where(_LIN <= 0.04045, _LIN / 12.92, ((_LIN + 0.055) / 1.055) ** 2.4).astype(np.float32)
_SRGB = np.arange(65536, dtype=np.float32) / 65535.0
_SRGB = np.where(_SRGB <= 0.0031308, _SRGB * 12.92, 1.055 * np.power(_SRGB, 1 / 2.4) - 0.055).astype(np.float32)


def _to_linear(u8: np.ndarray) -> np.ndarray:
    return _LIN[u8]


def _to_srgb(lin: np.ndarray) -> np.ndarray:
    return _SRGB[(np.clip(lin, 0.0, 1.0) * 65535.0 + 0.5).astype(np.uint16)]


def _luma(a: np.ndarray) -> np.ndarray:
    return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722


def _soft_mask(lum: np.ndarray) -> np.ndarray:
    """Blurred luminance: lets shadow and highlight moves follow regions instead of flattening texture."""
    h, w = lum.shape
    small = Image.fromarray((np.clip(lum, 0, 1) * 255).astype(np.uint8)).resize(
        (max(w // 8, 1), max(h // 8, 1)), Image.Resampling.BILINEAR
    )
    small = small.filter(ImageFilter.GaussianBlur(max(min(w, h) / 8 / 40, 1.0)))
    return np.asarray(small.resize((w, h), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0


def render(im: Image.Image, recipe: dict | None) -> Image.Image:
    """Apply a recipe to an upright RGB image and return a new image."""
    r = clean(recipe)
    if not r:
        return im
    icc = im.info.get("icc_profile")
    im = im.convert("RGB")
    if "rotate" in r:
        im = straighten(im, r["rotate"])
    if "crop" in r:
        im = im.crop(crop_box(im.size, r["crop"]))
    u8 = np.asarray(im)

    if "exposure" in r or "temperature" in r or "tint" in r:
        lin = _to_linear(u8)
        if "temperature" in r or "tint" in r:
            t, g = r.get("temperature", 0) / 100.0, r.get("tint", 0) / 100.0
            gains = np.array([1 + 0.22 * t + 0.08 * g, 1 - 0.14 * g, 1 - 0.22 * t + 0.08 * g], dtype=np.float32)
            before = _luma(lin)
            lin = lin * gains
            # A colour shift should not also make the picture brighter or darker.
            lin = lin * (before / np.maximum(_luma(lin), 1e-5))[..., None]
        if "exposure" in r:
            lin = lin * (2.0 ** r["exposure"])
            # Roll bright values off towards white instead of clipping them flat.
            knee = 0.8
            over = lin > knee
            lin = np.where(over, knee + (1 - knee) * np.tanh((lin - knee) / (1 - knee)), lin)
        a = _to_srgb(lin)
    else:
        a = u8.astype(np.float32) / 255.0

    if "highlights" in r or "shadows" in r:
        mask = _soft_mask(_luma(a))
        gain = np.ones_like(mask)
        if "shadows" in r:
            gain += (r["shadows"] / 100.0) * 0.9 * (1.0 - mask) ** 2.2
        if "highlights" in r:
            gain += (r["highlights"] / 100.0) * 0.45 * mask**2.2
        a = a * gain[..., None]

    if "contrast" in r:
        c = r["contrast"] / 100.0
        a = np.clip(a, 0.0, 1.0)
        if c > 0:
            s = a * a * (3.0 - 2.0 * a)  # smooth S-curve that keeps black and white where they are
            a = a + c * 1.1 * (s - a)
        else:
            a = a + (-c) * 0.6 * (0.5 - a)

    if "clarity" in r:
        lum = _luma(a)
        h, w = lum.shape
        blur = Image.fromarray((np.clip(lum, 0, 1) * 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(max(max(w, h) / 70.0, 2.0))
        )
        detail = lum - np.asarray(blur, dtype=np.float32) / 255.0
        mid = 1.0 - np.abs(lum - 0.5) * 2.0  # protect deep shadows and bright highlights
        a = a + (r["clarity"] / 100.0 * 0.9 * detail * (0.35 + 0.65 * mid))[..., None]

    if r.get("bw"):
        a = np.repeat(_luma(a)[..., None], 3, axis=2)
    elif "saturation" in r or "vibrance" in r:
        lum = _luma(a)[..., None]
        if "vibrance" in r:
            hi = np.maximum(np.maximum(a[..., 0], a[..., 1]), a[..., 2])
            lo = np.minimum(np.minimum(a[..., 0], a[..., 1]), a[..., 2])
            # Muted colours get most of the boost; already vivid ones are left nearly alone.
            a = lum + (a - lum) * (1.0 + (r["vibrance"] / 100.0) * 0.8 * (1.0 - np.clip(hi - lo, 0, 1)))[..., None]
        if "saturation" in r:
            a = lum + (a - lum) * (1.0 + r["saturation"] / 100.0)

    if "vignette" in r:
        h, w = a.shape[:2]
        x2 = ((np.arange(w, dtype=np.float32) - w / 2) / (w / 2)) ** 2
        y2 = ((np.arange(h, dtype=np.float32) - h / 2) / (h / 2)) ** 2
        d2 = np.clip((y2[:, None] + x2[None, :]) / 2.0, 0.0, 1.0)
        a = a * (1.0 + (r["vignette"] / 100.0) * 0.45 * d2**1.4)[..., None]

    out = Image.fromarray((np.clip(a, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8))
    if "sharpen" in r:
        radius = max(max(out.size) / 1600.0, 0.6)
        out = out.filter(ImageFilter.UnsharpMask(radius=radius, percent=int(r["sharpen"] * 1.6), threshold=2))
    if icc:
        out.info["icc_profile"] = icc  # the edit stays in the original's colour space
    return out


def tone_stats(im: Image.Image) -> dict:
    """Brightness, clipping, tonal spread and colourfulness, plus how far each is from a normal picture.

    `need` is what a plain auto-correct would do. It knows nothing about intent (a night scene
    is meant to be dark), so it only sets how far to go once a model has chosen the direction.
    """
    small = im.convert("RGB")
    small.thumbnail((512, 512))
    a = np.asarray(small, dtype=np.float32) / 255.0
    lum = _luma(a)
    mean = float(lum.mean())
    p5, p95 = (float(x) for x in np.percentile(lum, [5, 95]))
    chroma = float((a.max(axis=2) - a.min(axis=2)).mean())
    need: dict = {}
    # Only clear departures count. Colour is left out on purpose: a misty morning is meant to be muted.
    if mean < 0.36 or mean > 0.64:
        ev = 2.2 * math.log2(0.46 / max(mean, 0.02)) * 0.8  # stops to bring the average to a normal mid-tone
        need["exposure"] = round(min(max(ev, -1.5), 1.5), 2)
    if p95 - p5 < 0.45:
        need["contrast"] = round(min((0.7 - (p95 - p5)) * 160, 45))
    return {
        "brightness": round(mean * 100),
        "blown": round(float((lum >= 0.98).mean()) * 100, 1),
        "crushed": round(float((lum <= 0.02).mean()) * 100, 1),
        "spread": round((p95 - p5) * 100),
        "colour": round(chroma * 100),
        "need": need,
    }


def stats(im: Image.Image) -> dict:
    """Measurements that help a model (and the UI) reason about an edit."""
    tilt, conf = detect_tilt(im)
    return {**tone_stats(im), "tilt": tilt, "tilt_confidence": conf}


def side_by_side(before: Image.Image, after: Image.Image, height: int = 640) -> Image.Image:
    """Original left, edit right, with a divider. Any vision model can read one image."""
    def fit(im):
        w = max(int(im.width * height / im.height), 1)
        return im.convert("RGB").resize((w, height), Image.Resampling.LANCZOS)

    a, b = fit(before), fit(after)
    gap = 16
    out = Image.new("RGB", (a.width + b.width + gap, height), (255, 255, 255))
    out.paste(a, (0, 0))
    out.paste(b, (a.width + gap, 0))
    return out


def contact_sheet(tiles: list[Image.Image], numbers: list[int], cols: int = 4, cell: int = 384) -> Image.Image:
    """Numbered thumbnails on one canvas.

    One sheet costs an external model about as much as two or three single photos
    and shows it sixteen. Fine detail is lost, but composition, colour and impact
    are not, which is what a first cut is judged on.
    """
    from PIL import ImageDraw, ImageFont

    rows = (len(tiles) + cols - 1) // cols
    gap = 6
    sheet = Image.new("RGB", (cols * cell + (cols + 1) * gap, rows * cell + (rows + 1) * gap), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=max(cell // 11, 18))
    except TypeError:  # older Pillow: the built-in bitmap font only
        font = ImageFont.load_default()
    for i, (im, no) in enumerate(zip(tiles, numbers)):
        t = im.convert("RGB")
        t.thumbnail((cell, cell), Image.Resampling.LANCZOS)
        x = gap + (i % cols) * (cell + gap) + (cell - t.width) // 2
        y = gap + (i // cols) * (cell + gap) + (cell - t.height) // 2
        sheet.paste(t, (x, y))
        label = str(no)
        box = draw.textbbox((0, 0), label, font=font)
        w, h = box[2] - box[0] + 14, box[3] - box[1] + 12
        cx, cy = gap + (i % cols) * (cell + gap), gap + (i // cols) * (cell + gap)
        draw.rectangle([cx, cy, cx + w, cy + h], fill=(0, 0, 0))
        draw.text((cx + 7 - box[0], cy + 5 - box[1]), label, fill=(255, 255, 255), font=font)
    return sheet
