"""Portable, case-stable names for files and identifiers."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_COMPONENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}\Z")
_FILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}(?:\.(?:md|json))?\Z")
_CANONICAL_DIRS = frozenset({"Brain", "Memory", "Missions", "Runs", "Database"})
_DEVICES = frozenset({"con", "prn", "aux", "nul"}) | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def _validate_name(name: str) -> None:
    if name.casefold().split(".", 1)[0] in _DEVICES:
        raise ValueError(f"reserved device name: {name!r}")


def portable_component(value: str) -> str:
    """Return an ASCII, lowercase slug with stable cross-platform spelling.

    ASCII spaces become hyphens. Non-ASCII and punctuation are rejected rather
    than silently transliterated into potentially colliding names.
    """
    if not isinstance(value, str):
        raise TypeError("component must be a string")
    result = unicodedata.normalize("NFC", value).strip().lower().replace(" ", "-")
    if not _COMPONENT.fullmatch(result):
        raise ValueError(f"unsafe portable component: {value!r}")
    _validate_name(result)
    return result


def safe_relative(value: str | Path) -> Path:
    """Validate a portable path without touching the filesystem.

    Names are case-stable on case-insensitive filesystems: the five canonical
    data directories have fixed capitalization and other names are lowercase.
    """
    if not isinstance(value, (str, Path)):
        raise TypeError("relative path must be a string or Path")
    raw = value.as_posix() if isinstance(value, Path) else value
    if not raw or raw.startswith("/") or "\\" in raw or ":" in raw or "\x00" in raw:
        raise ValueError(f"unsafe relative path: {raw!r}")
    parts = raw.split("/")
    if any(part in ("", ".", "..") or part.endswith((".", " ")) for part in parts):
        raise ValueError(f"unsafe relative path: {raw!r}")
    for part in parts:
        if part in _CANONICAL_DIRS:
            continue
        if not _FILE.fullmatch(part):
            raise ValueError(f"unsafe relative path component: {part!r}")
        _validate_name(part)
    return Path(*parts)
