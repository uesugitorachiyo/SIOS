"""Bounded read-only successor evidence for explicitly requested notes.

Callbacks must use the existing authenticated SIOS read/search route. Nothing
here establishes index completeness, current truth, or write authority.
"""
from __future__ import annotations
import hashlib
import json
import re

PATH = re.compile(r'Memory/notes/(selective-[0-9a-f]{64})\.md\Z')
REVISION = re.compile(r'[0-9a-f]{64}\Z')
MAX_HITS = 10
MAX_SOURCE_BYTES = 131072


def _unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate field')
        obj[key] = value
    return obj


def _verify_source(path, result):
    if not isinstance(result, dict) or result.get('path') != path:
        raise ValueError('bad source')
    text, revision = result.get('text'), result.get('revision')
    if not isinstance(text, str) or not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise ValueError('bad source')
    raw = text.encode('utf-8')
    if len(raw) > MAX_SOURCE_BYTES or hashlib.sha256(raw).hexdigest() != revision:
        raise ValueError('bad source')
    marker = '<!-- aios-selective-memory-v1 -->'
    if text.count(marker) != 1:
        raise ValueError('bad source')
    after = text.split(marker, 1)[1]
    if after.count('```json\n') != 1 or '\n```' not in after:
        raise ValueError('bad source')
    record = json.loads(after.split('```json\n',1)[1].split('\n```',1)[0], object_pairs_hook=_unique)
    if not isinstance(record, dict):
        raise ValueError('bad source')
    predecessor = record.get('supersedes')
    if predecessor is not None and (not isinstance(predecessor, str) or
                                    not re.fullmatch(r'selective-[0-9a-f]{64}', predecessor)):
        raise ValueError('bad source')
    return revision, predecessor


async def inspect(predecessor_path, *, read, search):
    match = PATH.fullmatch(predecessor_path) if isinstance(predecessor_path, str) else None
    if not match:
        return {'status':'unknown','reason':'invalid_predecessor','current_state':'unknown','successors':[]}
    try:
        before, _ = _verify_source(predecessor_path, await read(predecessor_path))
    except Exception:
        return {'status':'unknown','reason':'predecessor_unavailable','current_state':'unknown','successors':[]}
    predecessor = {'path':predecessor_path,'revision':before}
    try:
        page = await search(match.group(1))
        if not isinstance(page, dict) or not isinstance(page.get('results'), list):
            raise ValueError('bad page')
        hits = page['results']
        if len(hits) > MAX_HITS:
            raise ValueError('too many hits')
        total, number, size = page.get('total'), page.get('current_page'), page.get('page_size')
        metadata_complete = (page.get('has_more') is False and page.get('total_is_exact') is True and
                             type(total) is int and total == len(hits) and
                             type(number) is int and number == 1 and
                             type(size) is int and 1 <= size <= MAX_HITS and len(hits) <= size)
        incomplete = not metadata_complete
        paths = []
        for hit in hits:
            if not isinstance(hit, dict) or not isinstance(hit.get('read_path'), str):
                incomplete = True
                continue
            path = hit['read_path']
            if path == predecessor_path:
                continue
            if (not path.startswith('Memory/') or '\\' in path or ':' in path or
                    not path.endswith('.md') or any(part in ('', '.', '..') for part in path.split('/'))):
                incomplete = True
                continue
            if PATH.fullmatch(path) and path not in paths:
                paths.append(path)
        successors = []
        for path in paths:
            try:
                revision, supersedes = _verify_source(path, await read(path))
            except Exception:
                incomplete = True
                continue
            if supersedes == match.group(1):
                successors.append({'path':path,'revision':revision})
        after, _ = _verify_source(predecessor_path, await read(predecessor_path))
        if after != before:
            return {'status':'unknown','reason':'predecessor_changed','current_state':'unknown',
                    'predecessor':predecessor,'successors':[]}
        reason = ('incomplete_search' if incomplete else 'multiple_successors' if len(successors) > 1
                  else 'index_coverage_unproven')
        return {'status':'unknown',
                'reason':reason,'current_state':'unknown','predecessor':predecessor,
                'successors':successors,'index_coverage':'unknown'}
    except Exception:
        return {'status':'unknown','reason':'search_or_read_unavailable','current_state':'unknown',
                'predecessor':predecessor,'successors':[]}
