"""ffprobe wrappers.

CapCut stores media dimensions and duration inside the draft, so we must read
the real values from disk (screen recordings are often retina, e.g. 5120x3414 —
never assume 1920x1080).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MediaInfo:
    width: int
    height: int
    duration_us: int
    has_audio: bool
    fps: float


def probe_media(path: str | Path) -> MediaInfo:
    """Probe a media file for width/height/duration/audio/fps.

    Raises FileNotFoundError if the file does not exist, RuntimeError if
    ffprobe fails or returns no video stream.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"media not found: {p}")

    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,width,height,r_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json",
        str(p),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(f"ffprobe failed for {p}: {e}") from e

    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError(f"no video stream in {p}")

    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)

    # r_frame_rate is a rational like "25/1"
    fps = 25.0
    rfr = video.get("r_frame_rate") or ""
    if "/" in rfr:
        num, den = rfr.split("/", 1)
        try:
            den_f = float(den)
            if den_f:
                fps = float(num) / den_f
        except ValueError:
            pass

    duration_s = float(data.get("format", {}).get("duration") or 0.0)
    duration_us = int(round(duration_s * 1_000_000))

    return MediaInfo(width=width, height=height, duration_us=duration_us,
                     has_audio=has_audio, fps=fps)
