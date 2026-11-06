"""Explicit literal search followed by bounded, revision-checked source reads.

The injected search may scan the entire store and trigger recovery; max_hits
limits full reads only. A successful read never establishes complete coverage.
"""
import hashlib
import re

_REVISION = re.compile(r"[0-9a-f]{64}\Z")

def _note_path(path):
    if not isinstance(path, str) or not path.startswith('Memory/notes/') or not path.endswith('.md'):
        return False
    if '\\' in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        return False
    try:
        if len(path.encode('utf-8')) > 1024:
            return False
    except UnicodeError:
        return False
    return all(part not in ('', '.', '..') for part in path.split('/'))

def verify_literal(literal, search_callable, read_callable, max_hits=6):
    if not isinstance(literal, str) or not literal.strip() or '\x00' in literal:
        raise ValueError('literal must be nonempty text without NUL')
    try:
        if len(literal.encode('utf-8')) > 1000:
            raise ValueError('literal too long')
    except UnicodeError:
        raise ValueError('literal must be valid UTF-8') from None
    if type(max_hits) is not int or not 1 <= max_hits <= 20:
        raise ValueError('max_hits must be an integer from 1 to 20')
    hits = search_callable(literal)
    if not isinstance(hits, list):
        raise ValueError('invalid source-search result')
    result = {'verified': [], 'stale': [], 'malformed': [], 'unreadable': [],
              'unknown_coverage': True, 'results_limited': len(hits) > max_hits}
    for hit in hits[:max_hits]:
        if not isinstance(hit, dict):
            result['malformed'].append({'reason': 'invalid_hit'})
            continue
        path, revision = hit.get('path'), hit.get('revision')
        if not _note_path(path) or not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            result['malformed'].append({'reason': 'invalid_path_or_revision'})
            continue
        try:
            source = read_callable(path)
        except Exception:
            result['unreadable'].append({'path': path, 'reason': 'read_failed'})
            continue
        if not isinstance(source, dict) or source.get('path') != path or not isinstance(source.get('text'), str):
            result['malformed'].append({'path': path, 'reason': 'invalid_source'})
            continue
        text, current = source['text'], source.get('revision')
        try:
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        except UnicodeError:
            result['malformed'].append({'path': path, 'reason': 'invalid_text'})
            continue
        if not isinstance(current, str) or not _REVISION.fullmatch(current) or digest != current:
            result['malformed'].append({'path': path, 'reason': 'invalid_source_revision'})
            continue
        if revision != current:
            result['stale'].append({'path': path, 'reason': 'source_changed'})
            continue
        if literal.casefold() not in text.casefold():
            result['stale'].append({'path': path, 'reason': 'literal_not_found'})
            continue
        result['verified'].append({'path': path, 'revision': current, 'text': text,
                                   'provenance': 'literal_source_search_and_full_read'})
    return result
