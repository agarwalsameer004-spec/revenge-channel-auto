"""
On-screen graphics for the corporate-collapse format.

Built from measured behaviour of the channels that win this niche, not taste:
  - ~46% of frames carry text; ~40% carry a number
  - roughly half of runtime is graphics, not footage
  - median shot ~1.4-3.2s; nothing sits unchanged for more than ~4s

Every card is rendered as a SEQUENCE of frames that accumulate one element at
a time. A finished slide is never shown all at once -- the build IS the motion,
which is what keeps a static graphic watchable without any video footage.
"""
import os
import random
import textwrap

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT = 1920, 1080

# Colour grammar, used consistently across every card:
#   green = money coming in / positive
#   red   = money owed / loss / the thing that kills them
#   amber = the phrase that damns them
CANVAS      = (18, 26, 33)      # the channel's constant background -- its "face"
CANVAS_EDGE = (11, 17, 22)
INK         = (17, 21, 26)
PAPER       = (247, 246, 242)
MUTED       = (120, 132, 145)
WHITE       = (255, 255, 255)
RED         = (214, 58, 48)
GREEN       = (46, 160, 90)
AMBER       = (247, 205, 70)

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
F_SANS       = os.path.join(FONT_DIR, "DejaVuSans.ttf")
F_SANS_BOLD  = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
F_COND_BOLD  = os.path.join(FONT_DIR, "DejaVuSansCondensed-Bold.ttf")
F_SERIF_BOLD = os.path.join(FONT_DIR, "DejaVuSerif-Bold.ttf")
F_MONO       = os.path.join(FONT_DIR, "DejaVuSansMono-Bold.ttf")

_FONT_CACHE = {}


def font(path, size):
    key = (path, size)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = ImageFont.truetype(path, size)
    return _FONT_CACHE[key]


def text_size(draw, s, f):
    box = draw.textbbox((0, 0), s, font=f)
    return box[2] - box[0], box[3] - box[1]


def canvas():
    """The constant background. One colour, every graphic beat, forever."""
    img = Image.new("RGB", (WIDTH, HEIGHT), CANVAS)
    d = ImageDraw.Draw(img)
    # A soft vignette stops a flat fill from looking like a broken render.
    for i in range(160):
        a = i / 160
        c = tuple(int(CANVAS[k] + (CANVAS_EDGE[k] - CANVAS[k]) * (1 - a)) for k in range(3))
        d.rectangle([i, i, WIDTH - i, HEIGHT - i], outline=c)
    return img


def wrap_to_width(draw, s, f, max_px):
    words, lines, cur = s.split(), [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if text_size(draw, cand, f)[0] > max_px and cur:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def rounded_shadow(base, box, radius, blur=18, alpha=110, offset=(0, 10)):
    """Drop shadow behind a card. Cheap, and it stops cards looking pasted-flat."""
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    x0, y0, x1, y1 = box
    sd.rounded_rectangle([x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]],
                         radius=radius, fill=(0, 0, 0, alpha))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(shadow) if base.mode == "RGBA" else \
        base.paste(Image.alpha_composite(base.convert("RGBA"), shadow).convert("RGB"), (0, 0))


# --------------------------------------------------------------------------- #
# 1. HEADLINE STACK  -- the hook. Zero footage for the first ~13 seconds.
# --------------------------------------------------------------------------- #

def _headline_card(headline, source, highlight, card_w, seed):
    """One news headline as a white card, key phrase marker-highlighted."""
    rng = random.Random(seed)
    f_head = font(F_SERIF_BOLD, 40)
    f_src = font(F_SANS, 24)

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    pad = 38
    lines = wrap_to_width(probe, headline, f_head, card_w - 2 * pad)
    line_h = 54
    card_h = pad + len(lines) * line_h + 14 + 30 + pad

    card = Image.new("RGBA", (card_w, card_h), PAPER + (255,))
    d = ImageDraw.Draw(card)

    # Highlight first so the marker sits BEHIND the text, like a real highlighter.
    if highlight:
        hl = highlight.lower()
        y = pad
        for ln in lines:
            low = ln.lower()
            idx = low.find(hl)
            if idx != -1:
                pre = ln[:idx]
                hit = ln[idx:idx + len(highlight)]
                x0 = pad + text_size(d, pre, f_head)[0]
                w = text_size(d, hit, f_head)[0]
                d.rounded_rectangle([x0 - 6, y - 2, x0 + w + 6, y + 46],
                                    radius=5, fill=AMBER)
            y += line_h

    y = pad
    for ln in lines:
        d.text((pad, y), ln, font=f_head, fill=INK)
        y += line_h

    d.line([pad, y + 8, card_w - pad, y + 8], fill=(214, 210, 200), width=2)
    d.text((pad, y + 20), source.upper(), font=f_src, fill=MUTED)

    return card.rotate(rng.uniform(-4.0, 4.0), expand=True,
                       resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0))


def headline_stack_frames(headlines, hold_frames=48):
    """headlines: list of {headline, source, highlight}.

    Returns [(PIL image, frames_to_hold)] -- one card lands roughly every 2s and
    NOTHING is ever cleared. The accumulating pile is the argument.

    Cards are laid out from their MEASURED heights so a later card only ever
    overlaps the previous card's source strip, never its headline text.
    """
    cards = []
    card_w = 980
    for i, h in enumerate(headlines[:5]):
        cards.append(_headline_card(h["headline"], h.get("source", ""),
                                    h.get("highlight"), card_w, seed=i * 17 + 3))

    OVERLAP = 26          # just enough to read as a pile, not enough to hide text
    MARGIN = 44

    # Auto-fit: rotation expands each card, so the stack can overflow 1080.
    # Scale the whole set down until it fits rather than letting cards fall off.
    def stack_height(cs):
        return sum(c.height for c in cs) - OVERLAP * (len(cs) - 1)

    avail = HEIGHT - 2 * MARGIN
    h = stack_height(cards)
    if h > avail:
        k = avail / h
        cards = [c.resize((max(1, int(c.width * k)), max(1, int(c.height * k))),
                          Image.LANCZOS) for c in cards]

    y = max(MARGIN, (HEIGHT - stack_height(cards)) // 2)
    placed = []
    for i, c in enumerate(cards):
        x = (WIDTH - c.width) // 2 + (i - len(cards) / 2) * 34
        placed.append((c, int(x), y))
        y += c.height - OVERLAP

    frames = []
    for i in range(len(placed)):
        img = canvas().convert("RGBA")
        for c, cx, cy in placed[:i + 1]:
            sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(sh).rounded_rectangle(
                [cx + 6, cy + 12, cx + c.width + 6, cy + c.height + 12],
                radius=14, fill=(0, 0, 0, 130))
            img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(18)))
            img.alpha_composite(c, (cx, cy))
        frames.append((img.convert("RGB"), hold_frames))
    return frames


# --------------------------------------------------------------------------- #
# 2. GIANT NUMBER  -- the figure written out in full, never abbreviated.
#    "$47,200,000,000" is a wall of digits. "$47.2B" is a shrug.
# --------------------------------------------------------------------------- #

def giant_number_frames(figure, caption="", sublabel="", colour=RED, hold_frames=40):
    f_big = font(F_COND_BOLD, 150)
    f_cap = font(F_SANS_BOLD, 46)
    f_sub = font(F_SANS, 30)

    steps = []
    for stage in (0, 1, 2):                    # caption -> number -> source
        img = canvas()
        d = ImageDraw.Draw(img)
        if caption:
            w, _ = text_size(d, caption.upper(), f_cap)
            d.text(((WIDTH - w) // 2, 300), caption.upper(), font=f_cap, fill=MUTED)
        if stage >= 1:
            fig = figure
            size = 150
            while text_size(d, fig, font(F_COND_BOLD, size))[0] > WIDTH - 200 and size > 60:
                size -= 6
            f_big = font(F_COND_BOLD, size)
            w, h = text_size(d, fig, f_big)
            d.text(((WIDTH - w) // 2, 430), fig, font=f_big, fill=colour)
            d.line([(WIDTH - w) // 2, 430 + h + 46, (WIDTH + w) // 2, 430 + h + 46],
                   fill=colour, width=6)
        if stage >= 2 and sublabel:
            w, _ = text_size(d, sublabel, f_sub)
            d.text(((WIDTH - w) // 2, 720), sublabel, font=f_sub, fill=MUTED)
        steps.append((img, hold_frames if stage == 2 else 14))
    return steps


# --------------------------------------------------------------------------- #
# 3. COMPARISON BARS  -- deliberately not real dataviz. Two or three bars,
#    no axes, no gridlines, no legend. One bar arrives at a time.
# --------------------------------------------------------------------------- #

def comparison_frames(title, items, hold_frames=44):
    """items: [{label, display, weight (0-1), colour}] -- 2 or 3 only."""
    f_title = font(F_SANS_BOLD, 52)
    f_val = font(F_COND_BOLD, 74)
    f_lab = font(F_SANS, 34)

    base_y, max_h = 830, 480
    slot = WIDTH // (len(items) + 1)
    frames = []

    for shown in range(1, len(items) + 1):
        img = canvas()
        d = ImageDraw.Draw(img)
        w, _ = text_size(d, title, f_title)
        d.text(((WIDTH - w) // 2, 150), title, font=f_title, fill=WHITE)
        d.line([160, base_y, WIDTH - 160, base_y], fill=(70, 82, 94), width=3)

        for i, it in enumerate(items[:shown]):
            cx = slot * (i + 1)
            bar_w = 230
            h = max(14, int(max_h * it["weight"]))
            col = it.get("colour", RED)
            d.rounded_rectangle([cx - bar_w // 2, base_y - h, cx + bar_w // 2, base_y],
                                radius=10, fill=col)
            vw, vh = text_size(d, it["display"], f_val)
            d.text((cx - vw // 2, base_y - h - vh - 34), it["display"], font=f_val, fill=col)
            for j, ln in enumerate(wrap_to_width(d, it["label"], f_lab, bar_w + 150)):
                lw, _ = text_size(d, ln, f_lab)
                d.text((cx - lw // 2, base_y + 26 + j * 44), ln, font=f_lab, fill=MUTED)
        frames.append((img, hold_frames if shown == len(items) else 20))
    return frames


# --------------------------------------------------------------------------- #
# 4. METRIC ROWS  -- built one row per beat, then a total. Never shown whole.
# --------------------------------------------------------------------------- #

def metric_rows_frames(title, rows, total=None, hold_frames=40):
    """rows: [{label, value, colour}]  total: {label, value, colour}"""
    f_title = font(F_SANS_BOLD, 50)
    f_lab = font(F_SANS, 40)
    f_val = font(F_MONO, 46)

    frames = []
    steps = len(rows) + (1 if total else 0)
    for shown in range(1, steps + 1):
        img = canvas()
        d = ImageDraw.Draw(img)
        d.text((240, 170), title, font=f_title, fill=WHITE)
        y = 320
        for r in rows[:min(shown, len(rows))]:
            d.text((240, y), r["label"], font=f_lab, fill=(206, 214, 222))
            vs = r["value"]
            vw, _ = text_size(d, vs, f_val)
            d.text((WIDTH - 240 - vw, y - 4), vs, font=f_val, fill=r.get("colour", WHITE))
            y += 92
        if total and shown > len(rows):
            d.line([240, y + 12, WIDTH - 240, y + 12], fill=(80, 92, 104), width=3)
            y += 46
            d.text((240, y), total["label"], font=f_lab, fill=WHITE)
            vw, _ = text_size(d, total["value"], f_val)
            d.text((WIDTH - 240 - vw, y - 4), total["value"], font=f_val,
                   fill=total.get("colour", RED))
        frames.append((img, hold_frames if shown == steps else 22))
    return frames
