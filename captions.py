"""
Caption system.

The old style was a transcript: 42-character lines of plain white text, timed
proportionally. Transcript captions are something you read *instead of*
watching. These are something you watch.

Rules, taken from what the channels that win this niche actually do:
  - 3-4 words per card, never a sentence
  - large, heavy, high-contrast -- legible on a phone at arm's length
  - the NUMBER is picked out in amber; everything else is white
  - the card changes on the word, using real word timings from the narration
"""
import re

WIDTH, HEIGHT = 1920, 1080

MAX_WORDS = 4
MIN_CARD = 0.45          # never flash a card shorter than this
MAX_CARD = 2.20          # never hold one longer than this

# ASS is &HBBGGRR, not RGB.
C_WHITE = "&H00FFFFFF"
C_AMBER = "&H0046CDF7"   # the amber from the card system
C_SHADOW = "&H90000000"

_NUMERIC = re.compile(r"[\d$£€%]")


def is_number_word(w):
    """Numbers, money and percentages get the accent colour."""
    return bool(_NUMERIC.search(w))


def group_words(words):
    """words: [(text, start, end)] -> caption cards of <=4 words.

    Breaks on sentence punctuation so a card never straddles a full stop, and
    splits on long gaps so a pause in the narration becomes a card boundary
    rather than being papered over.
    """
    cards, cur = [], []
    for i, (w, s, e) in enumerate(words):
        cur.append((w, s, e))
        ends_clause = w.endswith((".", "?", "!", ":", ";", ","))
        gap_next = (words[i + 1][1] - e) if i + 1 < len(words) else 0
        if len(cur) >= MAX_WORDS or ends_clause or gap_next > 0.35 or i == len(words) - 1:
            cards.append(cur)
            cur = []
    if cur:
        cards.append(cur)
    return cards


def _esc(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


def card_markup(card):
    """Render one card, colouring any word that carries a figure."""
    out = []
    for w, _, _ in card:
        if is_number_word(w):
            out.append(f"{{\\c{C_AMBER}}}{_esc(w)}{{\\c{C_WHITE}}}")
        else:
            out.append(_esc(w))
    return " ".join(out)


def ass_time(t):
    t = max(0.0, float(t))
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


ASS_HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,92,{C_WHITE},&H00101010,{C_SHADOW},-1,0,1,7,4,2,160,160,165,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def write_ass(cards, path):
    """cards: [[(word, start, end), ...], ...]"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(ASS_HEADER)
        for card in cards:
            if not card:
                continue
            start = card[0][1]
            end = max(card[-1][2], start + MIN_CARD)
            end = min(end, start + MAX_CARD)
            # A short pop on entry reads as "landing on the word" rather than
            # a subtitle fading in.
            fx = "{\\fad(60,60)}"
            f.write(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,{fx}{card_markup(card)}\n")
    return path


def words_from_alignment(text, align):
    """Turn the character alignment the narration returns into word spans."""
    if not align:
        return None
    chars = align.get("characters") or []
    starts = align.get("character_start_times_seconds") or []
    ends = align.get("character_end_times_seconds") or []
    n = min(len(chars), len(starts), len(ends))
    if not n:
        return None
    joined = "".join(chars[:n])
    words, cursor = [], 0
    for w in text.split():
        idx = joined.find(w, cursor)
        if idx == -1 or idx >= n:
            continue
        j = min(idx + len(w) - 1, n - 1)
        words.append((w, starts[idx], ends[j]))
        cursor = idx + len(w)
    return words or None
