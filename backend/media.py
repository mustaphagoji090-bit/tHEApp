"""ffmpeg discovery and audio preparation helpers."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


class MediaError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def ffmpeg_bin() -> str:
    """System ffmpeg if present, otherwise the static binary bundled with imageio-ffmpeg."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - only when the dep is missing
        raise MediaError(
            "ffmpeg not found. Install it (`brew install ffmpeg` / `apt install ffmpeg`) "
            "or `pip install imageio-ffmpeg`."
        ) from exc


@lru_cache(maxsize=1)
def ffprobe_bin() -> str | None:
    found = shutil.which("ffprobe")
    if found:
        return found
    # The static imageio-ffmpeg build ships ffmpeg only; probing falls back to parsing ffmpeg output.
    sibling = Path(ffmpeg_bin()).with_name("ffprobe")
    return str(sibling) if sibling.exists() else None


def run(cmd: list[str], *, timeout: int = 3600) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-15:])
        raise MediaError(f"{Path(cmd[0]).name} failed ({proc.returncode}):\n{tail}")
    return proc


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def audio_duration(path: Path) -> float:
    """Length in seconds. Uses ffprobe when available, else parses ffmpeg's banner."""
    probe = ffprobe_bin()
    if probe:
        proc = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0:
            try:
                value = json.loads(proc.stdout)["format"]["duration"]
                if value not in (None, "N/A"):
                    return float(value)
            except (KeyError, ValueError, json.JSONDecodeError):
                pass

    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, timeout=120)
    match = _DURATION_RE.search(proc.stderr or "")
    if not match:
        raise MediaError(f"Could not read the duration of {path.name}. Is it a valid audio file?")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def prepare_for_transcription(src: Path, dest: Path) -> Path:
    """Downmix to 16 kHz mono 32 kbps MP3 -- what Whisper wants, and ~9 MB for 40 minutes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(dest)])
    return dest


def split_audio(src: Path, out_dir: Path, chunk_seconds: int = 600) -> list[tuple[Path, float]]:
    """Split into chunks, returning (path, start_offset). Keeps each piece under API size limits."""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = audio_duration(src)
    if duration <= chunk_seconds:
        return [(src, 0.0)]

    chunks: list[tuple[Path, float]] = []
    offset = 0.0
    index = 0
    while offset < duration:
        piece = out_dir / f"chunk_{index:03d}.mp3"
        run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{offset:.3f}", "-t", str(chunk_seconds), "-i", str(src),
             "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(piece)])
        chunks.append((piece, offset))
        offset += chunk_seconds
        index += 1
    return chunks
