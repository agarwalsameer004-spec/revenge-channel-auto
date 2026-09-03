"""
Fully automated long-form video pipeline.

Flow: pick next script -> AI narration (with word timings) -> story-synced stock
footage -> Ken Burns motion -> burned-in captions -> thumbnail -> YouTube upload.

Runs unattended via GitHub Actions (.github/workflows/publish.yml).

Queue format (scripts_queue.json) is BEAT-BASED so footage can follow the story
instead of cycling keywords blindly:

  {
    "id": 1,
    "title": "...",
    "description": "...",
    "used": false,
    "beats": [
      {"text": "narration for this beat", "keywords": ["office night", "city"]},
      ...
    ]
  }
"""
import json
import os
import random
import subprocess
import textwrap

import requests
from PIL import Image, ImageDraw, ImageFont

# Narration provider. "google" is the default: Google Cloud TTS gives 1,000,000
# free characters/month against our ~91k usage, versus ElevenLabs Free at 10,000
# which cannot finish even one long-form video. Switch back with TTS_PROVIDER.
TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "google").strip().lower()

GOOGLE_TTS_API_KEY = os.environ.get("GOOGLE_TTS_API_KEY", "").strip()
GOOGLE_VOICE_NAME = os.environ.get("GOOGLE_VOICE_NAME", "").strip()

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID")  # optional override
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"].strip()

QUEUE_FILE = "scripts_queue.json"
OUTPUT_DIR = "output"

WIDTH, HEIGHT, FPS = 1920, 1080, 25
SECONDS_PER_CLIP = 6.0          # visual change cadence
CAPTIONS = os.environ.get("CAPTIONS", "1") != "0"
MUSIC = os.environ.get("MUSIC", "1") != "0"
MUSIC_GAIN_DB = -26             # bed sits well under the narration


# --------------------------------------------------------------------------- #
# queue
# --------------------------------------------------------------------------- #

def load_next_script():
    with open(QUEUE_FILE) as f:
        queue = json.load(f)
    for item in queue:
        if not item.get("used"):
            return item, queue
    raise SystemExit("No unused scripts left in scripts_queue.json — add more.")


def mark_used(item, queue):
    for q in queue:
        if q["id"] == item["id"]:
            q["used"] = True
    with open(QUEUE_FILE, "w") as f:
        json.dump(queue, f, indent=2)


def script_beats(item):
    """Return [(text, keywords)] regardless of old flat or new beat format."""
    if item.get("beats"):
        return [(b["text"].strip(), b.get("keywords") or ["documents", "office"])
                for b in item["beats"] if b.get("text", "").strip()]
    # backwards compatible with the old flat-script format
    return [(item["script"].strip(), item.get("keywords") or ["documents", "office"])]


# --------------------------------------------------------------------------- #
# narration
# --------------------------------------------------------------------------- #

def resolve_voice_id():
    """Use ELEVENLABS_VOICE_ID if set, else the account's first available voice.
    Avoids hardcoding an ID that may not exist on every account/plan."""
    global ELEVENLABS_VOICE_ID
    if ELEVENLABS_VOICE_ID:
        return ELEVENLABS_VOICE_ID
    r = requests.get("https://api.elevenlabs.io/v1/voices",
                     headers={"xi-api-key": ELEVENLABS_API_KEY}, timeout=30)
    if not r.ok:
        print(f"ElevenLabs /v1/voices error {r.status_code}: {r.text[:1000]}")
    r.raise_for_status()
    voices = r.json().get("voices", [])
    if not voices:
        raise SystemExit("ElevenLabs account has no voices available.")
    ELEVENLABS_VOICE_ID = voices[0]["voice_id"]
    print(f"  voice: {voices[0].get('name')} ({ELEVENLABS_VOICE_ID})")
    return ELEVENLABS_VOICE_ID


# Per-request caps differ by provider: ElevenLabs rejects over 10,000 characters,
# Google Cloud TTS over 5,000 bytes. Beats run ~860 chars so neither normally
# bites, but the splitter below guarantees it.
MAX_TTS_CHARS = 4500 if TTS_PROVIDER == "google" else 9500
BEAT_GAP_SECONDS = 0.35   # breath between beats; also masks any prosody seam


def split_long(text, limit=MAX_TTS_CHARS):
    """Safety net if a single beat is ever written over the request cap."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for sentence in text.replace("? ", "?|").replace("! ", "!|") \
                        .replace(". ", ".|").split("|"):
        if len(cur) + len(sentence) + 1 > limit and cur:
            out.append(cur.strip())
            cur = sentence
        else:
            cur += (" " if cur else "") + sentence
    if cur.strip():
        out.append(cur.strip())
    return out


def synthesise_beats(beats, out_dir):
    """One TTS request per beat.

    Required, not merely tidy: a 15-25 minute script runs 11-20k characters and
    ElevenLabs caps a single request at 10,000. Synthesising per beat also
    yields the REAL duration of each beat, so footage syncs to the story
    exactly instead of being apportioned by word count.
    """
    parts = []
    for i, (text, keywords) in enumerate(beats):
        chunks = split_long(text)
        chunk_paths, aligns = [], []
        for j, chunk in enumerate(chunks):
            path = os.path.join(out_dir, f"beat_{i:03d}_{j}.mp3")
            aligns.append(generate_voice(chunk, path))
            chunk_paths.append(path)
        if len(chunk_paths) == 1:
            path, align = chunk_paths[0], aligns[0]
        else:
            path = os.path.join(out_dir, f"beat_{i:03d}.mp3")
            concat_audio(chunk_paths, path)
            align = None
        dur = media_duration(path)
        parts.append({"path": path, "text": text, "keywords": keywords,
                      "align": align, "dur": dur})
        print(f"  beat {i+1:2d}/{len(beats)}: {dur:5.1f}s  ({len(text)} chars)")
    return parts


def concat_audio(paths, out_path, gap=0.0):
    """Join mp3 parts, optionally with a short silence between them."""
    list_file = out_path + ".txt"
    silence = None
    if gap > 0:
        silence = out_path + ".sil.mp3"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"anullsrc=r=44100:cl=mono", "-t", f"{gap}",
                        "-c:a", "libmp3lame", silence], check=True, timeout=120)
    with open(list_file, "w") as f:
        for k, p in enumerate(paths):
            f.write(f"file '{os.path.abspath(p)}'\n")
            if silence and k < len(paths) - 1:
                f.write(f"file '{os.path.abspath(silence)}'\n")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat",
                    "-safe", "0", "-i", list_file, "-c", "copy", out_path],
                   check=True, timeout=600)


_GOOGLE_VOICE = None


def resolve_google_voice():
    """Pick the best available en-US voice at runtime rather than hardcoding a
    name that may not exist on this project. Preference order is quality-first:
    Chirp3 HD, then Neural2, then Wavenet, then Standard."""
    global _GOOGLE_VOICE
    if _GOOGLE_VOICE:
        return _GOOGLE_VOICE
    if GOOGLE_VOICE_NAME:
        _GOOGLE_VOICE = GOOGLE_VOICE_NAME
        print(f"  voice (pinned): {_GOOGLE_VOICE}")
        return _GOOGLE_VOICE

    r = requests.get("https://texttospeech.googleapis.com/v1/voices",
                     params={"languageCode": "en-US"},
                     headers={"X-Goog-Api-Key": GOOGLE_TTS_API_KEY}, timeout=30)
    if not r.ok:
        print(f"Google /v1/voices error {r.status_code}: {r.text[:1000]}")
    r.raise_for_status()
    voices = r.json().get("voices", [])
    if not voices:
        raise SystemExit("Google TTS returned no en-US voices.")

    def rank(v):
        n = v.get("name", "")
        tier = (0 if "Chirp3-HD" in n else 1 if "Neural2" in n else
                2 if "Wavenet" in n else 3)
        # A male voice suits authoritative case-study narration.
        gender = 0 if v.get("ssmlGender") == "MALE" else 1
        return (tier, gender, n)

    best = sorted(voices, key=rank)[0]
    _GOOGLE_VOICE = best["name"]
    print(f"  voice: {_GOOGLE_VOICE} ({best.get('ssmlGender')})  "
          f"[{len(voices)} en-US voices available]")
    return _GOOGLE_VOICE


def google_tts(text, out_path):
    """Synthesise via Google Cloud TTS.

    Uses the REST endpoint with an API key header: the official client
    libraries do not accept API keys, but the REST API does, which keeps setup
    to one secret instead of a service-account JSON.

    Returns None — Google does not return character alignment, so captions fall
    back to proportional timing within each beat (beats are only ~40-55s, so
    drift stays small).
    """
    import base64

    voice = resolve_google_voice()
    payload = {
        "input": {"text": text},
        "voice": {"languageCode": "en-US", "name": voice},
        "audioConfig": {"audioEncoding": "MP3"},
    }
    # Chirp voices reject speaking-rate tuning; others benefit from a slightly
    # slower, more deliberate read for this format.
    if "Chirp" not in voice:
        payload["audioConfig"]["speakingRate"] = 0.95

    r = requests.post("https://texttospeech.googleapis.com/v1/text:synthesize",
                      json=payload,
                      headers={"X-Goog-Api-Key": GOOGLE_TTS_API_KEY,
                               "Content-Type": "application/json"},
                      timeout=180)
    if not r.ok:
        print(f"Google TTS error {r.status_code}: {r.text[:2000]}")
    r.raise_for_status()
    audio = r.json().get("audioContent")
    if not audio:
        raise SystemExit("Google TTS returned no audioContent.")
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(audio))
    return None


# Hard spend guard. Google Cloud TTS requires a billing account, so a runaway
# retry loop would bill a real card rather than simply erroring. One long-form
# script is ~12k characters; 60k is five scripts' worth in a single run, which
# can only mean something has gone wrong. Abort rather than spend.
TTS_CHAR_BUDGET = int(os.environ.get("TTS_CHAR_BUDGET", "60000"))
_TTS_CHARS_USED = 0


def generate_voice(text, out_path):
    """Dispatch to the configured narration provider."""
    global _TTS_CHARS_USED
    _TTS_CHARS_USED += len(text)
    if _TTS_CHARS_USED > TTS_CHAR_BUDGET:
        raise SystemExit(
            f"Aborting: this run has asked for {_TTS_CHARS_USED} characters of "
            f"narration, over the {TTS_CHAR_BUDGET} budget. Something is looping."
        )
    if TTS_PROVIDER == "google":
        if not GOOGLE_TTS_API_KEY:
            raise SystemExit("GOOGLE_TTS_API_KEY is not set (TTS_PROVIDER=google).")
        return google_tts(text, out_path)
    if not ELEVENLABS_API_KEY:
        raise SystemExit("ELEVENLABS_API_KEY is not set (TTS_PROVIDER=elevenlabs).")
    return elevenlabs_tts(text, out_path)


def elevenlabs_tts(text, out_path):
    """Synthesise narration. Returns character-level alignment when the API
    provides it, else None (callers fall back to proportional timing)."""
    import base64

    voice_id = resolve_voice_id()
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
    }

    # with-timestamps gives us captions for free from the same synthesis call.
    ts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
    r = requests.post(ts_url, json=payload, headers=headers, timeout=600)
    if r.ok:
        try:
            data = r.json()
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(data["audio_base64"]))
            align = data.get("normalized_alignment") or data.get("alignment")
            if align and align.get("characters"):
                print(f"  narration + timings ok ({len(align['characters'])} chars)")
                return align
            print("  narration ok (no timings returned)")
            return None
        except Exception as e:
            print(f"  with-timestamps parse failed ({e.__class__.__name__}); falling back")

    # Fallback: plain synthesis endpoint.
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    r = requests.post(url, json=payload, headers=headers, timeout=600)
    if not r.ok:
        print(f"ElevenLabs error {r.status_code}: {r.text[:2000]}")
    r.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(r.content)
    print("  narration ok (plain endpoint, no timings)")
    return None


def media_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


# --------------------------------------------------------------------------- #
# captions
# --------------------------------------------------------------------------- #

def alignment_char_times(align):
    """Normalise the two shapes ElevenLabs has used for alignment payloads."""
    chars = align.get("characters") or []
    starts = (align.get("character_start_times_seconds")
              or align.get("characterStartTimesSeconds") or [])
    ends = (align.get("character_end_times_seconds")
            or align.get("characterEndTimesSeconds") or [])
    n = min(len(chars), len(starts), len(ends))
    return chars[:n], starts[:n], ends[:n]


def caption_lines(text, align, total_duration, max_chars=42):
    """Group narration into short on-screen lines with start/end times."""
    words, cursor = [], 0
    if align:
        chars, starts, ends = alignment_char_times(align)
        joined = "".join(chars)
        # walk words through the aligned character stream
        for w in text.split():
            idx = joined.find(w, cursor)
            if idx == -1:
                words.append((w, None, None))
                continue
            j = min(idx + len(w) - 1, len(ends) - 1)
            words.append((w, starts[idx], ends[j]))
            cursor = idx + len(w)
    else:
        words = [(w, None, None) for w in text.split()]

    # fill any gaps proportionally so a partial alignment still works
    n = len(words)
    for i, (w, s, e) in enumerate(words):
        if s is None:
            s = total_duration * i / max(n, 1)
            e = total_duration * (i + 1) / max(n, 1)
            words[i] = (w, s, e)

    lines, cur, cur_start = [], [], None
    for w, s, e in words:
        if cur_start is None:
            cur_start = s
        candidate = " ".join(cur + [w])
        if len(candidate) > max_chars and cur:
            lines.append((cur_start, prev_end, " ".join(cur)))
            cur, cur_start = [w], s
        else:
            cur.append(w)
        prev_end = e
    if cur:
        lines.append((cur_start, prev_end, " ".join(cur)))
    return lines


def _ass_time(t):
    t = max(0.0, float(t))
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def write_subtitles(lines, path):
    """ASS gives control over size/outline that SRT does not."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,DejaVu Sans,64,&H00FFFFFF,&H00000000,&H80000000,-1,0,1,4,2,2,120,120,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        for start, end, text in lines:
            if end is None or end <= start:
                end = start + 1.2
            safe = text.replace("{", "(").replace("}", ")").replace("\n", " ")
            f.write(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,{safe}\n")


# --------------------------------------------------------------------------- #
# footage
# --------------------------------------------------------------------------- #

_SEARCH_CACHE = {}


def search_clips(keyword):
    """Search Pexels once per keyword and cache. A 17-minute video needs ~170
    visual changes; searching per slot would blow through Pexels' 200-requests
    -per-hour limit, so results are reused across slots instead."""
    if keyword in _SEARCH_CACHE:
        return _SEARCH_CACHE[keyword]
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            params={"query": keyword, "per_page": 15, "orientation": "landscape"},
            headers={"Authorization": PEXELS_API_KEY}, timeout=30,
        )
        r.raise_for_status()
        videos = r.json().get("videos", [])
    except Exception as e:
        print(f"  search '{keyword}' failed: {e.__class__.__name__}")
        videos = []
    _SEARCH_CACHE[keyword] = videos
    return videos


def download_clip(video, out_path):
    """Prefer the largest file at or below 1080p — decoding 4K source is what
    makes the encode crawl on a 2-core runner."""
    files = [v for v in video.get("video_files", []) if v.get("width")]
    if not files:
        return None
    hd = [v for v in files if v["width"] <= 1920]
    best = max(hd, key=lambda v: v["width"]) if hd else min(files, key=lambda v: v["width"])
    with requests.get(best["link"], stream=True, timeout=180) as resp:
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    return best["width"]


def fetch_story_synced_clips(beats, beat_spans, out_dir, max_unique=60):
    """Plan the visual timeline: each beat gets clips drawn from its OWN
    keywords, so footage follows what is being said. Downloads are capped and
    reused across the runtime — seeing a shot twice 10 minutes apart is normal
    and costs far less bandwidth than 170 separate files."""
    plan, downloaded, idx = [], {}, 0

    for (text, keywords), (start, end) in zip(beats, beat_spans):
        span = max(end - start, 0.1)
        n = max(1, int(round(span / SECONDS_PER_CLIP)))
        slot = span / n
        for k in range(n):
            kw = keywords[k % len(keywords)]
            videos = search_clips(kw)
            chosen = None

            if videos and len(downloaded) < max_unique:
                video = videos[(k // len(keywords)) % len(videos)]
                vid = str(video.get("id"))
                if vid in downloaded:
                    chosen = downloaded[vid]
                else:
                    path = os.path.join(out_dir, f"clip_{idx:04d}.mp4")
                    try:
                        if download_clip(video, path) and os.path.getsize(path) > 1000:
                            downloaded[vid] = path
                            chosen = path
                            idx += 1
                    except Exception as e:
                        print(f"  download '{kw}' failed: {e.__class__.__name__}")

            if chosen is None:
                # reuse something already on disk, preferring this beat's own
                # earlier footage over a random shot from elsewhere
                pool = [downloaded[str(v.get("id"))] for v in videos
                        if str(v.get("id")) in downloaded] or list(downloaded.values())
                if not pool:
                    continue
                chosen = pool[k % len(pool)]

            plan.append((chosen, slot))

    if not plan:
        raise SystemExit("No stock clips fetched — check PEXELS_API_KEY / keywords.")
    print(f"  {len(plan)} slots from {len(downloaded)} unique clips "
          f"({len(_SEARCH_CACHE)} searches)")
    return plan


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #

def assemble_video(plan, audio_path, subtitle_path, out_path, duration):
    """Each clip is trimmed to its slot, given a slow push-in, then concatenated.

    Deliberately avoids concat-demuxer `duration` directives: when a directive
    exceeds a clip's real length ffmpeg pads by duplicating frames and can spin
    for tens of minutes. Every segment here is explicitly bounded.
    """
    seg_dir = os.path.join(OUTPUT_DIR, "seg")
    os.makedirs(seg_dir, exist_ok=True)
    segments = []

    for i, (clip, slot) in enumerate(plan):
        seg = os.path.join(seg_dir, f"seg_{i:04d}.mp4")
        frames = max(int(slot * FPS), 2)
        # Ken Burns needs only enough headroom for the max zoom (1.08). Scaling
        # to 4K first and zooming down works but costs ~3x the pixel throughput
        # for no visible gain — this is the difference between a 20-minute and a
        # 60-minute run on a 2-core CI box.
        pad_w, pad_h = int(WIDTH * 1.15) // 2 * 2, int(HEIGHT * 1.15) // 2 * 2
        zoom = f"min(1.08,1+0.08*on/{frames})"
        vf = (
            f"scale={pad_w}:{pad_h}:force_original_aspect_ratio=increase,"
            f"crop={pad_w}:{pad_h},"
            f"zoompan=z='{zoom}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={WIDTH}x{HEIGHT}:fps={FPS},"
            f"setsar=1"
        )
        # Intermediate segments are re-encoded by the final pass anyway, so use
        # the fastest preset and a near-lossless CRF rather than paying twice.
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-stream_loop", "-1", "-i", clip,   # loop if the clip is shorter than its slot
            "-t", f"{slot:.3f}", "-an",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            seg,
        ], check=True, timeout=900)
        segments.append(seg)

    list_file = os.path.join(OUTPUT_DIR, "concat_list.txt")
    with open(list_file, "w") as f:
        for s in segments:
            f.write(f"file '{os.path.abspath(s)}'\n")

    vf_final = "null"
    if CAPTIONS and subtitle_path and os.path.exists(subtitle_path):
        esc = subtitle_path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        vf_final = f"subtitles='{esc}'"

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-stats",
           "-f", "concat", "-safe", "0", "-i", list_file,
           "-i", audio_path]

    if MUSIC:
        # Procedurally generated bed: no third-party track, so no Content ID
        # claim and no licence to track. Deliberately sparse for this format.
        cmd += ["-f", "lavfi", "-i",
                f"sine=frequency=55:duration={duration:.2f},"
                f"aformat=channel_layouts=stereo"]
        filter_a = (f"[1:a]volume=1.0[v];"
                    f"[2:a]volume={MUSIC_GAIN_DB}dB,afade=t=in:d=3,"
                    f"afade=t=out:st={max(duration-4,0):.2f}:d=4[m];"
                    f"[v][m]amix=inputs=2:duration=first:dropout_transition=0[a]")
        cmd += ["-filter_complex", filter_a, "-map", "0:v:0", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]

    cmd += ["-vf", vf_final,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            "-t", f"{duration:.2f}", "-shortest",
            out_path]

    subprocess.run(cmd, check=True, timeout=3600)


def make_thumbnail(title, out_path):
    img = Image.new("RGB", (1280, 720), (12, 12, 16))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 86)
    except Exception:
        font = ImageFont.load_default()
    draw.rectangle([0, 0, 24, 720], fill=(200, 30, 30))
    wrapped = textwrap.fill(title.upper(), width=15)
    draw.multiline_text((80, 160), wrapped, font=font,
                        fill=(245, 245, 245), spacing=18)
    img.save(out_path, quality=92)


# --------------------------------------------------------------------------- #
# upload
# --------------------------------------------------------------------------- #

def upload_to_youtube(video_path, thumb_path, title, description):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    # .strip() matters: a trailing newline pasted into a GitHub secret makes
    # Google reject the credentials with a misleading "OAuth client not found".
    client_id = os.environ["YT_CLIENT_ID"].strip()
    client_secret = os.environ["YT_CLIENT_SECRET"].strip()
    refresh_token = os.environ["YT_REFRESH_TOKEN"].strip()

    print(f"  YT_CLIENT_ID     = {client_id}")
    print(f"  YT_CLIENT_SECRET = {len(client_secret)} chars")
    print(f"  YT_REFRESH_TOKEN = {len(refresh_token)} chars")
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise SystemExit("YT_CLIENT_ID must end in .apps.googleusercontent.com")

    creds = Credentials(
        None, refresh_token=refresh_token,
        client_id=client_id, client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
    )
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {"title": title[:100], "description": description[:4900],
                    "categoryId": "22"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    response = yt.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(video_path, chunksize=-1, resumable=True),
    ).execute()
    video_id = response["id"]

    # Custom thumbnails need a phone-verified channel. Never let this cosmetic
    # step fail the run and cause the script to be re-published next time.
    try:
        yt.thumbnails().set(videoId=video_id,
                            media_body=MediaFileUpload(thumb_path)).execute()
        print("  custom thumbnail set")
    except Exception as e:
        print(f"  WARNING: thumbnail rejected ({e.__class__.__name__}). "
              f"Verify the channel at youtube.com/verify. Video is live regardless.")
    return video_id


# --------------------------------------------------------------------------- #

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    item, queue = load_next_script()
    audio_path = os.path.join(OUTPUT_DIR, "voice.mp3")
    video_path = os.path.join(OUTPUT_DIR, "final.mp4")
    thumb_path = os.path.join(OUTPUT_DIR, "thumb.jpg")
    subs_path = os.path.join(OUTPUT_DIR, "captions.ass")

    beats = script_beats(item)
    full_text = " ".join(t for t, _ in beats)
    print(f"Producing: {item['title']}  ({len(beats)} beats, {len(full_text.split())} words)")

    parts = synthesise_beats(beats, OUTPUT_DIR)
    concat_audio([p["path"] for p in parts], audio_path, gap=BEAT_GAP_SECONDS)
    duration = media_duration(audio_path)
    print(f"  narration duration: {duration/60:.1f} min")

    # Beat spans come from MEASURED audio, not a word-count guess, so the
    # footage cuts land exactly where the narration moves on.
    spans, t0 = [], 0.0
    for p in parts:
        spans.append((t0, t0 + p["dur"]))
        t0 += p["dur"] + BEAT_GAP_SECONDS

    if CAPTIONS:
        lines = []
        for p, (start, _) in zip(parts, spans):
            for s, e, txt in caption_lines(p["text"], p["align"], p["dur"]):
                lines.append((start + s, start + e, txt))
        write_subtitles(lines, subs_path)
        print(f"  captions written ({len(lines)} cues)")

    plan = fetch_story_synced_clips(beats, spans, OUTPUT_DIR)
    assemble_video(plan, audio_path, subs_path, video_path, duration)
    print(f"  video assembled: {os.path.getsize(video_path)/1e6:.1f} MB")

    make_thumbnail(item["title"], thumb_path)
    video_id = upload_to_youtube(video_path, thumb_path,
                                 item["title"], item.get("description", ""))
    mark_used(item, queue)
    print(f"Uploaded: https://youtu.be/{video_id}")


if __name__ == "__main__":
    main()
