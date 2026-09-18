"""Job store and pipeline orchestration.

One job == one video. It runs on a background thread and writes its whole state to
storage/jobs/<id>/job.json, so progress survives a page refresh (and a server restart).
"""
from __future__ import annotations

import csv
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, director, render, script_align
from .beats import build_beats
from .imagegen import generate_all, generate_one, get_provider
from .media import audio_duration
from .transcribe import transcribe

log = logging.getLogger(__name__)

PERSIST_INTERVAL = 1.0  # seconds; progress ticks are throttled to avoid hammering the disk


@dataclass
class Job:
    id: str
    title: str
    settings: dict[str, Any]
    status: str = "queued"
    stage: str = "queued"
    message: str = ""
    progress: dict[str, int] = field(default_factory=lambda: {"done": 0, "total": 0})
    audio: dict[str, Any] = field(default_factory=dict)
    alignment: dict[str, Any] | None = None
    style: dict[str, Any] | None = None
    beats: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    video: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_persist: float = field(default=0.0, repr=False)

    # --- paths -------------------------------------------------------------
    @property
    def dir(self) -> Path:
        return config.STORAGE_DIR / "jobs" / self.id

    @property
    def images_dir(self) -> Path:
        return self.dir / "images"

    @property
    def work_dir(self) -> Path:
        return self.dir / "work"

    # --- state -------------------------------------------------------------
    def to_dict(self, *, include_beats: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id, "title": self.title, "settings": self.settings,
            "status": self.status, "stage": self.stage, "message": self.message,
            "progress": self.progress, "audio": self.audio,
            "alignment": self.alignment, "style": self.style,
            "error": self.error, "video": self.video,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "beat_count": len(self.beats),
            "image_count": sum(1 for b in self.beats if b.get("file")),
            "failed_count": sum(1 for b in self.beats if b.get("error")),
        }
        if include_beats:
            data["beats"] = self.beats
        return data

    def set_stage(self, stage: str, message: str = "", *, done: int = 0, total: int = 0) -> None:
        with self._lock:
            self.stage = stage
            self.message = message
            self.progress = {"done": done, "total": total}
            self.updated_at = time.time()
        self.persist(force=True)

    def tick(self, done: int, total: int, message: str | None = None) -> None:
        with self._lock:
            self.progress = {"done": done, "total": total}
            if message:
                self.message = message
            self.updated_at = time.time()
        self.persist()

    def persist(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_persist < PERSIST_INTERVAL:
            return
        self._last_persist = now
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "job.json.tmp"
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(self.dir / "job.json")

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._load_existing()

    def _load_existing(self) -> None:
        root = config.STORAGE_DIR / "jobs"
        if not root.exists():
            return
        for job_file in root.glob("*/job.json"):
            try:
                data = json.loads(job_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            job = Job(id=data["id"], title=data.get("title", ""), settings=data.get("settings", {}))
            for key in ("status", "stage", "message", "progress", "audio", "alignment",
                        "style", "beats", "error", "video", "created_at", "updated_at"):
                if key in data:
                    setattr(job, key, data[key])
            # A job cannot still be running after a restart.
            if job.status in ("running", "rendering", "queued"):
                job.status, job.error = "error", "Interrupted by a server restart."
            self._jobs[job.id] = job

    def create(self, title: str, settings: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], title=title, settings=settings)
        job.dir.mkdir(parents=True, exist_ok=True)
        job.images_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._jobs[job.id] = job
        job.persist(force=True)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def delete(self, job_id: str) -> bool:
        import shutil

        with self._lock:
            job = self._jobs.pop(job_id, None)
        if not job:
            return False
        job.cancel()
        shutil.rmtree(job.dir, ignore_errors=True)
        return True


STORE = JobStore()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def start(job: Job, audio_path: Path, script_text: str, music_path: Path | None) -> None:
    thread = threading.Thread(
        target=_run, args=(job, audio_path, script_text, music_path), daemon=True
    )
    thread.start()


def _run(job: Job, audio_path: Path, script_text: str, music_path: Path | None) -> None:
    try:
        job.status = "running"
        settings = job.settings
        width, height = config.ASPECTS.get(settings.get("aspect", "16:9"), (1920, 1080))

        # 1. Probe -----------------------------------------------------------
        job.set_stage("probing", "Reading the voiceover")
        duration = audio_duration(audio_path)
        job.audio = {"filename": audio_path.name, "duration": round(duration, 2)}
        job.persist(force=True)

        # 2. Timing ----------------------------------------------------------
        script_text = script_text.strip()
        if not script_text:
            raise RuntimeError("No script was supplied. Upload or paste the script to start.")

        cues = script_align.parse_cues(script_text)
        if cues:
            # A subtitle file carries its own timings -- no transcription needed.
            job.set_stage("transcribing", "Reading timings from your subtitle file")
            words = script_align.cues_to_words(cues)
            job.alignment = {"source": "script-timed", "matched": len(words),
                             "total": len(words), "ratio": 1.0}
        else:
            job.set_stage("transcribing", "Transcribing voiceover for exact timings")
            heard = transcribe(audio_path, job.work_dir, progress=lambda m: job.tick(0, 0, m))
            _abort_if_cancelled(job)
            job.set_stage("planning", "Matching your script to the audio")
            aligned = script_align.align_script(script_text, heard, duration)
            words = aligned.words
            job.alignment = aligned.to_dict()
        job.persist(force=True)
        _abort_if_cancelled(job)

        # 3. Beats -----------------------------------------------------------
        ipm = float(settings.get("images_per_minute", 10))
        beats = build_beats(words, duration, ipm)
        job.beats = [b.to_dict() for b in beats]
        job.set_stage("planning", f"Split into {len(beats)} images", total=len(beats))
        _abort_if_cancelled(job)

        # 4. Style bible -----------------------------------------------------
        job.set_stage("directing", "Writing the style bible")
        style = director.plan_style(
            job.title, script_text,
            settings.get("style_preset", "cinematic"),
            settings.get("notes", ""),
            writer_name=settings.get("writer"),
        )
        job.style = style.to_dict()
        job.persist(force=True)
        _abort_if_cancelled(job)

        # 5. Prompts ---------------------------------------------------------
        job.set_stage("directing", "Writing image prompts", total=len(beats))
        written = director.write_prompts(
            beats, style, progress=lambda done, total: job.tick(done, total),
            writer_name=settings.get("writer"),
        )
        for beat, item in zip(job.beats, written):
            beat.update(item)
        job.persist(force=True)
        _write_exports(job)
        _abort_if_cancelled(job)

        # 6. Images ----------------------------------------------------------
        job.set_stage("generating", "Generating images", total=len(beats))
        results = generate_all(
            [b["prompt"] for b in job.beats],
            job.images_dir,
            provider_name=settings.get("provider"),
            width=width, height=height,
            negative=config.DEFAULT_NEGATIVE,
            progress=lambda done, total, failed: job.tick(
                done, total, f"Generating images ({failed} failed)" if failed else "Generating images"
            ),
            should_stop=lambda: job.cancelled,
        )
        for index, outcome in results.items():
            job.beats[index].update(outcome)
        job.persist(force=True)
        _write_exports(job)
        _abort_if_cancelled(job)

        if not any(b.get("file") for b in job.beats):
            raise RuntimeError(
                "Every image failed to generate. Check the API key and the error on the first beat."
            )

        # 7. Render ----------------------------------------------------------
        if settings.get("auto_render", True):
            _render(job, audio_path, music_path, width, height)
        else:
            job.status = "awaiting_review"
            job.set_stage("review", "Images ready -- review them, then render")

    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        log.exception("Job %s failed", job.id)
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.message = str(exc)[:300]
        job.persist(force=True)


def render_existing(job: Job) -> None:
    """Render (or re-render) using the images already on disk."""
    audio_path = job.dir / job.audio["filename"]
    music_path = job.dir / "music" / job.settings["music_filename"] if job.settings.get("music_filename") else None
    width, height = config.ASPECTS.get(job.settings.get("aspect", "16:9"), (1920, 1080))
    threading.Thread(
        target=_render_guarded, args=(job, audio_path, music_path, width, height), daemon=True
    ).start()


def _render_guarded(job: Job, audio_path: Path, music_path: Path | None, w: int, h: int) -> None:
    try:
        _render(job, audio_path, music_path, w, h)
    except Exception as exc:  # noqa: BLE001
        log.exception("Render for %s failed", job.id)
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.persist(force=True)


def _render(job: Job, audio_path: Path, music_path: Path | None, width: int, height: int) -> None:
    job.status = "rendering"
    job.set_stage("rendering", "Rendering video")
    settings = job.settings

    clips = [
        render.Clip(
            image=job.images_dir / beat["file"],
            duration=max(0.4, float(beat["end"]) - float(beat["start"])),
            motion=beat.get("motion", "zoom_in"),
        )
        for beat in job.beats
        if beat.get("file") and (job.images_dir / beat["file"]).exists()
    ]
    if not clips:
        raise RuntimeError("No generated images found on disk to render.")

    out_path = job.dir / "final.mp4"
    render.render_video(
        clips, audio_path, out_path, job.work_dir,
        width=width, height=height,
        fps=int(settings.get("fps", 30)),
        transition=settings.get("transition", "crossfade"),
        transition_seconds=float(settings.get("transition_seconds", 0.5)),
        music=music_path if music_path and music_path.exists() else None,
        music_gain_db=float(settings.get("music_gain_db", -22)),
        effect=settings.get("effect", "none"),
        progress=lambda stage, done, total: job.tick(done, total, stage),
        should_stop=lambda: job.cancelled,
    )

    job.video = out_path.name
    job.status = "done"
    job.set_stage("done", "Video ready")


def regenerate_image(job: Job, index: int, new_prompt: str | None = None) -> dict:
    """Re-run a single beat, optionally with an edited prompt."""
    beat = job.beats[index]
    if new_prompt:
        beat["prompt"] = new_prompt.strip()
    width, height = config.ASPECTS.get(job.settings.get("aspect", "16:9"), (1920, 1080))
    provider = get_provider(job.settings.get("provider"))

    beat.pop("error", None)
    try:
        path = generate_one(
            provider, beat["prompt"], job.images_dir / f"{index:04d}",
            negative=config.DEFAULT_NEGATIVE, width=width, height=height,
            seed=int(time.time()) % 100000,
        )
        beat["file"] = path.name
        # Bust the browser cache -- the filename is unchanged but the pixels are not.
        beat["version"] = int(time.time())
    except Exception as exc:  # noqa: BLE001
        beat["error"] = str(exc)[:300]
    job.persist(force=True)
    return beat


def _write_exports(job: Job) -> None:
    """Timing sheet + prompt list, so the shot list is usable outside this tool too."""
    try:
        with (job.dir / "timeline.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["index", "start", "end", "duration", "timecode",
                             "file", "shot", "motion", "narration", "prompt"])
            for beat in job.beats:
                start, end = float(beat["start"]), float(beat["end"])
                writer.writerow([
                    beat["index"], f"{start:.3f}", f"{end:.3f}", f"{end - start:.3f}",
                    _timecode(start), beat.get("file", ""), beat.get("shot", ""),
                    beat.get("motion", ""), beat.get("text", ""), beat.get("prompt", ""),
                ])
        (job.dir / "prompts.json").write_text(
            json.dumps({"title": job.title, "style": job.style, "beats": job.beats}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Could not write exports for %s: %s", job.id, exc)


def _timecode(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _abort_if_cancelled(job: Job) -> None:
    if job.cancelled:
        job.status = "cancelled"
        job.set_stage("cancelled", "Cancelled")
        raise RuntimeError("Job cancelled")
