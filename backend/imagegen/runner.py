"""Batch image generation: bounded concurrency, retries, and per-image failure tolerance.

A 40-minute video is 400 API calls. One bad call must never sink the run -- failures are
recorded per beat so the UI can retry just those.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

from .. import config
from .base import ImageGenError, ImageProvider, get_provider

log = logging.getLogger(__name__)

ATTEMPTS = 3
# Folded into the prompt for backends with no separate negative-prompt field (Flux, gpt-image).
CLEANUP_SUFFIX = "No text, no watermark, no caption, no logo."


def _extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def generate_one(
    provider: ImageProvider,
    prompt: str,
    out_path_stem: Path,
    *,
    negative: str,
    width: int,
    height: int,
    seed: int | None = None,
) -> Path:
    """Generate a single image, retrying transient failures. Returns the written file."""
    text = prompt if provider.supports_negative else f"{prompt} {CLEANUP_SUFFIX}"
    delay = 3.0
    last_error: Exception | None = None

    for attempt in range(ATTEMPTS):
        try:
            data = provider.generate(
                text, negative=negative, width=width, height=height, seed=seed
            )
            if not data:
                raise ImageGenError("empty response body")
            destination = out_path_stem.with_suffix(_extension(data))
            # Clear any stale file from a previous run with a different extension.
            for stale in out_path_stem.parent.glob(f"{out_path_stem.name}.*"):
                if stale != destination:
                    stale.unlink(missing_ok=True)
            destination.write_bytes(data)
            return destination
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced to the caller
            last_error = exc
            if attempt < ATTEMPTS - 1:
                time.sleep(delay)
                delay *= 2

    raise ImageGenError(str(last_error))


def generate_all(
    prompts: list[str],
    out_dir: Path,
    *,
    provider_name: str | None,
    width: int,
    height: int,
    negative: str = "",
    indices: Iterable[int] | None = None,
    progress: Callable[[int, int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[int, dict]:
    """Generate every prompt. Returns {index: {'file': name} | {'error': message}}."""
    provider = get_provider(provider_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = list(indices) if indices is not None else list(range(len(prompts)))
    total = len(targets)
    results: dict[int, dict] = {}
    completed = 0
    failures = 0

    def work(index: int) -> tuple[int, dict]:
        if should_stop and should_stop():
            return index, {"error": "cancelled"}
        try:
            path = generate_one(
                provider, prompts[index], out_dir / f"{index:04d}",
                negative=negative, width=width, height=height,
            )
            return index, {"file": path.name}
        except Exception as exc:  # noqa: BLE001 - recorded per image, run continues
            log.warning("Image %s failed: %s", index, exc)
            return index, {"error": str(exc)[:300]}

    with ThreadPoolExecutor(max_workers=max(1, config.IMAGE_CONCURRENCY)) as pool:
        futures = [pool.submit(work, i) for i in targets]
        for future in as_completed(futures):
            index, outcome = future.result()
            results[index] = outcome
            completed += 1
            if "error" in outcome:
                failures += 1
            if progress:
                progress(completed, total, failures)

    return results
