"""Assemble generated stills + voiceover into a finished video.

Each beat becomes a short clip with a Ken Burns move, then clips are merged. Crossfading 400
clips in one filtergraph is not viable, so the merge runs as a tree: groups of ten are xfaded
together, then the results of those, until one file remains.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config
from .media import MediaError, ffmpeg_bin, run

log = logging.getLogger(__name__)

MERGE_GROUP = 10
OVERSAMPLE = 2          # render the still at 2x before zoompan, which cuts pan/zoom jitter
MAX_ZOOM = 1.18
PAN_ZOOM = 1.15


@dataclass
class Clip:
    image: Path
    duration: float
    motion: str = "zoom_in"


def _ken_burns(motion: str, frames: int, width: int, height: int, fps: int) -> str:
    """A zoompan filter string. Expressions key off `on` (output frame) so there is no drift."""
    frames = max(frames, 2)
    last = frames - 1
    centre_x, centre_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"

    if motion == "zoom_out":
        step = (MAX_ZOOM - 1.0) / last
        zoom, x, y = f"max({MAX_ZOOM:.6f}-on*{step:.8f},1.0)", centre_x, centre_y
    elif motion == "pan_right":
        zoom, x, y = f"{PAN_ZOOM:.6f}", f"(iw-iw/zoom)*on/{last}", centre_y
    elif motion == "pan_left":
        zoom, x, y = f"{PAN_ZOOM:.6f}", f"(iw-iw/zoom)*(1-on/{last})", centre_y
    elif motion == "pan_up":
        zoom, x, y = f"{PAN_ZOOM:.6f}", centre_x, f"(ih-ih/zoom)*(1-on/{last})"
    elif motion == "pan_down":
        zoom, x, y = f"{PAN_ZOOM:.6f}", centre_x, f"(ih-ih/zoom)*on/{last}"
    else:  # zoom_in
        step = (MAX_ZOOM - 1.0) / last
        zoom, x, y = f"min(1.0+on*{step:.8f},{MAX_ZOOM:.6f})", centre_x, centre_y

    return (
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps}"
    )


def render_clip(clip: Clip, out_path: Path, *, width: int, height: int, fps: int,
                crf: int = 18, effect: str = "none") -> Path:
    """One still -> one moving clip, letterbox-free (cover-crop to the target frame)."""
    frames = max(2, int(round(clip.duration * fps)))
    big_w, big_h = width * OVERSAMPLE, height * OVERSAMPLE
    steps = [
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase",
        f"crop={big_w}:{big_h}",
        "setsar=1",
        _ken_burns(clip.motion, frames, width, height, fps),
    ]
    effect_filter = config.POST_EFFECTS.get(effect, "")
    if effect_filter:
        steps.append(effect_filter)
    steps.append("format=yuv420p")
    chain = ",".join(steps)
    run([
        ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(clip.image),
        "-vf", chain,
        "-frames:v", str(frames),
        "-r", str(fps),
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        str(out_path),
    ], timeout=600)
    return out_path


def _xfade_group(paths: list[Path], durations: list[float], out_path: Path,
                 *, fade: float, fps: int, crf: int) -> float:
    """Crossfade a handful of clips into one. Returns the resulting duration."""
    if len(paths) == 1:
        return durations[0]

    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
    for path in paths:
        cmd += ["-i", str(path)]

    steps: list[str] = []
    label = "0:v"
    elapsed = durations[0]
    for i in range(1, len(paths)):
        offset = max(0.0, elapsed - fade)
        nxt = f"v{i}"
        steps.append(
            f"[{label}][{i}:v]xfade=transition=fade:duration={fade:.3f}:offset={offset:.3f}[{nxt}]"
        )
        label = nxt
        elapsed = offset + fade + (durations[i] - fade)

    cmd += [
        "-filter_complex", ";".join(steps),
        "-map", f"[{label}]",
        "-r", str(fps), "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    run(cmd, timeout=3600)
    return elapsed


def _concat_copy(paths: list[Path], out_path: Path, work_dir: Path) -> Path:
    """Hard cuts: stream-copy concat, so no re-encode and no generation loss."""
    listing = work_dir / "concat.txt"
    listing.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in paths), encoding="utf-8"
    )
    run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out_path)],
        timeout=3600)
    return out_path


def _merge_tree(paths: list[Path], durations: list[float], work_dir: Path,
                *, fade: float, fps: int, progress=None) -> Path:
    """Crossfade-merge in groups of MERGE_GROUP until a single file remains."""
    level = 0
    current = list(paths)
    current_durations = list(durations)

    while len(current) > 1:
        level += 1
        stage_dir = work_dir / f"merge{level}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        groups = [
            (current[i:i + MERGE_GROUP], current_durations[i:i + MERGE_GROUP])
            for i in range(0, len(current), MERGE_GROUP)
        ]
        merged: list[Path] = []
        merged_durations: list[float] = []
        for index, (group_paths, group_durations) in enumerate(groups):
            if len(group_paths) == 1:
                merged.append(group_paths[0])
                merged_durations.append(group_durations[0])
                continue
            out = stage_dir / f"m{index:04d}.mp4"
            # Intermediates re-encode, so keep them near-lossless; the final mux copies video.
            duration = _xfade_group(group_paths, group_durations, out,
                                    fade=fade, fps=fps, crf=16)
            merged.append(out)
            merged_durations.append(duration)
            if progress:
                progress(f"Merging pass {level}", index + 1, len(groups))
        current, current_durations = merged, merged_durations

    return current[0]


def _mux_audio(video: Path, voiceover: Path, out_path: Path, *,
               music: Path | None, music_gain_db: float) -> Path:
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video), "-i", str(voiceover)]
    if music:
        # Loop the bed so a short track still covers a 40-minute video.
        cmd += ["-stream_loop", "-1", "-i", str(music),
                "-filter_complex",
                f"[2:a]volume={music_gain_db}dB[bed];"
                f"[1:a][bed]amix=inputs=2:duration=first:dropout_transition=0,"
                f"alimiter=limit=0.95[aout]",
                "-map", "0:v", "-map", "[aout]"]
    else:
        cmd += ["-map", "0:v", "-map", "1:a"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out_path)]
    run(cmd, timeout=3600)
    return out_path


def render_video(
    clips: list[Clip],
    voiceover: Path,
    out_path: Path,
    work_dir: Path,
    *,
    width: int,
    height: int,
    fps: int = 30,
    transition: str = "crossfade",
    transition_seconds: float = 0.5,
    music: Path | None = None,
    music_gain_db: float = -22.0,
    effect: str = "none",
    progress: Callable[[str, int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Path:
    """Render clips + voiceover into a finished mp4 at `out_path`."""
    if not clips:
        raise MediaError("Nothing to render -- no images were generated.")

    note = progress or (lambda *_args: None)
    seg_dir = work_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    crossfading = transition == "crossfade" and len(clips) > 1
    fade = max(0.1, min(transition_seconds, min(c.duration for c in clips) * 0.6)) if crossfading else 0.0

    # With crossfades each clip overlaps its neighbour, so it must run `fade` longer to keep
    # the finished timeline in sync with the voiceover. The last clip has nothing to overlap.
    durations = [
        c.duration + (fade if crossfading and i < len(clips) - 1 else 0.0)
        for i, c in enumerate(clips)
    ]

    done = 0
    total = len(clips)

    def build(args) -> tuple[int, Path]:
        index, clip, duration = args
        if should_stop and should_stop():
            raise MediaError("Render cancelled")
        target = seg_dir / f"{index:05d}.mp4"
        render_clip(Clip(clip.image, duration, clip.motion), target,
                    width=width, height=height, fps=fps, effect=effect)
        return index, target

    workers = max(1, min(os.cpu_count() or 2, 8))
    segments: dict[int, Path] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, path in pool.map(
            build, [(i, c, d) for i, (c, d) in enumerate(zip(clips, durations))]
        ):
            segments[index] = path
            done += 1
            note("Rendering clips", done, total)

    ordered = [segments[i] for i in range(len(clips))]

    note("Joining clips", 0, 1)
    if crossfading:
        joined = _merge_tree(ordered, durations, work_dir, fade=fade, fps=fps, progress=note)
    else:
        joined = _concat_copy(ordered, work_dir / "joined.mp4", work_dir)

    note("Adding audio", 1, 1)
    return _mux_audio(joined, voiceover, out_path, music=music, music_gain_db=music_gain_db)
