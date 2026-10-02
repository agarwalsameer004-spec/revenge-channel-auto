"""
Thumbnail renderer for the corporate-collapse format.

The thumbnail is the only part of a video that competes before anyone has
watched a second of it, and at the size it is actually seen -- roughly 168x94
in a phone feed -- almost nothing survives. What survives is: one object, one
number, and three or four words at genuinely different sizes. A block of text
all set at one size reads as grey at that scale, which is what the previous
placeholder renderer produced.

Layout, fixed so every video in the channel looks like the same channel:
    kicker    small, grey, letter-spaced, top left
    number    the figure the video is about, very large, white
    lines     up to three short lines, the last one in red
    document  optional paper object bleeding off the right edge, with the
              line that matters circled -- the channel's whole thesis is that
              the paperwork said it first, so the paperwork should be visible

A script supplies this via an optional "thumb" object in scripts_queue.json.
With no "thumb" it falls back to the title alone, still laid out with a real
size hierarchy rather than one wall of type.
"""
import os
import re

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 720
MARGIN = 64

CANVAS = (18, 26, 33)
EDGE   = (11, 17, 22)
INK    = (17, 21, 26)
PAPER  = (243, 241, 235)
MUTED  = (128, 140, 152)
WHITE  = (255, 255, 255)
RED    = (214, 58, 48)

_FD = "/usr/share/fonts/truetype/dejavu"
F_BOLD = os.path.join(_FD, "DejaVuSans-Bold.ttf")
F_REG  = os.path.join(_FD, "DejaVuSans.ttf")

# Matches a figure INCLUDING its unit word, so pulling it out of a title does
# not leave the unit stranded behind ("$47 BILLION" -> not "BILLION").
_NUMLIKE = re.compile(
    r"[\u20ac$\u00a3]?\d[\d.,]*\s*(?:billion|bn|million|mn|m|trillion|tn|percent|%)?",
    re.I)


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _fit(draw, text, path, max_w, start, floor=34):
    """Largest size at which `text` still fits `max_w`."""
    size = start
    while size > floor:
        fo = _font(path, size)
        if draw.textlength(text, font=fo) <= max_w:
            return fo
        size -= 2
    return _font(path, floor)


def _background():
    base = Image.new("RGB", (W, H), CANVAS)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).ellipse([-W * 0.3, -H * 0.5, W * 1.3, H * 1.5], fill=90)
    return Image.composite(base, Image.new("RGB", (W, H), EDGE),
                           mask.filter(ImageFilter.GaussianBlur(120)))


def _document(img, spec):
    """Paper object, rotated and bled off the right edge. Returns text width."""
    dw, dh = 400, 520
    doc = Image.new("RGB", (dw, dh), PAPER)
    dd = ImageDraw.Draw(doc)
    heading = (spec.get("heading") or ["DOCUMENT"])[:2]
    y = 36
    for line in heading:
        dd.text((30, y), line.upper(), font=_font(F_BOLD, 26), fill=INK)
        y += 34
    for i, ly in enumerate(range(140, 300, 28)):
        dd.rectangle([30, ly, 30 + (330 if i % 3 else 210), ly + 8],
                     fill=(205, 203, 196))
    if spec.get("figure"):
        dd.text((30, 336), spec["figure"], font=_font(F_BOLD, 27), fill=INK)
    label = spec.get("sign_label") or "Authorised signatory"
    dd.text((30, 418), label, font=_font(F_REG, 21), fill=(120, 118, 112))
    dd.line([30, 456, 300, 456], fill=(150, 148, 142), width=3)

    doc = doc.rotate(-4, expand=True, fillcolor=CANVAS)
    img.paste(doc, (862, 118))
    if spec.get("circle", True):
        ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(ov).ellipse([878, 512, 1216, 606],
                                   outline=RED + (255,), width=10)
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    return img, 790          # text column stops before the paper


def _wrap(draw, words, path, max_w, size):
    """Greedy wrap at a given size. Returns the lines, never dropping a word."""
    font = _font(path, size)
    out, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if cur and draw.textlength(cand, font=font) > max_w:
            out.append(cur); cur = w
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def _fit_block(draw, items, path, max_w, max_h, start=108, floor=34,
               max_lines=4, rewrap=True):
    """Largest size at which the WHOLE text fits the box.

    Never truncates. Text that will not fit even at the floor size is rendered
    at the floor and allowed to be small -- a small complete sentence beats a
    large half of one, which is what the first version of this did.
    """
    size = start
    while size >= floor:
        got = _wrap(draw, items, path, max_w, size) if rewrap else list(items)
        fo = _font(path, size)
        widest = max((draw.textlength(l, font=fo) for l in got), default=0)
        if len(got) <= max_lines and widest <= max_w and len(got) * (size + 10) <= max_h:
            return fo, got
        size -= 2
    fo = _font(path, floor)
    return fo, (_wrap(draw, items, path, max_w, floor) if rewrap else list(items))


def _from_title(title):
    """No thumb spec: build a usable one out of the title alone.

    This is a safety net, not the plan. Every script should carry its own
    "thumb" block -- a title is a sentence, and a thumbnail is not.
    """
    text = title.upper().replace("\u2014", " ")
    number = None
    m = _NUMLIKE.search(text)
    if m and any(ch.isdigit() for ch in m.group(0)):
        number = m.group(0).strip()
        text = text[:m.start()] + " " + text[m.end():]
    return {"kicker": "", "number": number,
            "words": [w for w in text.split() if w]}



def render(title, out_path, spec=None):
    spec = dict(spec or _from_title(title))
    img = _background()
    text_w = W - MARGIN * 2

    if spec.get("document"):
        img, text_w = _document(img, spec["document"])
        text_w -= MARGIN

    d = ImageDraw.Draw(img)

    if spec.get("kicker"):
        d.text((MARGIN, 48), " ".join(spec["kicker"].upper()),
               font=_font(F_BOLD, 28), fill=MUTED)

    authored = [l.upper() for l in (spec.get("lines") or []) if l]
    words = spec.get("words") or []
    y = 150

    if spec.get("number"):
        nf = _fit(d, spec["number"], F_BOLD, text_w, 132, 56)
        d.text((MARGIN, y), spec["number"], font=nf, fill=WHITE)
        y += nf.size + 32

    body = authored or words
    if body:
        lf, lines = _fit_block(d, body, F_BOLD, text_w, (H - 40) - y,
                               start=108, floor=34,
                               max_lines=3 if authored else 4,
                               rewrap=not authored)
        step = lf.size + 10
        for i, line in enumerate(lines):
            colour = RED if i == len(lines) - 1 and len(lines) > 1 else WHITE
            d.text((MARGIN, y + i * step), line, font=lf, fill=colour)

    img.save(out_path, quality=94)
    return out_path
