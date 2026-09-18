"""Environment-backed settings. Reads .env on import, never overriding real env vars."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


_load_dotenv()

STORAGE_DIR = Path(os.getenv("STORAGE_DIR") or BASE_DIR / "storage")
FRONTEND_DIR = BASE_DIR / "frontend"

# Who writes the image prompts. "openai" or "anthropic"; auto-detected from keys if unset.
DIRECTOR_PROVIDER = os.getenv("DIRECTOR_PROVIDER", "")
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o")
DIRECTOR_MODEL = os.getenv("DIRECTOR_MODEL", "claude-opus-5")
IMAGE_CONCURRENCY = int(os.getenv("IMAGE_CONCURRENCY", "6"))

FAL_MODEL = os.getenv("FAL_MODEL", "fal-ai/flux/dev")
REPLICATE_MODEL = os.getenv("REPLICATE_MODEL", "black-forest-labs/flux-dev")
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")

# width, height for each supported aspect ratio
ASPECTS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}

STYLE_PRESETS = {
    "cinematic": "cinematic film still, dramatic volumetric lighting, shallow depth of field, 35mm anamorphic lens, rich filmic color grade, highly detailed",
    "dark_cinematic": "dark moody cinematic film still, low-key chiaroscuro lighting, deep shadows, desaturated teal and amber grade, 35mm film grain, ominous atmosphere",
    "storybook": "painterly digital storybook illustration, soft warm rim light, textured brush strokes, gentle saturated palette, whimsical detail",
    "anime": "modern anime key visual, cel shaded, crisp linework, dramatic sky, vivid saturated palette, studio quality background art",
    "comic": "graphic novel panel art, bold ink outlines, halftone shading, high contrast dramatic composition, limited duotone palette",
    "photoreal": "photorealistic editorial photograph, natural light, 50mm lens, true-to-life skin texture and materials, subtle grain",
    "biblical": "epic biblical oil painting in the style of the old masters, golden divine light breaking through cloud, dramatic baroque composition, aged canvas texture",
    "horror": "unsettling horror cinematography, harsh single-source light, heavy grain, sickly green and cold blue grade, oppressive negative space",
    "declassified": "found-footage documentary still, mix of black-and-white archival photography, grainy 1970s government file photos and rough pencil witness sketches, desaturated sepia-and-desert palette, heavy film grain and dust, dark vignette, mysterious investigative mood",
}

# Post-processing look baked onto every rendered frame via ffmpeg, independent of the image
# model's own output -- this is what makes mixed-source stills (photos, sketches, renders)
# read as one unified "found footage" film instead of a slideshow of clip art.
POST_EFFECTS = {
    "none": "",
    "film_grain": "noise=alls=10:allf=t+u",
    "vignette": "vignette=PI/5",
    "archival": "eq=saturation=0.6:contrast=1.08:brightness=-0.02,curves=preset=vintage,"
                "vignette=PI/4,noise=alls=14:allf=t+u",
    "bw_found_footage": "hue=s=0,eq=contrast=1.15,vignette=PI/4,noise=alls=18:allf=t+u",
}

DEFAULT_NEGATIVE = (
    "text, watermark, signature, caption, subtitles, logo, ugly, deformed, disfigured, "
    "extra limbs, extra fingers, mutated hands, bad anatomy, blurry, low quality, jpeg artifacts, "
    "collage, split screen, frame, border"
)


def provider_status() -> dict[str, bool]:
    """Which services currently have credentials, for the UI to reflect."""
    return {
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")),
        "fal": bool(os.getenv("FAL_KEY")),
        "replicate": bool(os.getenv("REPLICATE_API_TOKEN")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
    }


def director_provider() -> str | None:
    """Which text model writes the prompts. Prefers OpenAI so one key can run everything."""
    if DIRECTOR_PROVIDER:
        return DIRECTOR_PROVIDER
    status = provider_status()
    for name in ("openai", "anthropic"):
        if status[name]:
            return name
    return None


def default_image_provider() -> str | None:
    explicit = os.getenv("DEFAULT_IMAGE_PROVIDER")
    if explicit:
        return explicit
    status = provider_status()
    for name in ("fal", "replicate", "openai"):
        if status[name]:
            return name
    return None
