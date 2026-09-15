"""Slice the voiceover timeline into image 'beats' -- one beat becomes one generated image."""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict

from .transcribe import Word

SENTENCE_END = re.compile(r"[.!?…]['\"”’)]*$")
CLAUSE_END = re.compile(r"[,;:—-]['\"”’)]*$")

# A cut may land this far either side of its grid slot while hunting for a clean break.
WINDOW = 0.45
# How far off-slot a boundary may be and still win, as a fraction of target.
SENTENCE_PULL, CLAUSE_PULL = 0.25, 0.10


@dataclass
class Beat:
    index: int
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["duration"] = round(self.duration, 3)
        return data


def build_beats(words: list[Word], duration: float, images_per_minute: float) -> list[Beat]:
    """Group words into ~60/ipm second beats, cutting at the nearest sentence break."""
    if images_per_minute <= 0:
        raise ValueError("images_per_minute must be positive")
    target = 60.0 / images_per_minute

    if not words:
        return _even_beats(duration, target, "")

    beats: list[Beat] = []
    cursor = 0
    beat_start = words[0].start

    while cursor < len(words):
        cut = _choose_cut(words, cursor, _slot_end(beat_start, target), target)
        chunk = words[cursor:cut + 1]
        beats.append(_make_beat(len(beats), beat_start, chunk[-1].end, chunk))
        beat_start = chunk[-1].end
        cursor = cut + 1

    if not beats:
        return _even_beats(duration, target, " ".join(w.text for w in words))

    # Tile the timeline with no gaps: each beat runs until the next one starts.
    beats[0].start = 0.0
    for current, following in zip(beats, beats[1:]):
        current.end = following.start
    beats[-1].end = max(duration, beats[-1].end)
    return [b for b in beats if b.duration > 0.05]


def _slot_end(beat_start: float, target: float) -> float:
    """Next slot on the fixed `target`-spaced grid.

    Anchoring to an absolute grid rather than to each beat's own start keeps the average
    rate at exactly the requested images-per-minute: a beat cut short to land on a sentence
    makes the following beat longer instead of dragging the whole timeline early.
    """
    slot = (int(beat_start // target) + 1) * target
    if slot - beat_start < target * 0.5:
        slot += target
    return slot


def _choose_cut(words: list[Word], start_index: int, slot_end: float, target: float) -> int:
    """Index of the word this beat should end on -- nearest good boundary to its grid slot."""
    best_index, best_score = None, None
    fallback = len(words) - 1

    for i in range(start_index, len(words)):
        offset = words[i].end - slot_end
        if offset < -target * WINDOW:
            continue
        if offset > target * WINDOW:
            fallback = i
            break

        score = abs(offset)
        text = words[i].text.strip()
        if SENTENCE_END.search(text):
            score -= target * SENTENCE_PULL
        elif CLAUSE_END.search(text):
            score -= target * CLAUSE_PULL

        if best_score is None or score < best_score:
            best_index, best_score = i, score
    else:
        fallback = len(words) - 1

    return best_index if best_index is not None else max(start_index, fallback)


def _make_beat(index: int, start: float, end: float, bucket: list[Word]) -> Beat:
    text = " ".join(w.text.strip() for w in bucket).strip()
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return Beat(index=index, start=start, end=end, text=text)


def _even_beats(duration: float, target: float, text: str) -> list[Beat]:
    """No usable word timings -- fall back to equal slices, splitting text proportionally."""
    count = max(1, round(duration / target))
    slice_len = duration / count
    chunks = _split_text(text, count)
    return [
        Beat(index=i, start=i * slice_len, end=min((i + 1) * slice_len, duration), text=chunks[i])
        for i in range(count)
    ]


def _split_text(text: str, count: int) -> list[str]:
    words = text.split()
    if not words:
        return [""] * count
    per = max(1, len(words) // count)
    chunks = [" ".join(words[i * per:(i + 1) * per]) for i in range(count)]
    leftover = " ".join(words[count * per:])
    if leftover:
        chunks[-1] = (chunks[-1] + " " + leftover).strip()
    return chunks
