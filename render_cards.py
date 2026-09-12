"""Turn card frame-sequences into video segments.

Two jobs beyond writing frames out:
  - a slow scale drift on every card, so a "static" graphic is never actually
    static (dead-still frames are what make a slide feel like a slide)
  - the 4-second rule: no frame is allowed to sit unchanged longer than that
"""
import os
import subprocess

FPS = 25
MAX_STILL = 4.0          # seconds any single frame may hold


def write_sequence(frames, out_dir, start_index=0, drift=True):
    """frames: [(PIL image, hold_frames)] -> numbered PNGs, returns count."""
    os.makedirs(out_dir, exist_ok=True)
    n = start_index
    for img, hold in frames:
        hold = min(hold, int(MAX_STILL * FPS))
        w, h = img.size
        for k in range(hold):
            if drift:
                # 1.0 -> 1.012 over the hold: imperceptible per frame,
                # but the frame is never twice the same.
                s = 1.0 + 0.012 * (k / max(hold - 1, 1))
                nw, nh = int(w * s), int(h * s)
                fr = img.resize((nw, nh)).crop(
                    ((nw - w) // 2, (nh - h) // 2, (nw - w) // 2 + w, (nh - h) // 2 + h))
            else:
                fr = img
            fr.save(os.path.join(out_dir, f"f_{n:06d}.png"))
            n += 1
    return n


def encode(seq_dir, out_path, ass_path=None, audio=None):
    vf = f"fps={FPS},format=yuv420p"
    if ass_path:
        vf = f"subtitles={ass_path}:fontsdir=/usr/share/fonts," + vf
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-framerate", str(FPS), "-i", os.path.join(seq_dir, "f_%06d.png")]
    if audio:
        cmd += ["-i", audio, "-c:a", "aac", "-shortest"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", "20", out_path]
    subprocess.run(cmd, check=True, timeout=1800)
    return out_path
