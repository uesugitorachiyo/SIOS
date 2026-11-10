"""Verify caller-selected Hindsight source pointers against SIOS originals.

This module does not contact Hindsight, import records, or decide relevance.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Callable

_REVISION = re.compile(r"[0-9a-f]{64}\Z")


def _valid_path(path: Any) -> bool:
    if not isinstance(path, str) or not path.startswith("Memory/") or not path.endswith(".md"):
        return False
    if len(path.encode("utf-8")) > 1024 or "\\" in path or "\x00" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def verify_hindsight_sources(candidates: Any, read: Callable[[str], Any]) -> dict[str, Any]:
    """Read up to 10 explicit pointers; compare both advertised and actual hashes."""
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 10:
        raise ValueError("provide between one and ten source pointers")
    results = []
    for item in candidates:
        if not isinstance(item, dict) or set(item) != {"path", "revision"}:
            raise ValueError("each pointer must contain only path and revision")
        path, revision = item["path"], item["revision"]
        if not _valid_path(path) or not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise ValueError("invalid source pointer")
    for item in candidates:
        path, revision = item["path"], item["revision"]
        try:
            source = read(path)
            if not isinstance(source, dict) or source.get("path") != path:
                raise ValueError("unexpected source")
            body, current = source.get("text"), source.get("revision")
            if not isinstance(body, str) or not isinstance(current, str) or not _REVISION.fullmatch(current):
                raise ValueError("invalid source")
            raw = body.encode("utf-8")
            if len(raw) > 131072 or hashlib.sha256(raw).hexdigest() != current:
                raise ValueError("source integrity unconfirmed")
            if current != revision:
                results.append({"path": path, "status": "revision_changed", "current_revision": current})
            else:
                results.append({"path": path, "status": "verified", "revision": current, "text": body})
        except Exception:
            results.append({"path": path, "status": "unconfirmed"})
    return {"results": results, "coverage": "unknown", "current_state": "unknown",
            "notice": "A verified source does not establish relevance or successor completeness."}
