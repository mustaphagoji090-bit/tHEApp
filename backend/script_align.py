"""Bind your script to the voiceover's timing.

You supply the script, so it -- not the transcript -- is the source of truth for wording.
Whisper is used only for *when* each word is spoken; the script words are then matched onto
those timings. That way a misheard name ("Mara" heard as "Mira") never reaches an image prompt.

If the script is already timed (.srt / .vtt), its own timings are used and transcription is
skipped entirely.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .transcribe import Word

TIMED_SUFFIXES = {".srt", ".vtt"}

# 00:00:04,120 --> 00:00:07,880   (srt uses a comma, vtt a dot; hours are optional in vtt)
CUE_RE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)
# Stage directions and speaker labels are for the reader, not the picture.
BRACKETED = re.compile(r"[\[\(\{][^\]\)\}]{0,80}[\]\)\}]")
SPEAKER_LABEL = re.compile(r"^\s*[A-Z][A-Z .'\-]{1,28}:\s*")
SRT_INDEX = re.compile(r"^\d+\s*$")


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class AlignResult:
    words: list[Word]
    matched: int
    total: int
    source: str  # "script-timed" | "aligned" | "transcript"

    @property
    def ratio(self) -> float:
        return self.matched / self.total if self.total else 0.0

    def to_dict(self) -> dict:
        return {"matched": self.matched, "total": self.total,
                "ratio": round(self.ratio, 3), "source": self.source}


# --------------------------------------------------------------------- parsing
def clean_script(text: str) -> str:
    """Strip stage directions and speaker labels; keep the words that are actually spoken."""
    lines = []
    for raw in text.splitlines():
        line = BRACKETED.sub(" ", raw)
        line = SPEAKER_LABEL.sub("", line)
        if line.strip():
            lines.append(line.strip())
    return "\n".join(lines)


def is_timed(filename: str) -> bool:
    return any(filename.lower().endswith(suffix) for suffix in TIMED_SUFFIXES)


def parse_cues(text: str) -> list[Cue]:
    """Parse an .srt/.vtt into cues. Returns [] if it has no timing lines."""
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if SRT_INDEX.match(lines[0]) and len(lines) > 1:
            lines = lines[1:]
        match = CUE_RE.search(lines[0])
        if not match:
            continue
        start, end = _cue_seconds(match)
        body = clean_script(" ".join(lines[1:])).replace("\n", " ").strip()
        if body:
            cues.append(Cue(start=start, end=end, text=body))
    return cues


def _cue_seconds(match: re.Match) -> tuple[float, float]:
    g = match.groups()
    start = (int(g[0] or 0) * 3600 + int(g[1]) * 60 + int(g[2])
             + int(g[3].ljust(3, "0")) / 1000)
    end = (int(g[4] or 0) * 3600 + int(g[5]) * 60 + int(g[6])
           + int(g[7].ljust(3, "0")) / 1000)
    return start, end


def cues_to_words(cues: list[Cue]) -> list[Word]:
    """Spread each cue's words evenly across the cue's own span."""
    words: list[Word] = []
    for cue in cues:
        tokens = _tokens(cue.text)
        if not tokens:
            continue
        span = max(0.05, cue.end - cue.start) / len(tokens)
        for i, token in enumerate(tokens):
            words.append(Word(text=token,
                              start=cue.start + i * span,
                              end=cue.start + (i + 1) * span))
    return words


# ------------------------------------------------------------------- alignment
def _tokens(text: str) -> list[str]:
    return [t for t in text.split() if any(c.isalnum() for c in t)]


def _normalize(token: str) -> str:
    return "".join(c for c in token.lower() if c.isalnum())


def align_script(script_text: str, words: list[Word], duration: float) -> AlignResult:
    """Give each script word a timestamp borrowed from the transcript."""
    tokens = _tokens(clean_script(script_text))
    if not tokens:
        return AlignResult(words=words, matched=0, total=0, source="transcript")
    if not words:
        return AlignResult(words=_spread(tokens, 0.0, duration), matched=0,
                           total=len(tokens), source="transcript")

    script_norm = [_normalize(t) for t in tokens]
    audio_norm = [_normalize(w.text) for w in words]

    matcher = difflib.SequenceMatcher(a=script_norm, b=audio_norm, autojunk=False)
    times: list[tuple[float, float] | None] = [None] * len(tokens)
    matched = 0
    for i, j, size in matcher.get_matching_blocks():
        for k in range(size):
            times[i + k] = (words[j + k].start, words[j + k].end)
            matched += 1

    if matched == 0:
        # The script and the audio have nothing in common -- trust the transcript instead.
        return AlignResult(words=words, matched=0, total=len(tokens), source="transcript")

    _fill_gaps(tokens, times, duration)
    aligned = [Word(text=tok, start=times[i][0], end=times[i][1]) for i, tok in enumerate(tokens)]
    _enforce_monotonic(aligned)
    return AlignResult(words=aligned, matched=matched, total=len(tokens), source="aligned")


def _fill_gaps(tokens: list[str], times: list, duration: float) -> None:
    """Interpolate timings for script words the transcript did not match."""
    anchors = [i for i, t in enumerate(times) if t is not None]
    first, last = anchors[0], anchors[-1]

    if first > 0:
        _spread_into(times, 0, first, 0.0, times[first][0])
    for a, b in zip(anchors, anchors[1:]):
        if b - a > 1:
            _spread_into(times, a + 1, b, times[a][1], times[b][0])
    if last < len(tokens) - 1:
        end = max(duration, times[last][1] + 0.5)
        _spread_into(times, last + 1, len(tokens), times[last][1], end)


def _spread_into(times: list, start_index: int, stop_index: int,
                 start_time: float, stop_time: float) -> None:
    count = stop_index - start_index
    if count <= 0:
        return
    span = max(0.0, stop_time - start_time) / count
    for k in range(count):
        begin = start_time + k * span
        times[start_index + k] = (begin, begin + span)


def _spread(tokens: list[str], start: float, end: float) -> list[Word]:
    span = max(0.05, end - start) / max(1, len(tokens))
    return [Word(text=t, start=start + i * span, end=start + (i + 1) * span)
            for i, t in enumerate(tokens)]


def _enforce_monotonic(words: list[Word]) -> None:
    """Interpolation can produce tiny overlaps; keep the sequence strictly forward-moving."""
    cursor = 0.0
    for word in words:
        if word.start < cursor:
            word.start = cursor
        if word.end < word.start:
            word.end = word.start
        cursor = word.end
