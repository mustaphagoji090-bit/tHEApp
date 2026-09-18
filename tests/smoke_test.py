"""End-to-end pipeline check that costs nothing.

Fakes transcription, the director and the image provider, then runs the real beat slicing,
the real job orchestration and the real ffmpeg render. Use it to confirm an install works
before spending any API credits:

    python tests/smoke_test.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="aistory-smoke-"))
# Point storage at a scratch directory before anything imports config's STORAGE_DIR.
import os
os.environ["STORAGE_DIR"] = str(TMP / "storage")

import re  # noqa: E402

from backend import jobs, transcribe, writers  # noqa: E402
from backend.imagegen import base as imagegen_base  # noqa: E402
from backend.media import audio_duration, ffmpeg_bin  # noqa: E402

AUDIO_SECONDS = 60
IMAGES_PER_MINUTE = 10
# Deliberately spelled differently from what the fake transcript "hears", so the run proves
# the uploaded script -- not the transcript -- is what reaches the prompts.
SCRIPT = " ".join(
    f"word{i}" + ("." if i % 12 == 11 else "") for i in range(150)
).replace("word7 ", "Kaelen ").replace("word19 ", "Mara ")
COLOURS = ["0x8e3b46", "0x2f6d5a", "0x35486e", "0x7a6238", "0x4a3660",
           "0x2c6b6b", "0x8a4a2f", "0x3b5d33", "0x5c3b52", "0x666f3a"]


def make_audio(path: Path) -> Path:
    subprocess.run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"sine=frequency=280:duration={AUDIO_SECONDS}",
                    "-b:a", "128k", str(path)], check=True)
    return path


class MockProvider(imagegen_base.ImageProvider):
    """Writes a flat colour JPEG instead of calling an image API."""

    name = "mock"
    calls = 0

    def generate(self, prompt, *, negative, width, height, seed=None) -> bytes:
        index = MockProvider.calls
        MockProvider.calls += 1
        out = TMP / f"mock_{index:04d}.jpg"
        subprocess.run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i",
                        f"color=c={COLOURS[index % len(COLOURS)]}:s={width}x{height}",
                        "-frames:v", "1", str(out)], check=True)
        return out.read_bytes()


def fake_words(_audio, _work_dir, progress=None):
    """A steady 2.5 words/second read with a sentence every 12 words.

    Note it never produces "Mara" or "Kaelen" -- those exist only in the script, so finding
    them in the finished beats proves alignment kept the script's wording.
    """
    words, t, i = [], 0.0, 0
    while t < AUDIO_SECONDS:
        words.append(transcribe.Word(
            text=f"word{i}" + ("." if i % 12 == 11 else ""), start=t, end=t + 0.36))
        t += 0.4
        i += 1
    return words


class MockWriter(writers.Writer):
    """Returns schema-shaped JSON so the real director code path runs unchanged."""

    name = "mock"
    calls = 0

    def complete_json(self, *, system, user, schema, max_tokens):
        MockWriter.calls += 1
        properties = schema.get("properties", {})
        if "art_direction" in properties:
            return {
                "art_direction": "dark cinematic film still", "palette": "teal and amber",
                "lighting": "low key", "camera": "35mm", "mood": "tense",
                "subjects": [{"name": "Mara", "look": "28, black bob, grey wool coat"}],
                "settings": ["an empty night diner"],
            }
        indices = [int(m) for m in re.findall(r"^\[(\d+)\]", user, re.MULTILINE)]
        return {"prompts": [
            {"index": i,
             "prompt": f"Shot {i}: Mara, 28, black bob, grey wool coat, in the diner. "
                       f"Dark cinematic film still, teal and amber.",
             "shot": ["establishing", "medium", "close", "detail"][i % 4]}
            for i in indices
        ]}


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")
    return condition


def main() -> int:
    print(f"Scratch dir: {TMP}\n")

    transcribe.transcribe = fake_words
    jobs.transcribe = fake_words
    writers.WRITERS["mock"] = MockWriter
    imagegen_base.PROVIDERS["mock"] = MockProvider

    audio = make_audio(TMP / "vo.mp3")
    print(f"Test voiceover: {audio_duration(audio):.1f}s\n")

    job = jobs.STORE.create("Smoke Test Story", {
        "images_per_minute": IMAGES_PER_MINUTE, "aspect": "16:9", "style_preset": "cinematic",
        "provider": "mock", "writer": "mock", "notes": "",
        "transition": "crossfade", "transition_seconds": 0.5,
        "fps": 24, "music_gain_db": -22, "auto_render": True,
    })
    # Mirror what the upload endpoint does.
    import shutil
    shutil.copy(audio, job.dir / audio.name)

    started = time.time()
    jobs._run(job, job.dir / audio.name, SCRIPT, None)
    elapsed = time.time() - started
    print(f"\nPipeline finished in {elapsed:.1f}s with status '{job.status}'\n")

    beat_text = " ".join(b.get("text", "") for b in job.beats)
    expected = round(AUDIO_SECONDS / 60 * IMAGES_PER_MINUTE)
    video = job.dir / (job.video or "final.mp4")
    results = [
        check("job completed", job.status == "done", job.error or ""),
        check(f"beat count is ~{expected}", abs(len(job.beats) - expected) <= 1,
              f"got {len(job.beats)}"),
        check("every beat has a prompt", all(b.get("prompt") for b in job.beats)),
        check("script wording survived into the beats", "Mara" in beat_text and "Kaelen" in beat_text,
              "script spellings beat the transcript's"),
        check("alignment reported", bool(job.alignment),
              f"{(job.alignment or {}).get('source')} "
              f"{round((job.alignment or {}).get('ratio', 0) * 100)}% matched"),
        check("every image generated", all(b.get("file") for b in job.beats),
              f"{sum(1 for b in job.beats if b.get('file'))}/{len(job.beats)}"),
        check("beats tile the timeline with no gaps",
              all(abs(b["start"] - a["end"]) < 1e-6 for a, b in zip(job.beats, job.beats[1:]))),
        check("video file exists", video.exists()),
        check("timeline.csv written", (job.dir / "timeline.csv").exists()),
        check("prompts.json written", (job.dir / "prompts.json").exists()),
    ]
    if video.exists():
        length = audio_duration(video)
        results.append(check("video length matches the voiceover",
                             abs(length - AUDIO_SECONDS) < 1.0, f"{length:.2f}s vs {AUDIO_SECONDS}s"))
        results.append(check("video has a real size", video.stat().st_size > 50_000,
                             f"{video.stat().st_size/1e6:.2f} MB"))

    passed = all(results)
    print(f"\n{'ALL CHECKS PASSED' if passed else 'SOME CHECKS FAILED'}"
          f"  ({sum(results)}/{len(results)})")
    if passed:
        print(f"\nRendered video: {video}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
