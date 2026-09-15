"""FastAPI app: upload a voiceover, watch the pipeline run, download the video."""
from __future__ import annotations

import io
import logging
import shutil
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, jobs
from .imagegen import available_providers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="AI Story Image Generator")

# Rough per-image prices in USD, only for the estimate shown in the UI.
PROVIDER_COST = {"fal": 0.025, "replicate": 0.035, "openai": 0.04}

if config.FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=config.FRONTEND_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = config.FRONTEND_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>Frontend missing</h1>", status_code=500)
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/api/config")
def get_config() -> dict:
    providers = available_providers()
    return {
        "providers": providers,
        "default_provider": config.default_image_provider(),
        "provider_cost": PROVIDER_COST,
        "services": config.provider_status(),
        "style_presets": list(config.STYLE_PRESETS),
        "aspects": list(config.ASPECTS),
        "director_model": config.DIRECTOR_MODEL,
    }


def _save_upload(upload: UploadFile, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        shutil.copyfileobj(upload.file, handle, length=1024 * 1024)
    return destination


@app.post("/api/jobs")
async def create_job(
    voiceover: UploadFile = File(...),
    title: str = Form(""),
    script: str = Form(""),
    script_file: UploadFile | None = File(None),
    music: UploadFile | None = File(None),
    images_per_minute: float = Form(10),
    aspect: str = Form("16:9"),
    style_preset: str = Form("cinematic"),
    provider: str = Form(""),
    notes: str = Form(""),
    transition: str = Form("crossfade"),
    transition_seconds: float = Form(0.5),
    fps: int = Form(30),
    music_gain_db: float = Form(-22),
    auto_render: bool = Form(True),
) -> dict:
    if aspect not in config.ASPECTS:
        raise HTTPException(400, f"Unknown aspect '{aspect}'")
    if not 1 <= images_per_minute <= 60:
        raise HTTPException(400, "images_per_minute must be between 1 and 60")

    settings = {
        "images_per_minute": images_per_minute, "aspect": aspect,
        "style_preset": style_preset, "provider": provider or None, "notes": notes,
        "transition": transition, "transition_seconds": transition_seconds,
        "fps": fps, "music_gain_db": music_gain_db, "auto_render": auto_render,
    }
    job = jobs.STORE.create(title.strip(), settings)

    audio_name = Path(voiceover.filename or "voiceover.mp3").name
    audio_path = _save_upload(voiceover, job.dir / audio_name)

    script_text = script or ""
    if script_file is not None and script_file.filename:
        raw = (await script_file.read()).decode("utf-8", errors="replace")
        script_text = f"{script_text}\n{raw}".strip()

    music_path = None
    if music is not None and music.filename:
        music_name = Path(music.filename).name
        music_path = _save_upload(music, job.dir / "music" / music_name)
        job.settings["music_filename"] = music_name

    job.persist(force=True)
    jobs.start(job, audio_path, script_text, music_path)
    return {"id": job.id}


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": [job.to_dict(include_beats=False) for job in jobs.STORE.list()]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, beats: bool = True) -> dict:
    job = jobs.STORE.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    return job.to_dict(include_beats=beats)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    if not jobs.STORE.delete(job_id):
        raise HTTPException(404, "No such job")
    return {"deleted": True}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = _require(job_id)
    job.cancel()
    return {"cancelled": True}


@app.get("/api/jobs/{job_id}/image/{index}")
def get_image(job_id: str, index: int):
    job = _require(job_id)
    if not 0 <= index < len(job.beats):
        raise HTTPException(404, "No such beat")
    name = job.beats[index].get("file")
    if not name:
        raise HTTPException(404, "Image not generated")
    path = job.images_dir / name
    if not path.exists():
        raise HTTPException(404, "Image file missing")
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@app.post("/api/jobs/{job_id}/regenerate")
async def regenerate(job_id: str, payload: dict) -> dict:
    job = _require(job_id)
    index = int(payload.get("index", -1))
    if not 0 <= index < len(job.beats):
        raise HTTPException(400, "Bad beat index")
    return jobs.regenerate_image(job, index, payload.get("prompt"))


@app.post("/api/jobs/{job_id}/render")
def start_render(job_id: str) -> dict:
    job = _require(job_id)
    if job.status == "rendering":
        raise HTTPException(409, "Already rendering")
    if not any(b.get("file") for b in job.beats):
        raise HTTPException(409, "There are no images to render yet")
    jobs.render_existing(job)
    return {"rendering": True}


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    job = _require(job_id)
    if not job.video:
        raise HTTPException(404, "No video rendered yet")
    path = job.dir / job.video
    if not path.exists():
        raise HTTPException(404, "Video file missing")
    safe = "".join(c for c in (job.title or "story") if c.isalnum() or c in " -_").strip() or "story"
    return FileResponse(path, media_type="video/mp4", filename=f"{safe}.mp4")


@app.get("/api/jobs/{job_id}/timeline.csv")
def timeline(job_id: str):
    job = _require(job_id)
    path = job.dir / "timeline.csv"
    if not path.exists():
        raise HTTPException(404, "No timeline yet")
    return FileResponse(path, media_type="text/csv", filename=f"{job.id}-timeline.csv")


@app.get("/api/jobs/{job_id}/images.zip")
def images_zip(job_id: str):
    """Numbered stills named by timecode, for dropping straight into an editor."""
    job = _require(job_id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for beat in job.beats:
            name = beat.get("file")
            if not name:
                continue
            path = job.images_dir / name
            if not path.exists():
                continue
            stamp = jobs._timecode(float(beat["start"])).replace(":", "-").replace(".", "-")
            archive.write(path, f"{beat['index']:04d}_{stamp}{path.suffix}")
        if (job.dir / "timeline.csv").exists():
            archive.write(job.dir / "timeline.csv", "timeline.csv")
    buffer.seek(0)
    return StreamingResponse(
        buffer, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{job.id}-images.zip"'},
    )


def _require(job_id: str) -> jobs.Job:
    job = jobs.STORE.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    return job
