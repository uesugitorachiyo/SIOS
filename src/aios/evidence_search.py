"""Read-only evidence search using an explicitly supplied SIOS client."""
from __future__ import annotations

import json
import hashlib
import re
import unicodedata
from collections import deque
from datetime import date, datetime

_PREFIX = {'brain': 'Brain/brain/', 'memory': 'Memory/', 'missions': 'Missions/'}
_DATE_KEYS = ('recorded_at', 'review_due', 'stale_after', 'last_touched')
_META_KEYS = ('status', 'type', 'recorded_at', 'review_due', 'stale_after', 'last_touched',
              'source_type', 'evidence_status', 'adoption_status', 'decision_date', 'verified_at')


def _path_ok(path, prefix):
    if not isinstance(path, str) or not path.startswith(prefix) or not path.endswith('.md'):
        return False
    if '\\' in path or ':' in path or any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} for c in path):
        return False
    return all(part and part not in ('.', '..') and not part.startswith('.') for part in path.split('/'))


def _metadata(text, path):
    text = text.replace('\r\n', '\n')
    data = {}
    if text.startswith('---\n'):
        end = text.find('\n---', 4)
        if end >= 0:
            lines = text[4:end].splitlines()
            in_verified = False
            for line in lines:
                if re.match(r'^[A-Za-z_][A-Za-z_0-9]*:', line):
                    in_verified = line.strip() == 'verified:'
                elif in_verified:
                    verified = re.fullmatch(r'\s+(?:-\s+)?at:\s*(\S+)\s*', line)
                    if verified:
                        try:
                            moment = datetime.fromisoformat(verified.group(1).replace('Z', '+00:00'))
                            if moment.tzinfo is not None:
                                data['verified_at'] = verified.group(1)
                        except ValueError:
                            pass
                match = re.fullmatch(r'([a-z_]+):\s*([^\n]*)', line)
                if match and match.group(1) in _META_KEYS:
                    value = match.group(2).strip().strip('"\'')
                    if value and value not in ('null', '~'):
                        data[match.group(1)] = value
    match_path = re.fullmatch(r'Memory/notes/(selective-[0-9a-f]{64})\.md', path)
    marker = None
    if match_path:
        name = match_path.group(1)
        prefix = ('---\ntitle: ' + name + '\ntype: note\npermalink: aios/notes/' + name +
                  '\n---\n\n<!-- aios-selective-memory-v1 -->\n# Selective Memory: ')
        if text.startswith(prefix):
            marker = re.match(r'[^\n]+\n\n```json\n(.*?)\n```', text[len(prefix):], re.DOTALL)
    if marker:
        try:
            embedded = json.loads(marker.group(1))
            if isinstance(embedded, dict) and embedded.get('schema') == 'aios-selective-memory-v1':
                for key in _META_KEYS:
                    if key not in data and isinstance(embedded.get(key), str):
                        data[key] = embedded[key]
        except (ValueError, TypeError):
            pass
    return data


def _overdue(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
        return parsed < date.today() if parsed.isoformat() == value else None
    except ValueError:
        return None


def _kind(meta):
    # Only labels explicitly carried by the document are used as evidence.
    if meta.get('adoption_status') in ('proposal', 'proposed'):
        return 'proposal'
    declared = meta.get('type', '').casefold()
    if declared in ('decision', 'discussion', 'proposal', 'verification'):
        return declared
    return 'unknown'


def _terms(query):
    """Expand a question into a small number of literal, non-semantic terms."""
    stop = {'what', 'where', 'when', 'which', 'why', 'how', 'does', 'did', 'the', 'for', 'and', 'with', 'about', 'status', 'is', 'are', 'can', 'you', 'me', 'please', 'explain', 'tell', 'show', 'could', 'would', 'a', 'an', 'of', 'to', 'in', 'on', 'was', 'were'}
    words = [word for word in re.findall(r'[A-Za-z][A-Za-z0-9_-]{1,63}', query)
             if word.casefold() not in stop]
    candidates = [query]
    if len(words) >= 2 and re.search(re.escape(words[0]) + r'\s+' + re.escape(words[1]), query, re.IGNORECASE):
        candidates.append(words[0] + '-' + words[1])
    candidates.extend(words[:5])
    if words:
        candidates.append(words[-1])
    out = []
    for value in candidates:
        if value.casefold() not in {x.casefold() for x in out}:
            out.append(value)
    return out[:8]


def search_evidence(call, query, *, limit=10):
    """Return bounded full source records and explicit coverage uncertainty."""
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError('query must be nonempty text')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('limit must be an integer from 1 to 50')
    query = query.strip()
    terms = _terms(query)
    results, errors, warnings, seen = [], [], [], set()
    queues = {}
    truncated = False
    for store in ('brain', 'memory', 'missions'):
        hits = []
        successful = False
        failed = False
        try:
            for term in terms:
                if store == 'memory':
                    payload = call('memory_index_search', {'query': term})
                    if isinstance(payload, dict) and isinstance(payload.get('result'), dict):
                        payload = payload['result']
                    if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
                        raise ValueError('invalid index response')
                    hits.extend(payload['results'])
                    truncated = truncated or payload.get('has_more') is True
                else:
                    found = call('search', {'store': store, 'query': term})
                    if not isinstance(found, list):
                        raise ValueError('invalid search response')
                    hits.extend(found)
                successful = True
        except Exception:
            failed = True
            errors.append({'store': store, 'reason': 'search_unavailable'})
        if store == 'memory' and successful:
            warnings.append({'store': 'memory', 'reason': 'index_may_lag'})
        if failed and store == 'missions':
            try:
                catalog = call('catalog', {'store': 'missions'})
                if not isinstance(catalog, dict) or not isinstance(catalog.get('paths'), list):
                    raise ValueError('invalid catalog response')
                paths = catalog['paths']
                if not all(isinstance(path, str) for path in paths):
                    raise ValueError('invalid catalog path')
                paths = sorted(paths, key=lambda path: (not any(term.casefold() in path.casefold() for term in terms), path))
                hits.extend({'path': path, '_catalog': True} for path in paths)
                warnings.append({'store': 'missions', 'reason': 'mission_catalog_partial'})
            except Exception:
                pass
        if not successful and not hits:
            continue
        unique, known = [], set()
        for hit in hits:
            path = hit.get('read_path' if store == 'memory' else 'path') if isinstance(hit, dict) else None
            if _path_ok(path, _PREFIX[store]):
                if path in known:
                    continue
                known.add(path)
            unique.append(hit)
        queues[store] = deque(unique[:limit])
        if len(unique) > limit:
            truncated = True
    attempted = 0
    while queues and attempted < limit:
        for store in tuple(queues):
            if attempted >= limit:
                break
            hits = queues[store]
            if not hits:
                del queues[store]
                continue
            hit = hits.popleft()
            attempted += 1
            path = hit.get('read_path' if store == 'memory' else 'path') if isinstance(hit, dict) else None
            if not _path_ok(path, _PREFIX[store]):
                errors.append({'store': store, 'reason': 'invalid_result_path'})
                continue
            if path in seen:
                continue
            seen.add(path)
            try:
                source = call('read', {'relative': path})
                if (not isinstance(source, dict) or source.get('path') != path or
                    not isinstance(source.get('text'), str) or not isinstance(source.get('revision'), str)):
                    raise ValueError('invalid read response')
            except Exception:
                errors.append({'store': store, 'reason': 'source_unavailable', 'path': path})
                continue
            try:
                actual_revision = hashlib.sha256(source['text'].encode('utf-8')).hexdigest()
            except UnicodeError:
                actual_revision = None
            if (re.fullmatch(r'[0-9a-f]{64}', source['revision']) is None or
                    actual_revision != source['revision']):
                errors.append({'store': store, 'reason': 'source_integrity_mismatch', 'path': path})
                continue
            if hit.get('_catalog') is True and not any(term.casefold() in source['text'].casefold() for term in terms):
                continue
            meta = _metadata(source['text'], path)
            old_revision = hit.get('revision')
            if old_revision is not None and (not isinstance(old_revision, str) or re.fullmatch(r'[0-9a-f]{64}', old_revision) is None):
                errors.append({'store': store, 'reason': 'invalid_search_revision', 'path': path})
                drift = None
            else:
                drift = old_revision != source['revision'] if old_revision is not None else None
            row = {'store': store, 'path': path, 'revision': source['revision'],
                   'text': source['text'], 'classification': _kind(meta),
                   'declared_status': meta.get('status'), 'evidence_status': meta.get('evidence_status'),
                   'recorded_at': meta.get('recorded_at'), 'last_touched': meta.get('last_touched'),
                   'decision_date': meta.get('decision_date'), 'verified_at': meta.get('verified_at'),
                   'review_due': meta.get('review_due'), 'review_overdue': _overdue(meta.get('review_due')),
                   'stale_after': meta.get('stale_after'), 'stale': _overdue(meta.get('stale_after')),
                   'changed_since_search': drift}
            results.append(row)
            if not hits:
                del queues[store]
    if any(remaining for remaining in queues.values()):
        truncated = True
    return {'query': query, 'terms': terms, 'results': results, 'errors': errors,
            'warnings': warnings, 'truncated': truncated}
