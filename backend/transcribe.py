"""Voiceover -> word-level timestamps. These timings are what the whole timeline hangs off."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from . import config, keys_store, media

# The Whisper endpoint rejects uploads over 25 MB; stay comfortably under it.
MAX_UPLOAD_BYTES = 24 * 1024 * 1024


@dataclass
class Word:
    text: str
    start: float
    end: float


class TranscriptionError(RuntimeError):
    pass


def _post_chunk(path: Path, api_key: str, offset: float) -> list[Word]:
    with httpx.Client(timeout=900.0) as client:
        with path.open("rb") as handle:
            response = client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (path.name, handle, "audio/mpeg")},
                data=[
                    ("model", config.WHISPER_MODEL),
                    ("response_format", "verbose_json"),
                    ("timestamp_granularities[]", "word"),
                    ("timestamp_granularities[]", "segment"),
                ],
            )
    if response.status_code != 200:
        raise TranscriptionError(f"Whisper API error {response.status_code}: {response.text[:400]}")

    payload = response.json()
    words = [
        Word(text=item["word"], start=float(item["start"]) + offset, end=float(item["end"]) + offset)
        for item in payload.get("words") or []
    ]
    if words:
        return words

    # Some models return segments but no word array; fall back to segment timings.
    return [
        Word(text=seg.get("text", "").strip(),
             start=float(seg["start"]) + offset,
             end=float(seg["end"]) + offset)
        for seg in payload.get("segments") or []
        if seg.get("text", "").strip()
    ]


def transcribe(
    audio_path: Path,
    work_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> list[Word]:
    """Transcribe with word timings, splitting long files to stay under the upload limit."""
    api_key = keys_store.get("OPENAI_API_KEY")
    if not api_key:
        raise TranscriptionError(
            "Transcription needs an OpenAI API key (Whisper). Paste one in Settings -- it is "
            "what gives every image its exact in/out timecode."
        )

    note = progress or (lambda _msg: None)
    work_dir.mkdir(parents=True, exist_ok=True)

    note("Compressing voiceover for transcription")
    prepared = media.prepare_for_transcription(audio_path, work_dir / "for_whisper.mp3")

    if prepared.stat().st_size <= MAX_UPLOAD_BYTES:
        pieces = [(prepared, 0.0)]
    else:
        note("Splitting voiceover into chunks")
        pieces = media.split_audio(prepared, work_dir / "chunks", chunk_seconds=600)

    words: list[Word] = []
    for index, (piece, offset) in enumerate(pieces, start=1):
        note(f"Transcribing part {index}/{len(pieces)}")
        words.extend(_post_chunk(piece, api_key, offset))

    if not words:
        raise TranscriptionError("Whisper returned no speech. Is the voiceover file silent?")

    words.sort(key=lambda w: w.start)
    return words
