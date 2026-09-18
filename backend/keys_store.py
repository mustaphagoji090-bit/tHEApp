"""Local storage for API keys entered through the UI, as an alternative to editing .env.

Keys are written to storage/keys.json (STORAGE_DIR is gitignored, and the file is written
owner-read/write only) and take priority over the same-named environment variable, so pasting
a key into the UI works immediately -- no restart, no editing .env by hand.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

KNOWN_KEYS = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "FAL_KEY", "REPLICATE_API_TOKEN"]


def _path() -> Path:
    return config.STORAGE_DIR / "keys.json"


def _load() -> dict[str, str]:
    path = _path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def get(name: str) -> str | None:
    """A key entered in the UI wins over the same-named .env / shell variable."""
    return _load().get(name) or os.getenv(name) or None


def status() -> dict[str, bool]:
    return {name: bool(get(name)) for name in KNOWN_KEYS}


def masked() -> dict[str, str | None]:
    """A short, non-secret hint for the UI -- never the key itself."""
    out: dict[str, str | None] = {}
    for name in KNOWN_KEYS:
        value = get(name)
        out[name] = f"...{value[-4:]}" if value and len(value) > 4 else ("set" if value else None)
    return out


def save(updates: dict[str, str]) -> None:
    """Merge new values in. A blank value leaves the existing key untouched -- use clear()."""
    current = _load()
    for name, value in updates.items():
        if name not in KNOWN_KEYS:
            continue
        value = (value or "").strip()
        if value:
            current[name] = value
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        log.warning("Could not restrict permissions on %s", path)


def clear(name: str) -> None:
    current = _load()
    if current.pop(name, None) is not None:
        _path().write_text(json.dumps(current, indent=2), encoding="utf-8")
