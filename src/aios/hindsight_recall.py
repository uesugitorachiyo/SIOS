"""Optional explicit Hindsight recall followed by authorized SIOS source reads."""
from __future__ import annotations

import asyncio
import ipaddress
import re
from urllib.parse import urlsplit
from typing import Any, Callable

from .hindsight_evidence import verify_hindsight_sources

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def recall_verified(*, url: str, bank: str, tag: str, query: str,
                    read: Callable[[str], Any], api_key: str | None = None,
                    client_factory: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Send one explicit query to the configured bank; never retain or retry."""
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("invalid endpoint")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        loopback = host == 'localhost' or (host is not None and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        loopback = False
        parsed = urlsplit(url)
        host = parsed.hostname
    if (not host or parsed.username or parsed.password or parsed.query or parsed.fragment or
            (parsed.scheme != 'https' and not (parsed.scheme == 'http' and loopback))):
        raise ValueError("endpoint requires HTTPS or loopback HTTP")
    if (not isinstance(bank, str) or not _NAME.fullmatch(bank) or
            not isinstance(tag, str) or not _NAME.fullmatch(tag) or
            not isinstance(query, str) or not query.strip() or len(query) > 2000):
        raise ValueError("invalid bank, tag or query")
    if client_factory is None:
        from hindsight_client import Hindsight
        client_factory = Hindsight
    async def query_once():
        client = client_factory(base_url=url, api_key=api_key, timeout=30, max_attempts=1)
        try:
            return await client.arecall(bank_id=bank, query=query.strip(), tags=[tag],
                                        tags_match='all_strict', budget='low', max_tokens=2048)
        finally:
            await client.aclose()

    try:
        response = asyncio.run(query_once())
        hits = response.results
        if not isinstance(hits, list) or len(hits) > 100:
            raise ValueError("invalid recall response")
    except Exception:
        raise RuntimeError("hindsight_recall_unavailable") from None
    pointers = []
    skipped = 0
    for hit in hits:
        metadata = getattr(hit, 'metadata', None)
        hit_tags = getattr(hit, 'tags', None)
        if not isinstance(metadata, dict) or not isinstance(hit_tags, list) or tag not in hit_tags:
            skipped += 1
            continue
        path, revision = metadata.get('source_path'), metadata.get('source_revision')
        if not isinstance(path, str) or not isinstance(revision, str):
            skipped += 1
            continue
        pointer = {'path': path, 'revision': revision}
        try:
            verify_hindsight_sources([pointer], lambda _: (_ for _ in ()).throw(PermissionError()))
        except (ValueError, UnicodeError):
            skipped += 1
            continue
        if pointer not in pointers:
            pointers.append(pointer)
        if len(pointers) == 10:
            break
    if pointers:
        result = verify_hindsight_sources(pointers, read)
    else:
        result = {'results': [], 'coverage': 'unknown', 'current_state': 'unknown',
                  'notice': 'No verifiable pointers; empty recall does not establish absence.'}
    result['unusable_hits'] = skipped
    result['recall_coverage'] = 'unknown'
    result['notice'] += ' Recall snippets are not evidence; tags are not access control.'
    return result
