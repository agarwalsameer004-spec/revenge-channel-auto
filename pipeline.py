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

ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]

QUEUE_FILE = "scripts_queue.json"
OUTPUT_DIR = "output"


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
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.4, "similarity_boost": 0.75},
    }
    r = requests.post(url, json=payload, headers=headers, timeout=120)
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
        best = min(video["video_files"], key=lambda v: abs(v.get("width", 1920) - 1920))
        path = os.path.join(out_dir, f"clip_{i}.mp4")
        with requests.get(best["link"], stream=True, timeout=120) as resp:
            with open(path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
        paths.append(path)
    return paths


def assemble_video(clip_paths, audio_path, out_path, duration):
    if not clip_paths:
        raise SystemExit("No stock clips fetched — check PEXELS_API_KEY / keywords.")
    per_clip = duration / len(clip_paths)
    list_file = os.path.join(OUTPUT_DIR, "concat_list.txt")
    with open(list_file, "w") as f:
        for c in clip_paths:
            f.write(f"file '{os.path.abspath(c)}'\nduration {per_clip}\n")
        f.write(f"file '{os.path.abspath(clip_paths[-1])}'\n")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
        "-i", audio_path, "-map", "0:v", "-map", "1:a",
        "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
        "-c:v", "libx264", "-c:a", "aac", "-shortest", out_path,
    ], check=True)


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

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
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
    yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_path)).execute()
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
    print(f"Uploaded: https://youtu.be/{video_id}")
    mark_used(item, queue)


if __name__ == "__main__":
    main()
