"""Bounded catch-up of the existing Basic Memory index for one committed note."""
from __future__ import annotations

import asyncio
from pathlib import Path
import re

from .basic_memory import BasicMemoryError
from .index_binding import load_index
from .identifiers import portable_component
from .store import ConflictError, Store, ValidationError


def refresh(store: Store, identifier: str, expected_revision: str,
            query: str, timeout: int = 30) -> dict:
    """Return a verified indexed hit and authoritative full note, or fail closed.

    The caller first commits memory_write. A timeout does not undo that commit.
    Retrying refresh is safe; the same source revision must still be current.
    """
    try:
        valid_identifier = portable_component(identifier) == identifier
    except (TypeError, ValueError):
        valid_identifier = False
    if not valid_identifier:
        raise ValidationError("Invalid note identifier")
    if not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
        raise ValidationError("Invalid expected revision")
    if not isinstance(query, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{3,99}", query):
        raise ValidationError("Use a unique plain-text marker for this note revision")
    if type(timeout) is not int or not 1 <= timeout <= 30:
        raise ValidationError("Invalid index timeout")
    relative = f"Memory/notes/{identifier}.md"
    note = store.read(relative)
    if note["revision"] != expected_revision:
        raise ConflictError("Source revision changed")
    if query not in note["text"].casefold():
        raise ValidationError("Index marker is absent from current source")
    index = load_index(store.root)
    observed = asyncio.run(index.wait_ready(query, timeout=timeout,
                                            target_path=store.root / relative))
    if not observed["ready"]:
        return {"ready": False, "reason": observed["reason"], "path": relative,
                "revision": expected_revision}
    search = observed["search"]
    if isinstance(search, dict) and set(search) == {"result"}:
        search = search["result"]
    expected = (store.root / relative).resolve()
    matches = []
    for hit in search["results"]:
        raw = Path(hit["file_path"])
        if raw.is_absolute():
            path = raw.resolve()
        elif raw.parts and raw.parts[0] == "Memory":
            path = (store.root / raw).resolve()
        else:
            path = (store.root / "Memory" / raw).resolve()
        indexed_text = "\n".join(value for value in
            (hit.get("matched_chunk"), hit.get("content")) if isinstance(value, str))
        snippets = [value.strip().strip("…").strip() for value in
                    (hit.get("matched_chunk"), hit.get("content")) if isinstance(value, str)]
        if (path == expected and query in indexed_text.casefold() and
                any(part and part in note["text"] for part in snippets)):
            matches.append(hit)
    # A hit for a reused marker can belong to an older indexed revision.
    # Require the source, entity and complete FTS posting observation for
    # the requested revision before reporting readiness.
    if matches and not index._indexed_checksums_match({f"notes/{identifier}.md": expected_revision}):
        return {"ready": False, "reason": "index_revision_unconfirmed",
                "path": relative, "revision": expected_revision}
    current = store.read(relative)
    if current["revision"] != expected_revision:
        raise ConflictError("Source revision changed during indexing")
    return {"ready": bool(matches), "reason": None if matches else "target_marker_not_found",
            "atomic_snapshot": False, "index_coverage": "unknown",
            "scope": "target_marker_index_and_source_observation",
            "path": relative, "revision": expected_revision,
            "index_hit": {"title": matches[0]["title"], "type": matches[0]["type"],
                          "score": matches[0]["score"], "path": relative} if matches else None,
            "note": current if matches else None}
