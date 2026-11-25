"""Technical quality from pixels alone: sharpness, exposure, contrast, colour.

These are deliberately soft signals. A silhouette or a long exposure can be a
great photograph, so flags lower the funnel score but never remove a photo.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image

ANALYSIS_SIDE = 2048  # sharpness is measured near this size so focus blur stays visible


def _scale(x: float, lo: float, hi: float) -> float:
    return float(np.clip((x - lo) / (hi - lo), 0.0, 1.0) * 100.0)


def analyze(im: Image.Image) -> dict:
    """`im` is an upright RGB image, ideally about ANALYSIS_SIDE on its long side."""
    a = np.asarray(im, dtype=np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    h, w = lum.shape

    # Laplacian response, scored on the sharpest tiles so a shallow depth of
    # field (sharp subject, soft background) is not mistaken for a blurry shot.
    lap = lum[:-2, 1:-1] + lum[2:, 1:-1] + lum[1:-1, :-2] + lum[1:-1, 2:] - 4.0 * lum[1:-1, 1:-1]
    # Normalise to the analysis size: a smaller image looks sharper per pixel.
    size_gain = (max(h, w) / ANALYSIS_SIDE) ** 1.2 if max(h, w) < ANALYSIS_SIDE else 1.0
    grid = 8
    th, tw = max(lap.shape[0] // grid, 1), max(lap.shape[1] // grid, 1)
    tiles = lap[: th * grid, : tw * grid].reshape(grid, th, grid, tw).var(axis=(1, 3)).ravel()
    tiles.sort()
    peak = float(tiles[-6:].mean()) * size_gain  # mean of the six sharpest of 64 tiles
    sharp = _scale(math.log10(peak + 1.0), math.log10(12.0), math.log10(4000.0))

    mean = float(lum.mean())
    p1, p50, p99 = (float(x) for x in np.percentile(lum, [1, 50, 99]))
    clip_hi = float((lum >= 250).mean())
    clip_lo = float((lum <= 4).mean())
    std = float(lum.std())

    exposure = 100.0
    exposure -= min(clip_hi / 0.30, 1.0) * 55.0  # blown highlights
    if p99 < 150:  # nothing bright anywhere: plain underexposure, not a night scene
        exposure -= min((150 - p99) / 110.0, 1.0) * 60.0
    if mean > 205:
        exposure -= min((mean - 205) / 40.0, 1.0) * 30.0
    exposure = float(max(exposure, 0.0))

    contrast = _scale(std, 12.0, 62.0)

    rg, yb = r - g, 0.5 * (r + g) - b
    colorful = float(
        math.sqrt(float(rg.var()) + float(yb.var())) + 0.3 * math.sqrt(float(rg.mean()) ** 2 + float(yb.mean()) ** 2)
    )

    flags = []
    if clip_hi > 0.28 or mean > 228:
        flags.append("overexposed")
    if p99 < 95 and mean < 45:
        flags.append("underexposed")
    if std < 16 and (p99 - p1) < 70:
        flags.append("low_contrast")

    tech = 0.6 * sharp + 0.25 * exposure + 0.15 * contrast
    return {
        "sharp": round(sharp, 1),
        "exposure": round(exposure, 1),
        "contrast": round(contrast, 1),
        "colorful": round(_scale(colorful, 5.0, 90.0), 1),
        "tech": round(tech, 1),
        "flags": flags,
    }


def blur_flag(sharp: float, threshold: float) -> bool:
    return sharp < threshold
