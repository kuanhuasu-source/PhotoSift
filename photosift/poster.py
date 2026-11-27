"""An exhibition poster: every work on one sheet, under the series title.

The works are laid out the way a gallery wall is: in hanging order, in rows
that each fill the full width, every picture at its own shape. Landscape and
upright works can sit in the same row without gaps or cropping.

Text needs a font with Chinese glyphs, and Pillow ships none, so one is looked
for on the system. If none is found the poster is still made, with the
pictures only, and the caller is told.
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

from PIL import Image, ImageCms, ImageDraw, ImageFont

A3 = (3508, 4961)  # 297 x 420 mm at 300 dpi
STYLES = {
    "light": {"paper": (255, 255, 255), "ink": (24, 24, 24), "muted": (112, 112, 112)},
    "dark": {"paper": (22, 22, 22), "ink": (245, 245, 245), "muted": (168, 168, 168)},
}
FONTS = {
    "darwin": ["/System/Library/Fonts/PingFang.ttc", "/System/Library/AssetsV2/com_apple_MobileAsset_Font*/*/AssetData/PingFang.ttc",
               "/System/Library/Fonts/STHeiti Medium.ttc", "/System/Library/Fonts/STHeiti Light.ttc",
               "/System/Library/Fonts/Hiragino Sans GB.ttc", "/Library/Fonts/Arial Unicode.ttf",
               "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"],
    "win32": ["C:/Windows/Fonts/msjh.ttc", "C:/Windows/Fonts/msjhbd.ttc", "C:/Windows/Fonts/mingliu.ttc"],
    "linux": ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
              "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
              "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "/usr/share/fonts/truetype/arphic/uming.ttc"],
}  # fmt: skip
HEAVY = ("semibold", "bold", "medium", "heavy", "black")
NO_LINE_START = "，。、；：！？）」』》〉…"
_found: dict = {}


def _faces(path: str) -> list[tuple[int, str, str]]:
    """Every face in a font file: (index, family, style). A .ttc holds several."""
    out = []
    for i in range(40):
        try:
            family, style = ImageFont.truetype(path, 20, index=i).getname()
        except (OSError, ValueError):
            break
        out.append((i, family or "", style or ""))
    return out


def find_fonts() -> tuple[tuple[str, int] | None, tuple[str, int] | None]:
    """(regular, heavy) as (file, face index), or None where the system has nothing with Chinese glyphs."""
    if "fonts" in _found:
        return _found["fonts"]
    paths = [os.environ["PHOTOSIFT_FONT"]] if os.environ.get("PHOTOSIFT_FONT") else []
    for pattern in FONTS.get(sys.platform, FONTS["linux"]):
        paths += sorted(glob.glob(pattern)) if "*" in pattern else [pattern]
    regular = heavy = None
    for path in paths:
        if not Path(path).exists():
            continue
        faces = _faces(path)
        if not faces:
            continue
        # Prefer the Traditional Chinese face of a collection; any of them has the glyphs.
        faces.sort(key=lambda f: 0 if (" TC" in f[1] or "Traditional" in f[1] or "JhengHei" in f[1]) else 1)
        if regular is None:
            pick = next((f for f in faces if f[2].lower() in ("regular", "light", "normal", "book")), faces[0])
            regular = (path, pick[0])
        if heavy is None:
            pick = next((f for f in faces if any(h in f[2].lower() for h in HEAVY)), None)
            if pick:
                heavy = (path, pick[0])
        if regular and heavy:
            break
    _found["fonts"] = (regular, heavy or regular)
    return _found["fonts"]


def _font(which, size: float):
    return ImageFont.truetype(which[0], max(int(round(size)), 6), index=which[1])


def split_rows(aspects: list[float], rows: int) -> list[list[int]]:
    """Consecutive groups whose total widths are as even as they can be, keeping the order."""
    n = len(aspects)
    rows = max(1, min(rows, n))
    prefix = [0.0]
    for a in aspects:
        prefix.append(prefix[-1] + a)
    inf = float("inf")
    best = [[inf] * (rows + 1) for _ in range(n + 1)]
    cut = [[0] * (rows + 1) for _ in range(n + 1)]
    best[0][0] = 0.0
    for i in range(1, n + 1):
        for k in range(1, min(i, rows) + 1):
            for j in range(k - 1, i):
                cost = max(best[j][k - 1], prefix[i] - prefix[j])  # the widest row decides how small everything gets
                if cost < best[i][k]:
                    best[i][k], cut[i][k] = cost, j
    out, i = [], n
    for k in range(rows, 0, -1):
        j = cut[i][k]
        out.append(list(range(j, i)))
        i = j
    return out[::-1]


def layout(aspects: list[float], width: float, height: float, gap: float) -> list[list[tuple[int, float, float, float, float]]]:
    """Rows of (index, x, y, w, h) inside a width x height area, arranged so the pictures are as large as they can be.

    More rows means larger pictures until the rows no longer fit the height. The choice is between the last
    arrangement that fits at full width and the next one shrunk to fit. Full width lines up with the text
    above it, so the shrunk one is only taken when it shows the pictures clearly larger.
    """
    if not aspects:
        return []
    full = shrunk = None
    for r in range(1, len(aspects) + 1):
        groups = split_rows(aspects, r)
        heights = [(width - gap * (len(g) - 1)) / sum(aspects[i] for i in g) for g in groups]
        total = sum(heights) + gap * (r - 1)
        if total <= height:
            full = (total, groups, heights, total, 1.0)
        else:
            k = height / total
            shrunk = (total * k * k, groups, heights, total, k)
            break  # further rows only get smaller from here
    best = full if full and (shrunk is None or shrunk[0] < full[0] * 1.12) else shrunk
    _, groups, heights, total, shrink = best
    y = (height - total * shrink) / 2
    out = []
    for g, h in zip(groups, heights):
        h *= shrink
        row_w = sum(aspects[i] * h for i in g) + gap * shrink * (len(g) - 1)
        x = (width - row_w) / 2
        row = []
        for i in g:
            w = aspects[i] * h
            row.append((i, x, y, w, h))
            x += w + gap * shrink
        out.append(row)
        y += h + gap * shrink
    return out


def wrap(draw: ImageDraw.ImageDraw, text: str, font, width: float, max_lines: int) -> list[str]:
    """Breaks text at any character, as Chinese is set, without starting a line on a closing mark."""
    lines, cur = [], ""
    for para in str(text).replace("\r", "").split("\n"):
        for ch in para:
            if cur and draw.textlength(cur + ch, font=font) > width and ch not in NO_LINE_START:
                lines.append(cur)
                cur = ch.lstrip()
            else:
                cur += ch
        if cur:
            lines.append(cur)
            cur = ""
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def render(images: list, *, title: str, exhibition: str = "", author: str = "", statement: str = "", note: str = "",
           style: str = "light", scale: float = 1.0) -> tuple[Image.Image, dict]:  # fmt: skip
    """The poster as an image. `images` are file paths or PIL images, in hanging order; `scale` 1.0 is A3 at 300 dpi."""
    pal = STYLES.get(style, STYLES["light"])
    W, H = int(A3[0] * scale), int(A3[1] * scale)
    u = scale  # every measurement below is in A3 pixels
    canvas = Image.new("RGB", (W, H), pal["paper"])
    draw = ImageDraw.Draw(canvas)
    margin, gap = 230 * u, 34 * u
    inner = W - 2 * margin
    regular, heavy = find_fonts()
    y = margin
    bottom = H - margin
    if regular:
        if exhibition:
            f = _font(regular, 66 * u)
            draw.text((margin, y), exhibition, font=f, fill=pal["muted"])
            y += 66 * u * 1.75
        size = 300 * u
        f = _font(heavy, size)
        while title and draw.textlength(title, font=f) > inner and size > 90 * u:
            size *= 0.94
            f = _font(heavy, size)
        if title:
            draw.text((margin, y), title, font=f, fill=pal["ink"], stroke_width=0 if heavy != regular else max(int(size / 60), 1), stroke_fill=pal["ink"])
            y += size * 1.3
        line = "　".join(x for x in (author, note) if x)
        if line:
            f = _font(regular, 78 * u)
            parts = wrap(draw, line, f, inner, 2)
            for t in parts:
                draw.text((margin, y), t, font=f, fill=pal["ink"])
                y += 78 * u * 1.5
        y += 70 * u
        if statement:
            f = _font(regular, 52 * u)
            lines = wrap(draw, statement, f, inner * 0.82, 5)
            block = len(lines) * 52 * u * 1.7
            ty = bottom - block
            for t in lines:
                draw.text((margin, ty), t, font=f, fill=pal["muted"])
                ty += 52 * u * 1.7
            bottom -= block + 90 * u
    opened = []
    for im in images:
        if isinstance(im, (str, Path)):
            im = Image.open(im)
            im.draft("RGB", (int(1600 * max(scale, 0.4)), int(1600 * max(scale, 0.4))))  # decode no larger than needed
        opened.append(im)
    aspects = [im.width / im.height for im in opened]
    rows = layout(aspects, inner, max(bottom - y, 200 * u), gap)
    for row in rows:
        for i, x, ry, w, h in row:
            tile = opened[i].convert("RGB").resize((max(int(round(w)), 1), max(int(round(h)), 1)), Image.Resampling.LANCZOS)
            canvas.paste(tile, (int(round(margin + x)), int(round(y + ry))))
    return canvas, {"text": bool(regular), "rows": len(rows), "size": [W, H], "per_row": [len(r) for r in rows]}


def build(jpg: Path, pdf: Path | None, images: list, **kw) -> dict:
    """Writes the poster for print: a JPEG, and a one-page PDF of the same sheet."""
    canvas, info = render(images, **kw)
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    canvas.save(jpg, "JPEG", quality=93, subsampling=0, dpi=(300, 300), icc_profile=srgb)
    if pdf is not None:
        canvas.save(pdf, "PDF", resolution=300.0)
    return info
