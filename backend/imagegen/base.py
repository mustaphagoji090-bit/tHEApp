"""Provider-agnostic image generation. Add a backend by subclassing ImageProvider."""
from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod

import httpx

from .. import config


class ImageGenError(RuntimeError):
    pass


class ImageProvider(ABC):
    name: str = "base"
    #  Whether the backend accepts a separate negative prompt, or it must be folded into the text.
    supports_negative: bool = False

    @abstractmethod
    def generate(self, prompt: str, *, negative: str, width: int, height: int,
                 seed: int | None = None) -> bytes:
        """Return the encoded image bytes (PNG or JPEG)."""

    @staticmethod
    def _download(url: str) -> bytes:
        with httpx.Client(timeout=180.0, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.content

    @staticmethod
    def _post(url: str, *, headers: dict, json: dict, timeout: float = 300.0) -> dict:
        """POST with retries on rate limits and transient server errors."""
        delay = 2.0
        last: str = ""
        for attempt in range(4):
            try:
                with httpx.Client(timeout=timeout) as client:
                    response = client.post(url, headers=headers, json=json)
                if response.status_code < 400:
                    return response.json()
                last = f"{response.status_code}: {response.text[:300]}"
                # 4xx other than rate limiting will not fix themselves.
                if response.status_code not in (408, 409, 429) and response.status_code < 500:
                    raise ImageGenError(last)
            except httpx.HTTPError as exc:
                last = str(exc)
            if attempt < 3:
                time.sleep(delay)
                delay *= 2
        raise ImageGenError(last or "request failed")


def _resolve_size(width: int, height: int) -> tuple[int, int]:
    return max(256, width), max(256, height)


class FalProvider(ImageProvider):
    name = "fal"
    supports_negative = False

    def generate(self, prompt, *, negative, width, height, seed=None) -> bytes:
        key = os.getenv("FAL_KEY")
        if not key:
            raise ImageGenError("FAL_KEY is not set.")
        width, height = _resolve_size(width, height)
        payload = {
            "prompt": prompt,
            "image_size": {"width": width, "height": height},
            "num_images": 1,
            "output_format": "jpeg",
            "enable_safety_checker": True,
        }
        if seed is not None:
            payload["seed"] = seed
        data = self._post(
            f"https://fal.run/{config.FAL_MODEL}",
            headers={"Authorization": f"Key {key}", "Content-Type": "application/json"},
            json=payload,
        )
        images = data.get("images") or []
        if not images:
            raise ImageGenError(f"fal returned no image: {str(data)[:200]}")
        return self._download(images[0]["url"])


class ReplicateProvider(ImageProvider):
    name = "replicate"
    supports_negative = False

    def generate(self, prompt, *, negative, width, height, seed=None) -> bytes:
        token = os.getenv("REPLICATE_API_TOKEN")
        if not token:
            raise ImageGenError("REPLICATE_API_TOKEN is not set.")
        width, height = _resolve_size(width, height)
        payload = {"input": {
            "prompt": prompt,
            "aspect_ratio": _closest_replicate_ratio(width, height),
            "output_format": "jpg",
            "num_outputs": 1,
        }}
        if seed is not None:
            payload["input"]["seed"] = seed

        data = self._post(
            f"https://api.replicate.com/v1/models/{config.REPLICATE_MODEL}/predictions",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Prefer": "wait"},
            json=payload,
        )
        data = self._await_replicate(data, token)
        output = data.get("output")
        url = output[0] if isinstance(output, list) and output else output
        if not isinstance(url, str):
            raise ImageGenError(f"replicate returned no image: {str(data)[:200]}")
        return self._download(url)

    def _await_replicate(self, data: dict, token: str) -> dict:
        """`Prefer: wait` usually returns a finished prediction; poll if it did not."""
        deadline = time.time() + 300
        while data.get("status") in ("starting", "processing") and time.time() < deadline:
            time.sleep(2.0)
            url = (data.get("urls") or {}).get("get")
            if not url:
                break
            with httpx.Client(timeout=60.0) as client:
                data = client.get(url, headers={"Authorization": f"Bearer {token}"}).json()
        if data.get("status") == "failed":
            raise ImageGenError(f"replicate prediction failed: {data.get('error')}")
        return data


def _closest_replicate_ratio(width: int, height: int) -> str:
    ratio = width / height
    options = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4}
    return min(options, key=lambda k: abs(options[k] - ratio))


class OpenAIProvider(ImageProvider):
    name = "openai"
    supports_negative = False

    def generate(self, prompt, *, negative, width, height, seed=None) -> bytes:
        import base64

        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise ImageGenError("OPENAI_API_KEY is not set.")
        data = self._post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": config.OPENAI_IMAGE_MODEL,
                "prompt": prompt,
                "size": _openai_size(width, height),
                "n": 1,
            },
        )
        items = data.get("data") or []
        if not items:
            raise ImageGenError(f"openai returned no image: {str(data)[:200]}")
        first = items[0]
        if first.get("b64_json"):
            return base64.b64decode(first["b64_json"])
        return self._download(first["url"])


def _openai_size(width: int, height: int) -> str:
    ratio = width / height
    if ratio > 1.2:
        return "1536x1024"
    if ratio < 0.83:
        return "1024x1536"
    return "1024x1024"


PROVIDERS: dict[str, type[ImageProvider]] = {
    "fal": FalProvider,
    "replicate": ReplicateProvider,
    "openai": OpenAIProvider,
}


def available_providers() -> list[str]:
    status = config.provider_status()
    return [name for name in PROVIDERS if status.get(name)]


def get_provider(name: str | None) -> ImageProvider:
    name = name or config.default_image_provider()
    if not name:
        raise ImageGenError(
            "No image provider is configured. Add FAL_KEY, REPLICATE_API_TOKEN or "
            "OPENAI_API_KEY to your .env file."
        )
    if name not in PROVIDERS:
        raise ImageGenError(f"Unknown image provider '{name}'. Choose one of: {', '.join(PROVIDERS)}")
    return PROVIDERS[name]()
