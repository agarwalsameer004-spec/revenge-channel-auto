"""
Fully automated video pipeline: pick next script -> AI voice -> stock footage assembly
-> thumbnail -> upload to YouTube. Runs unattended via GitHub Actions (see
.github/workflows/publish.yml). No human step required once the one-time
setup in SETUP.md is done.
"""
import json
import os
import random
import subprocess
import textwrap

import requests
from PIL import Image, ImageDraw, ImageFont

ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"].strip()
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID")  # optional override; auto-detected below if unset
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]

QUEUE_FILE = "scripts_queue.json"
OUTPUT_DIR = "output"


def resolve_voice_id():
    """Use ELEVENLABS_VOICE_ID if set; otherwise pick the account's first available voice.
    Avoids hardcoding a voice ID that may not exist on every ElevenLabs account/plan."""
    global ELEVENLABS_VOICE_ID
    if ELEVENLABS_VOICE_ID:
        return ELEVENLABS_VOICE_ID
    r = requests.get(
        "https://api.elevenlabs.io/v1/voices",
        headers={"xi-api-key": ELEVENLABS_API_KEY}, timeout=30,
    )
    if not r.ok:
        print(f"ElevenLabs /v1/voices error {r.status_code}: {r.text[:2000]}")
    r.raise_for_status()
    voices = r.json().get("voices", [])
    if not voices:
        raise SystemExit("ElevenLabs account has no voices available — add one at elevenlabs.io/app/voice-library.")
    ELEVENLABS_VOICE_ID = voices[0]["voice_id"]
    print(f"Using ElevenLabs voice: {voices[0].get('name')} ({ELEVENLABS_VOICE_ID})")
    return ELEVENLABS_VOICE_ID


def load_next_script():
    with open(QUEUE_FILE) as f:
        queue = json.load(f)
    for item in queue:
        if not item["used"]:
            return item, queue
    raise SystemExit("No unused scripts left in scripts_queue.json — add more before the next run.")


def mark_used(item, queue):
    for q in queue:
        if q["id"] == item["id"]:
            q["used"] = True
    with open(QUEUE_FILE, "w") as f:
        json.dump(queue, f, indent=2)


def generate_voice(text, out_path):
    voice_id = resolve_voice_id()
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.4, "similarity_boost": 0.75},
    }
    r = requests.post(url, json=payload, headers=headers, timeout=120)
    if not r.ok:
        print(f"ElevenLabs error {r.status_code}: {r.text[:2000]}")
    r.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(r.content)


def get_audio_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def fetch_stock_clips(keywords, n, out_dir):
    headers = {"Authorization": PEXELS_API_KEY}
    paths = []
    for i in range(n):
        kw = keywords[i % len(keywords)]
        r = requests.get(
            "https://api.pexels.com/videos/search",
            params={"query": kw, "per_page": 5, "orientation": "landscape"},
            headers=headers, timeout=30,
        )
        r.raise_for_status()
        videos = r.json().get("videos", [])
        if not videos:
            continue
        video = random.choice(videos)
        # Prefer the largest file at or below 1080p. Decoding 4K source on a
        # 2-core CI runner is what makes the encode crawl.
        files = [v for v in video["video_files"] if v.get("width")]
        hd = [v for v in files if v["width"] <= 1920]
        best = max(hd, key=lambda v: v["width"]) if hd else min(files, key=lambda v: v["width"])
        path = os.path.join(out_dir, f"clip_{i}.mp4")
        with requests.get(best["link"], stream=True, timeout=120) as resp:
            with open(path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
        print(f"  clip {i}: '{kw}' @ {best['width']}x{best.get('height')}")
        paths.append(path)
    return paths


def assemble_video(clip_paths, audio_path, out_path, duration):
    """Concatenate stock clips and lay the narration over them.

    Deliberately does NOT use per-file `duration` directives in the concat list:
    when a directive exceeds a clip's real length, ffmpeg pads by duplicating
    frames and can spin indefinitely. Instead the clips are played end to end
    (looping the list if the footage is shorter than the audio) and the output
    is hard-capped with -t, which always terminates.
    """
    if not clip_paths:
        raise SystemExit("No stock clips fetched — check PEXELS_API_KEY / keywords.")

    # Repeat the playlist enough times that footage always outlasts the narration.
    total_clip_secs = 0.0
    for c in clip_paths:
        try:
            total_clip_secs += get_audio_duration(c)  # ffprobe works for video too
        except Exception:
            total_clip_secs += 5.0  # conservative fallback
    loops = max(1, int(duration / max(total_clip_secs, 1.0)) + 1)

    list_file = os.path.join(OUTPUT_DIR, "concat_list.txt")
    with open(list_file, "w") as f:
        for _ in range(loops):
            for c in clip_paths:
                f.write(f"file '{os.path.abspath(c)}'\n")

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-i", audio_path,
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,"
               "crop=1920:1080,fps=25",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-t", f"{duration:.2f}",          # hard stop — cannot run away
        "-shortest",
        out_path,
    ], check=True, timeout=1800)


def make_thumbnail(title, out_path):
    img = Image.new("RGB", (1280, 720), (15, 15, 20))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 80
        )
    except Exception:
        font = ImageFont.load_default()
    wrapped = textwrap.fill(title.upper(), width=14)
    draw.multiline_text((80, 200), wrapped, font=font, fill=(230, 40, 40), spacing=15)
    img.save(out_path)


def upload_to_youtube(video_path, thumb_path, title, description):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    # .strip() matters: a trailing newline pasted into a GitHub secret makes
    # Google reject the credentials with a misleading "OAuth client was not found".
    client_id = os.environ["YT_CLIENT_ID"].strip()
    client_secret = os.environ["YT_CLIENT_SECRET"].strip()
    refresh_token = os.environ["YT_REFRESH_TOKEN"].strip()

    # A client ID is public (it appears in OAuth URLs), so logging it is safe and
    # makes a mismatched secret obvious instead of guessable.
    print(f"  YT_CLIENT_ID     = {client_id}")
    print(f"  YT_CLIENT_SECRET = {len(client_secret)} chars, ends '{client_secret[-4:]}'")
    print(f"  YT_REFRESH_TOKEN = {len(refresh_token)} chars, starts '{refresh_token[:5]}'")
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise SystemExit(
            "YT_CLIENT_ID does not look like a Google client ID "
            "(it must end in .apps.googleusercontent.com)."
        )

    creds = Credentials(
        None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
    )
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {"title": title, "description": description, "categoryId": "24"},
        "status": {"privacyStatus": "public"},
    }
    request = yt.videos().insert(
        part="snippet,status", body=body, media_body=MediaFileUpload(video_path)
    )
    response = request.execute()
    video_id = response["id"]

    # A custom thumbnail needs a phone-verified YouTube channel. If it isn't
    # verified yet the video is still published fine — YouTube just uses an
    # auto-generated frame. Never let this cosmetic step fail the whole run and
    # cause the same script to be re-uploaded next time.
    try:
        yt.thumbnails().set(
            videoId=video_id, media_body=MediaFileUpload(thumb_path)
        ).execute()
        print("  custom thumbnail set")
    except Exception as e:
        print(f"  WARNING: could not set custom thumbnail ({e.__class__.__name__}). "
              f"Verify the channel at youtube.com/verify to enable this. "
              f"Video is published regardless.")

    return video_id


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    item, queue = load_next_script()
    audio_path = os.path.join(OUTPUT_DIR, "voice.mp3")
    video_path = os.path.join(OUTPUT_DIR, "final.mp4")
    thumb_path = os.path.join(OUTPUT_DIR, "thumb.jpg")

    print(f"Producing: {item['title']}")
    generate_voice(item["script"], audio_path)
    duration = get_audio_duration(audio_path)
    n_clips = max(int(duration // 12), 4)
    clips = fetch_stock_clips(item.get("keywords", ["dramatic", "city night"]), n_clips, OUTPUT_DIR)
    assemble_video(clips, audio_path, video_path, duration)
    make_thumbnail(item["title"], thumb_path)
    video_id = upload_to_youtube(video_path, thumb_path, item["title"], item.get("description", ""))
    # Mark used IMMEDIATELY after a successful upload. If anything later throws,
    # the script must not be published a second time on the next run.
    mark_used(item, queue)
    print(f"Uploaded: https://youtu.be/{video_id}")


if __name__ == "__main__":
    main()
