"""
Segment-and-splice narration.

Why this exists
---------------
A TTS engine handed a whole beat decides the rhythm itself, and edge-tts
decides it the same way every time: even stress, even spacing, no held beat
before a punchline. Measured on our own output that reads as a Loudness Range
of ~3 LU against 6-12 LU for human documentary narration. Compression cannot
fix it -- compression REDUCES loudness range. The dynamics have to be put in
before the audio exists.

So narration is cut into segments, each segment is its own TTS request (edge-tts
exposes rate/volume/pitch per request, so each segment can be delivered
differently), and the segments are spliced back together with silence of an
exact authored length.

Authoring
---------
    The auditors signed it anyway. [[1.2]] Nobody checked the bank.

`[[1.2]]` is a 1.2-second hold. Everything else is ordinary prose.

Delivery is then shaped automatically: a short sentence following a long one is
the shape of a punchline in this format, and gets slowed and dropped in volume
-- the "lean in". That is what puts range back into the signal.
"""
import os
import re
import subprocess

SAMPLE_RATE = 24000          # edge-tts emits 24 kHz mono; stay there, no resample
BYTES_PER_SAMPLE = 2

PAUSE_RE = re.compile(r"\[\[\s*(\d+(?:\.\d+)?)\s*\]\]")

# Delivery presets. Values are what edge-tts accepts: a signed percentage.
# `rate` is applied by the engine. `gain_db` is applied by us on the decoded
# samples, because edge-tts's own volume percentage is small and imprecise and
# loudness range is the thing being fixed -- it has to be exact.
NEUTRAL = {"rate": None,   "pitch": None,   "gain_db": 0.0}
LEAN_IN = {"rate": "-14%", "pitch": "-9Hz",  "gain_db": -3.5}   # held: slower, lower, quieter
PUSH    = {"rate": "+7%",  "pitch": "+5Hz",  "gain_db": +1.5}   # a run of figures: forward

# Nothing a person says twice comes out identical. Flat segments get a small
# deterministic wobble -- seeded from the sentence text, so a re-render of the
# same script is byte-identical, but no two sentences sit at exactly the same
# level or speed. Without this the "flat" segments are machine-flat, and that
# uniformity is most of what reads as synthetic.
DRIFT_RATE_PCT = 3       # +/- this many percent
DRIFT_GAIN_DB = 0.8      # +/- this many dB
DRIFT_PITCH_HZ = 3       # +/- this many Hz

# Humans breathe at clause boundaries. The engine does not. These are spliced in
# afterwards at the measured word boundary, so they cost no extra TTS request.
CLAUSE_PAUSES = {",": 0.13, ";": 0.22, ":": 0.24, "\u2014": 0.26, "\u2013": 0.22}
CLAUSE_MIN_TAIL = 0.25   # never insert one this close to the end of a segment

MAX_PAUSE = 4.0              # an authored hold longer than this is a typo
MAX_SEGMENT_CHARS = 4000     # below every provider's per-request cap
LEAN_IN_MAX_WORDS = 8        # "Nobody checked the bank." is 4
HELD_BEFORE = 0.8            # an authored hold this long marks what follows
PUSH_MIN_WORDS = 12          # only a genuinely long sentence can be pushed
LONG_SETUP_WORDS = 14        # the sentence before it has to actually be long

AUTO_DELIVERY = os.environ.get("AUTO_DELIVERY", "1").strip() not in ("0", "false", "")


def strip_pauses(text):
    """The text as it should appear in captions: markers gone, spacing sane."""
    return re.sub(r"\s+", " ", PAUSE_RE.sub(" ", text)).strip()


# A terminator can be followed by a closing quote or bracket. Without this the
# trailing " of a quoted sentence becomes a "sentence" of its own, gets sent to
# the engine alone, and comes back as audio with no word timings at all.
_SENT_RE = re.compile(r"[^.!?]+[.!?]+[\"\u201d\u2019')\]]*\s*|[^.!?]+$")
_HAS_WORD = re.compile(r"\w")


def _sentences(text):
    out = []
    for s in _SENT_RE.findall(text):
        s = s.strip()
        if not s:
            continue
        if not _HAS_WORD.search(s):
            # Punctuation-only crumb: it belongs to the sentence before it.
            if out:
                out[-1] = (out[-1] + s).strip()
            continue
        out.append(s)
    return out


def plan_segments(text):
    """text -> [{"text": str, "gap_after": float, "delivery": dict}]

    Splits on authored [[pause]] marks, then on sentence boundaries so delivery
    can differ sentence to sentence. Sentences inside one authored span are not
    separated by silence -- gap_after is 0 between them -- so splitting for
    delivery never changes the timing.
    """
    chunks, gaps = [], []
    last = 0
    for m in PAUSE_RE.finditer(text):
        chunks.append(text[last:m.start()])
        gaps.append(min(float(m.group(1)), MAX_PAUSE))
        last = m.end()
    chunks.append(text[last:])
    gaps.append(0.0)

    segs = []
    for chunk, gap in zip(chunks, gaps):
        sents = _sentences(re.sub(r"\s+", " ", chunk).strip())
        if not sents:
            # An authored pause with nothing before it still has to hold.
            if segs and gap:
                segs[-1]["gap_after"] += gap
            continue
        for k, s in enumerate(sents):
            # Sentences are far below every provider's per-request cap, but a
            # script with no terminators at all would otherwise send the lot in
            # one request and be rejected. Split on commas as a last resort.
            pieces = [s] if len(s) <= MAX_SEGMENT_CHARS else _hard_split(s)
            for j, piece in enumerate(pieces):
                last = (k == len(sents) - 1) and (j == len(pieces) - 1)
                segs.append({"text": piece,
                             "gap_after": gap if last else 0.0,
                             "delivery": dict(NEUTRAL)})

    if AUTO_DELIVERY:
        _apply_auto_delivery(segs)
    return segs


_NUMISH = re.compile(r"[\d%$\u20ac\u00a3]|\b(?:billion|million|thousand|per\s?cent|percent)\b", re.I)


def _hard_split(text, limit=MAX_SEGMENT_CHARS):
    out, cur = [], ""
    for part in text.split(", "):
        if cur and len(cur) + len(part) + 2 > limit:
            out.append(cur); cur = part
        else:
            cur = (cur + ", " + part) if cur else part
    if cur:
        out.append(cur)
    return out


def _apply_auto_delivery(segs):
    """Give the script its dynamics back.

    Three structural signals, in priority order:
      1. The author held a beat before this line -> they are pointing at it.
      2. A short line after a long one -> the punchline shape of this format.
      3. A long line carrying several figures -> a run; push through it.
    Anything else is delivered flat, which is what makes the other three read.
    """
    for i, seg in enumerate(segs):
        words = seg["text"].split()
        n = len(words)
        held_before = i > 0 and segs[i - 1]["gap_after"] >= HELD_BEFORE
        short = n <= LEAN_IN_MAX_WORDS
        long_setup = i > 0 and len(segs[i - 1]["text"].split()) >= LONG_SETUP_WORDS

        if short and (held_before or long_setup):
            seg["delivery"] = dict(LEAN_IN)
        elif n >= PUSH_MIN_WORDS and len(_NUMISH.findall(seg["text"])) >= 2:
            seg["delivery"] = dict(PUSH)
        else:
            seg["delivery"] = _drift(seg["text"])


def _drift(text):
    """A reproducible wobble around neutral, keyed to the sentence itself."""
    import hashlib
    h = hashlib.sha256(text.encode("utf-8")).digest()
    span = lambda b, lim: ((h[b] / 255.0) * 2 - 1) * lim
    r = round(span(0, DRIFT_RATE_PCT))
    pz = round(span(1, DRIFT_PITCH_HZ))
    return {"rate": f"{r:+d}%" if r else None,
            "pitch": f"{pz:+d}Hz" if pz else None,
            "gain_db": round(span(2, DRIFT_GAIN_DB), 2)}


def decode_pcm(path):
    """mp3 -> raw mono s16le at SAMPLE_RATE. Exact sample counts, no guessing."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path,
         "-f", "s16le", "-acodec", "pcm_s16le",
         "-ar", str(SAMPLE_RATE), "-ac", "1", "-"],
        check=True, stdout=subprocess.PIPE, timeout=300).stdout
    return out


def trim_silence(pcm, threshold=600, pad_ms=30):
    """Strip the dead air edge-tts puts around every utterance.

    Returns (trimmed_pcm, lead_seconds_removed). The lead figure matters: word
    timings from the engine are relative to the UNtrimmed audio, so every
    timing in that segment has to move back by the same amount.
    """
    import array
    a = array.array("h")
    a.frombytes(pcm)
    n = len(a)
    start, end = 0, n
    while start < n and abs(a[start]) < threshold:
        start += 1
    while end > start and abs(a[end - 1]) < threshold:
        end -= 1
    if start >= end:
        return pcm, 0.0
    pad = int(pad_ms * SAMPLE_RATE / 1000)
    start = max(0, start - pad)
    end = min(n, end + pad)
    return a[start:end].tobytes(), start / SAMPLE_RATE


def breathe(text, pcm, align, lead):
    """Splice a short rest in at every clause boundary.

    The engine runs commas straight through. A reader does not. Because word
    timings are already in hand, the rest can be dropped in at the measured
    boundary and the timings after it shifted by the same amount -- so this
    costs nothing but the silence itself, and captions stay on the word.

    Returns (pcm, alignment_shifted_and_lead_already_applied, 0.0).
    """
    if not align:
        return pcm, align, lead
    chars = align["characters"]
    starts = [max(0.0, t - lead) for t in align["character_start_times_seconds"]]
    ends = [max(0.0, t - lead) for t in align["character_end_times_seconds"]]
    total = len(pcm) / BYTES_PER_SAMPLE / SAMPLE_RATE

    cuts = []
    for i, ch in enumerate(chars):
        hold = CLAUSE_PAUSES.get(ch)
        if not hold or i >= len(ends):
            continue
        t = ends[i]
        if t <= 0.05 or t >= total - CLAUSE_MIN_TAIL:
            continue
        cuts.append((t, hold))
    if not cuts:
        return pcm, {"characters": chars,
                     "character_start_times_seconds": starts,
                     "character_end_times_seconds": ends}, 0.0

    cuts.sort()
    out, prev_sample, added = [], 0, 0.0
    for t, hold in cuts:
        k = int(round(t * SAMPLE_RATE)) * BYTES_PER_SAMPLE
        k = max(prev_sample, min(k, len(pcm)))
        out.append(pcm[prev_sample:k])
        out.append(silence_pcm(hold))
        prev_sample = k
    out.append(pcm[prev_sample:])

    def shift(t):
        return t + sum(h for ct, h in cuts if ct <= t)

    return (b"".join(out),
            {"characters": chars,
             "character_start_times_seconds": [shift(t) for t in starts],
             "character_end_times_seconds": [shift(t) for t in ends]},
            0.0)


def apply_gain(pcm, gain_db):
    """Exact per-segment level change on the decoded samples."""
    if not gain_db:
        return pcm
    import array
    factor = 10 ** (gain_db / 20.0)
    a = array.array("h")
    a.frombytes(pcm)
    for i in range(len(a)):
        v = int(a[i] * factor)
        a[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    return a.tobytes()


def silence_pcm(seconds):
    return b"\x00" * (int(round(seconds * SAMPLE_RATE)) * BYTES_PER_SAMPLE)


def encode_mp3(pcm, out_path, bitrate="64k"):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
         "-c:a", "libmp3lame", "-b:a", bitrate, out_path],
        input=pcm, check=True, timeout=600)


def merge_alignments(pieces):
    """pieces: [(alignment_or_None, text, offset_seconds, lead_removed)]

    Returns one alignment covering the spliced audio, in the character shape
    the caption builder already consumes.
    """
    chars, starts, ends = [], [], []
    for align, text, offset, lead in pieces:
        if not align:
            return None
        c = align["characters"]
        s = align["character_start_times_seconds"]
        e = align["character_end_times_seconds"]
        m = min(len(c), len(s), len(e))
        for k in range(m):
            chars.append(c[k])
            starts.append(max(0.0, s[k] - lead) + offset)
            ends.append(max(0.0, e[k] - lead) + offset)
        chars.append(" "); starts.append(ends[-1]); ends.append(ends[-1])
    return {"characters": chars,
            "character_start_times_seconds": starts,
            "character_end_times_seconds": ends}


def synthesise(text, out_path, synth_fn, work_dir=None, log=print):
    """Synthesise one beat as segments, splice, return (alignment, clean_text).

    synth_fn(text, path, rate=..., volume=...) -> alignment-or-None, and must
    write an mp3 at `path`. Everything timing-related is derived from decoded
    sample counts rather than ffprobe, so the audio and the word timings can
    never disagree.
    """
    work_dir = work_dir or os.path.dirname(os.path.abspath(out_path))
    segs = plan_segments(text)
    base = os.path.splitext(os.path.basename(out_path))[0]

    pcm_parts, pieces, cursor = [], [], 0.0
    for i, seg in enumerate(segs):
        seg_path = os.path.join(work_dir, f"{base}__s{i:03d}.mp3")
        align = synth_fn(seg["text"], seg_path,
                         rate=seg["delivery"]["rate"],
                         pitch=seg["delivery"].get("pitch"))
        pcm, lead = trim_silence(decode_pcm(seg_path))
        pcm = apply_gain(pcm, seg["delivery"].get("gain_db", 0.0))
        pcm, align, lead = breathe(seg["text"], pcm, align, lead)
        dur = len(pcm) / BYTES_PER_SAMPLE / SAMPLE_RATE
        pieces.append((align, seg["text"], cursor, lead))
        pcm_parts.append(pcm)
        cursor += dur
        if seg["gap_after"] > 0:
            pcm_parts.append(silence_pcm(seg["gap_after"]))
            cursor += seg["gap_after"]
        try:
            os.remove(seg_path)
        except OSError:
            pass

    encode_mp3(b"".join(pcm_parts), out_path)
    held = sum(s["gap_after"] for s in segs)
    leaned = sum(1 for s in segs if s["delivery"] in (LEAN_IN, PUSH)
                 or s["delivery"].get("gain_db") in (-3.5, 1.5))
    log(f"    {len(segs)} segments, {held:.1f}s held, {leaned} leaned in")
    return merge_alignments(pieces), strip_pauses(text), cursor
