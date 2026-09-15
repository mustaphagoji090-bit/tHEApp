"""Turns script beats into image-generation prompts.

Two passes. First a 'style bible' is written once for the whole video -- art direction plus a
locked physical description for every recurring character. Then each beat gets a standalone
prompt that restates those locked descriptions verbatim. Image models have no memory between
calls, so repeating the description is the only thing keeping a character's face consistent
across 400 images.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from . import config
from .beats import Beat
from .writers import RefusalError, WriterError, get_writer

log = logging.getLogger(__name__)

BEATS_PER_CALL = 16
DIRECTOR_WORKERS = 4
SHOT_MOTION = {
    "wide": "pan_right", "establishing": "zoom_out", "medium": "zoom_in",
    "close": "zoom_in", "detail": "zoom_in", "over_shoulder": "pan_left",
}
MOTION_CYCLE = ["zoom_in", "pan_right", "zoom_out", "pan_left"]


@dataclass
class Character:
    name: str
    look: str


@dataclass
class StyleBible:
    art_direction: str
    palette: str
    lighting: str
    camera: str
    mood: str
    characters: list[Character] = field(default_factory=list)
    settings: list[str] = field(default_factory=list)

    def suffix(self) -> str:
        parts = [self.art_direction, self.palette, self.lighting, self.camera]
        return ", ".join(p.strip() for p in parts if p and p.strip())

    def cast_sheet(self) -> str:
        if not self.characters:
            return "(no recurring characters)"
        return "\n".join(f"- {c.name}: {c.look}" for c in self.characters)

    def to_dict(self) -> dict:
        return {
            "art_direction": self.art_direction, "palette": self.palette,
            "lighting": self.lighting, "camera": self.camera, "mood": self.mood,
            "characters": [{"name": c.name, "look": c.look} for c in self.characters],
            "settings": self.settings,
        }


class DirectorError(RuntimeError):
    pass


STYLE_SCHEMA = {
    "type": "object",
    "properties": {
        "art_direction": {"type": "string"},
        "palette": {"type": "string"},
        "lighting": {"type": "string"},
        "camera": {"type": "string"},
        "mood": {"type": "string"},
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "look": {"type": "string"}},
                "required": ["name", "look"],
                "additionalProperties": False,
            },
        },
        "settings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["art_direction", "palette", "lighting", "camera", "mood", "characters", "settings"],
    "additionalProperties": False,
}

PROMPTS_SCHEMA = {
    "type": "object",
    "properties": {
        "prompts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "prompt": {"type": "string"},
                    "shot": {
                        "type": "string",
                        "enum": ["establishing", "wide", "medium", "close", "detail", "over_shoulder"],
                    },
                },
                "required": ["index", "prompt", "shot"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["prompts"],
    "additionalProperties": False,
}


def _json(writer, *, system: str, user: str, schema: dict, max_tokens: int) -> dict:
    try:
        return writer.complete_json(system=system, user=user, schema=schema, max_tokens=max_tokens)
    except RefusalError as exc:
        raise DirectorError(
            f"The prompt writer declined this script ({exc}). Try softening the most graphic lines."
        ) from exc
    except WriterError as exc:
        raise DirectorError(str(exc)) from exc


STYLE_SYSTEM = """You are the art director for a faceless AI-narrated story video on YouTube.

You will be given the title and the narration script. Produce a style bible that every one of \
the video's images will follow, so the finished video looks like one film rather than a pile of \
unrelated pictures.

Rules:
- art_direction, palette, lighting, camera: short comma-separated phrase fragments suitable for \
appending to an image-generation prompt. No sentences, no preamble.
- characters: every person the narration returns to. `look` must be a LOCKED physical description \
-- age, build, hair, face, clothing, distinguishing features -- concrete enough that repeating it \
verbatim yields the same person every time. Never reference the plot in `look`. 6-25 words.
- Do not invent characters who never appear. If the story is narrated with no recurring people, \
return an empty characters list.
- settings: the recurring locations, one short visual phrase each."""

PROMPT_SYSTEM = """You write prompts for a text-to-image model, one per beat of a narrated story video.

STYLE BIBLE (every image obeys this)
{style}

CAST -- copy these descriptions VERBATIM whenever the character appears
{cast}

RECURRING SETTINGS
{settings}

Rules for every prompt you write:
1. Self-contained. The image model sees nothing but this one prompt -- no memory of other beats. \
If a cast member appears, restate their locked description word for word.
2. Describe ONE frozen moment that a viewer could photograph: subject, action, setting, framing. \
Never describe narration, voiceover, story structure, or the passage of time.
3. No text, letters, numbers, logos, captions or speech bubbles in the image.
4. Open with the subject and action; end with the style fragment. 35-70 words.
5. Vary the shot type between consecutive beats -- do not shoot everything as a close-up.
6. If a beat's narration is abstract (a moral, a rhetorical question, a transition), invent a \
concrete symbolic image consistent with the story's world rather than anything literal.
7. Keep every image safe for advertisers: no gore, nudity, or graphic injury. Imply violence \
through aftermath, shadow, reaction and negative space instead of depicting it.

Return one entry per beat you are given, keeping the same index numbers."""


def plan_style(title: str, script: str, style_preset: str, extra_notes: str = "",
               writer_name: str | None = None) -> StyleBible:
    writer = get_writer(writer_name)
    preset_hint = config.STYLE_PRESETS.get(style_preset, style_preset or "")
    script_excerpt = script[:24000]

    user = (
        f"TITLE: {title or '(untitled)'}\n\n"
        f"REQUESTED VISUAL STYLE: {preset_hint}\n"
        + (f"EXTRA DIRECTION FROM THE CREATOR: {extra_notes}\n" if extra_notes else "")
        + f"\nSCRIPT:\n{script_excerpt}"
    )
    data = _json(writer, system=STYLE_SYSTEM, user=user, schema=STYLE_SCHEMA, max_tokens=8000)
    return StyleBible(
        art_direction=data["art_direction"], palette=data["palette"], lighting=data["lighting"],
        camera=data["camera"], mood=data["mood"],
        characters=[Character(name=c["name"], look=c["look"]) for c in data.get("characters", [])],
        settings=list(data.get("settings", [])),
    )


def _fallback_prompt(beat: Beat, style: StyleBible) -> str:
    """Used when a batch fails -- keeps the run going instead of losing the whole video."""
    text = (beat.text or "a quiet moment in the story").strip()
    return f"A cinematic depiction of: {text[:240]}. {style.suffix()}. No text or watermarks."


def _write_batch(writer, system_text: str, batch: list[Beat], context: str) -> dict[int, dict]:
    listing = "\n".join(f"[{b.index}] ({b.duration:.1f}s) {b.text or '(no narration)'}" for b in batch)
    user = (
        (f"PRECEDING NARRATION (for continuity, do not illustrate):\n{context}\n\n" if context else "")
        + f"BEATS TO ILLUSTRATE:\n{listing}"
    )
    data = _json(writer, system=system_text, user=user, schema=PROMPTS_SCHEMA, max_tokens=16000)
    return {int(item["index"]): item for item in data.get("prompts", [])}


def write_prompts(
    beats: list[Beat],
    style: StyleBible,
    progress: Callable[[int, int], None] | None = None,
    writer_name: str | None = None,
) -> list[dict]:
    """Return one {prompt, shot, motion} per beat, in beat order."""
    writer = get_writer(writer_name)
    system_text = PROMPT_SYSTEM.format(
        style=json.dumps(
            {"art_direction": style.art_direction, "palette": style.palette,
             "lighting": style.lighting, "camera": style.camera, "mood": style.mood},
            indent=2),
        cast=style.cast_sheet(),
        settings="\n".join(f"- {s}" for s in style.settings) or "(none)",
    )

    batches = [beats[i:i + BEATS_PER_CALL] for i in range(0, len(beats), BEATS_PER_CALL)]
    done = 0
    results: dict[int, dict] = {}

    def run(batch: list[Beat]) -> dict[int, dict]:
        first = batch[0].index
        context = " ".join(b.text for b in beats[max(0, first - 2):first])
        try:
            return _write_batch(writer, system_text, batch, context)
        except DirectorError:
            raise
        except Exception as exc:
            log.warning("Prompt batch at beat %s failed (%s); using fallback prompts", first, exc)
            return {}

    with ThreadPoolExecutor(max_workers=DIRECTOR_WORKERS) as pool:
        for produced in pool.map(run, batches):
            results.update(produced)
            done += 1
            if progress:
                progress(min(done * BEATS_PER_CALL, len(beats)), len(beats))

    out = []
    for position, beat in enumerate(beats):
        item = results.get(beat.index)
        if item and item.get("prompt"):
            prompt, shot = item["prompt"].strip(), item.get("shot", "medium")
        else:
            prompt, shot = _fallback_prompt(beat, style), "medium"
        motion = SHOT_MOTION.get(shot, MOTION_CYCLE[position % len(MOTION_CYCLE)])
        # Nudge toward variety when several beats in a row share a shot type.
        if position and out[-1]["motion"] == motion:
            motion = MOTION_CYCLE[position % len(MOTION_CYCLE)]
        out.append({"prompt": prompt, "shot": shot, "motion": motion})
    return out
