"""The caption cards and the biography as a slide deck.

One slide per work, photo on the left and the words for its caption card on
the right, so the organiser can match text to picture and edit it while
proofreading. A cover, the biography with every work in hanging order, and a
divider per chapter come first.

The text stays ordinary editable text boxes. Latin text is set in Calibri,
which Office has everywhere; Chinese in Microsoft JhengHei, which PowerPoint
replaces with the system's own face on a Mac.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

W, H, M = 13.333, 7.5, 0.6          # slide and margin, inches
INK, MUTED, PAPER = "212121", "6B6B6B", "FFFFFF"
NIGHT, MIST = "1E1E1E", "C9C9C9"    # the dark slides and the quieter text on them
LATIN, EAST = "Calibri", "Microsoft JhengHei"


def _font(run, size: float, bold: bool = False, color: str = INK) -> None:
    f = run.font
    f.size, f.bold, f.name = Pt(size), bold, LATIN
    f.color.rgb = RGBColor.from_string(color)
    rpr = run._r.get_or_add_rPr()
    for old in rpr.findall(qn("a:ea")):
        rpr.remove(old)
    ea = rpr.makeelement(qn("a:ea"), {"typeface": EAST})
    latin = rpr.find(qn("a:latin"))
    latin.addnext(ea) if latin is not None else rpr.append(ea)  # the schema wants ea straight after latin


def _box(slide, name: str, x: float, y: float, w: float, h: float, paras: list[tuple], anchor=MSO_ANCHOR.TOP, shrink: bool = False):
    """A text box. paras: (text, size, bold, colour, space after in points)."""
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    box.name = name
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    if shrink:
        tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE  # a safety net for unusually long text; normal text already fits
    first = True
    for text, size, bold, color, after in paras:
        for line in str(text).split("\n"):
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            p.alignment = PP_ALIGN.LEFT
            p.space_after = Pt(after)
            p.line_spacing = 1.15
            _font(p.add_run(), size, bold, color)
            p.runs[0].text = line
    return box


def _title(slide, text: str, x: float, y: float, w: float, h: float, size: float, color: str = INK, anchor=MSO_ANCHOR.TOP) -> None:
    """Fills the layout's title placeholder, so the slide has a real title for outlines and screen readers."""
    t = slide.shapes.title
    t.left, t.top, t.width, t.height = Inches(x), Inches(y), Inches(w), Inches(h)
    tf = t.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    _font(p.add_run(), size, True, color)
    p.runs[0].text = text


def _background(slide, color: str) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = RGBColor.from_string(color)


def _picture(slide, data: bytes, x: float, y: float, w: float, h: float, alt: str, cover: bool = False):
    """Places a picture inside a box, whole and centred; or, with `cover`, filling the box and cropped to it."""
    with Image.open(io.BytesIO(data)) as im:
        iw, ih = im.size
    box, pic = w / h, iw / ih
    if cover:
        shape = slide.shapes.add_picture(io.BytesIO(data), Inches(x), Inches(y), Inches(w), Inches(h))
        if pic > box:
            shape.crop_left = shape.crop_right = (1 - box / pic) / 2
        else:
            shape.crop_top = shape.crop_bottom = (1 - pic / box) / 2
    else:
        pw, ph = (w, w / pic) if pic > box else (h * pic, h)
        shape = slide.shapes.add_picture(io.BytesIO(data), Inches(x + (w - pw) / 2), Inches(y + (h - ph) / 2), Inches(pw), Inches(ph))
    shape._element.xpath("./p:nvPicPr/p:cNvPr")[0].set("descr", alt)
    return shape


def _grid(slide, items: list[dict], x: float, y: float, w: float, h: float, label_color: str) -> None:
    """Every work as a small numbered picture, in hanging order."""
    n = len(items)
    if not n:
        return
    cols = next(c for c in range(2, 13) if -(-n // c) * ((w - (c - 1) * 0.12) / c / 1.5 + 0.34) - 0.12 <= h or c == 12)
    cw = (w - (cols - 1) * 0.12) / cols
    ch = cw / 1.5
    for i, s in enumerate(items):
        cx, cy = x + (i % cols) * (cw + 0.12), y + (i // cols) * (ch + 0.34)
        if cy + ch > y + h + 0.01:
            break
        _picture(slide, s["image"], cx, cy, cw, ch, s["title"])
        _box(slide, f"編號 {s['no']}", cx, cy + ch + 0.03, cw, 0.2, [(f"{s['no']:02d}", 10, False, label_color, 0)])


def build(path: Path, *, exhibition: str, series: str, author: str, statement: str, bio: str, chapters: list[dict],
          slides: list[dict]) -> None:  # fmt: skip
    """slides: [{no, title, gear, author, size, description, chapter (index), image (JPEG bytes)}] in hanging order."""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(W), Inches(H)
    title_only, cover_layout = prs.slide_layouts[5], prs.slide_layouts[5]
    prs.core_properties.title = f"{exhibition}　{series}".strip()
    prs.core_properties.author = author
    total = len(slides)

    # ------------------------------------------------------------------ cover
    s = prs.slides.add_slide(cover_layout)
    _background(s, NIGHT)
    if slides:
        _picture(s, slides[0]["image"], W * 0.46, 0, W * 0.54, H, slides[0]["title"], cover=True)
    _title(s, series or exhibition, M, 2.3, W * 0.46 - M - 0.4, 1.9, 44 if len(series or exhibition) <= 8 else 36, PAPER, MSO_ANCHOR.BOTTOM)
    _box(s, "展覽與創作者", M, 4.45, W * 0.46 - M - 0.4, 1.4,
         [(exhibition, 20, False, MIST, 6), (author, 20, True, PAPER, 6), (f"{total} 件作品", 14, False, MIST, 0)])  # fmt: skip

    # -------------------------------------------------------------- biography
    s = prs.slides.add_slide(title_only)
    _background(s, PAPER)
    _title(s, "個人簡介", M, 0.55, 6.2, 0.8, 36)
    left = [(bio or "（尚未撰寫）", 16, False, INK, 16)]
    if statement:
        left += [("創作自述", 20, True, INK, 8), (statement, 16, False, INK, 0)]
    _box(s, "簡介與自述", M, 1.6, 6.2, 5.3, left, shrink=True)
    _box(s, "作品一覽標題", 7.5, 0.72, W - 7.5 - M, 0.4, [("作品一覽（依展出順序）", 14, False, MUTED, 0)])
    _grid(s, slides, 7.5, 1.6, W - 7.5 - M, 5.3, MUTED)

    # ---------------------------------------------------- chapters and works
    current = None
    for it in slides:
        ch = it.get("chapter")
        if chapters and len(chapters) > 1 and ch != current and ch is not None and ch < len(chapters):
            current = ch
            group = [x for x in slides if x.get("chapter") == ch]
            d = prs.slides.add_slide(title_only)
            _background(d, NIGHT)
            _box(d, "章節編號", M, 2.35, 5.2, 0.4, [(f"第 {ch + 1} 章　{len(group)} 件作品", 14, False, MIST, 0)])
            _title(d, chapters[ch]["title"], M, 2.8, 5.2, 1.0, 40, PAPER)
            if chapters[ch].get("idea"):
                _box(d, "章節說明", M, 3.95, 5.2, 2.4, [(chapters[ch]["idea"], 18, False, MIST, 0)], shrink=True)
            _grid(d, group, 6.6, 1.2, W - 6.6 - M, 5.4, MIST)

        w = prs.slides.add_slide(title_only)
        _background(w, PAPER)
        _picture(w, it["image"], M, 0.75, 7.6, 6.0, it["title"])
        x, cw = 8.75, W - 8.75 - M
        where = f"{it['no']:02d} / {total:02d}"
        if chapters and ch is not None and ch < len(chapters) and len(chapters) > 1:
            where += f"　{chapters[ch]['title']}"
        _box(w, "編號與章節", x, 0.8, cw, 0.3, [(where, 12, False, MUTED, 0)])
        _title(w, it["title"], x, 1.2, cw, 1.15, 32 if len(it["title"]) <= 8 else 26)
        rows = []
        for label, value in (("攝影設備", it.get("gear")), ("創作者", it.get("author")), ("尺寸", it.get("size"))):
            rows += [(label, 11, False, MUTED, 1), (value or "—", 14, False, INK, 9)]
        _box(w, "作品資料", x, 2.5, cw, 2.2, rows)
        desc = it.get("description") or "（尚未撰寫）"
        _box(w, "作品說明", x, 4.75, cw, 2.0, [("作品說明", 11, False, MUTED, 3), (desc, 16 if len(desc) <= 70 else 14, False, INK, 0)], shrink=True)
    prs.save(str(path))
