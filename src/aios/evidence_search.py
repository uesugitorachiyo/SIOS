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
_MAX_CANDIDATES = 5000


class SearchBudgetExceeded(Exception):
    """A page ran out of its bounded time or source-content budget."""


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


def _snapshot_source(call, path, revision):
    """Reconstruct a current oversized Mission log from verified read snapshots."""
    if not path.endswith('/log.md') or not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision):
        raise ValueError('snapshot unavailable')
    def current():
        value = call('read_revision', {'relative': path})
        if (not isinstance(value, dict) or value.get('path') != path or
            value.get('revision') != revision or type(value.get('bytes')) is not int or
            not 0 <= value['bytes'] <= 8_000_000):
            raise ValueError('snapshot is stale or too large')
        return value['bytes']
    size = current()
    root = path.rsplit('/', 1)[0] + '/evidence/log-read-snapshot-' + revision[:12]
    index_path = root + '/index.md'
    index = call('read', {'relative': index_path})
    if (not isinstance(index, dict) or index.get('path') != index_path or
        not isinstance(index.get('text'), str) or
        hashlib.sha256(index['text'].encode('utf-8')).hexdigest() != index.get('revision')):
        raise ValueError('invalid snapshot index')
    text = index['text']
    if ('Source path: `' + path + '`') not in text or ('Source SHA-256: `' + revision + '`') not in text:
        raise ValueError('snapshot does not identify its source')
    parts = re.findall(r'^\| \[(part-\d{3}\.md)\]\(part-\d{3}\.md\) \| (\d+) \| `([0-9a-f]{64})` \|$', text, re.MULTILINE)
    if not parts or len(parts) > 16 or sum(int(row[1]) for row in parts) != size:
        raise ValueError('invalid snapshot parts')
    chunks = []
    for number, (name, length, digest) in enumerate(parts, 1):
        if name != f'part-{number:03d}.md':
            raise ValueError('invalid snapshot order')
        part_path = root + '/' + name
        part = call('read', {'relative': part_path})
        if not isinstance(part, dict) or part.get('path') != part_path or not isinstance(part.get('text'), str):
            raise ValueError('snapshot part unavailable')
        raw = part['text'].encode('utf-8')
        if len(raw) != int(length) or hashlib.sha256(raw).hexdigest() != digest or part.get('revision') != digest:
            raise ValueError('snapshot part changed')
        chunks.append(raw)
    combined = b''.join(chunks)
    if len(combined) != size or hashlib.sha256(combined).hexdigest() != revision or current() != size:
        raise ValueError('snapshot source changed')
    return {'path': path, 'revision': revision, 'text': combined.decode('utf-8'),
            'read_method': 'verified_snapshot'}


def _snapshot_status(call, path, source, cache):
    match = re.fullmatch(r'(Missions/[^/]+)/evidence/(log-read-snapshot-([0-9a-f]{12}))/[^/]+\.md', path)
    if not match:
        return None
    mission, folder, prefix = match.groups()
    root = mission + '/evidence/' + folder
    if root not in cache:
        status = 'unknown'
        parts = {}
        try:
            index_path = root + '/index.md'
            index = call('read', {'relative': index_path})
            text = index.get('text') if isinstance(index, dict) else None
            if (not isinstance(text, str) or index.get('path') != index_path or
                hashlib.sha256(text.encode('utf-8')).hexdigest() != index.get('revision')):
                raise ValueError('invalid index')
            source_path = mission + '/log.md'
            match_revision = re.search(r'^Source SHA-256: `([0-9a-f]{64})`$', text, re.MULTILINE)
            if (not match_revision or not match_revision.group(1).startswith(prefix) or
                ('Source path: `' + source_path + '`') not in text):
                raise ValueError('invalid source')
            current = call('read_revision', {'relative': source_path})
            if not isinstance(current, dict) or current.get('path') != source_path or not re.fullmatch(r'[0-9a-f]{64}', str(current.get('revision', ''))):
                raise ValueError('unavailable source revision')
            status = ('index references current log; full snapshot unchecked'
                      if current['revision'] == match_revision.group(1)
                      else 'index references older log; full snapshot unchecked')
            rows = re.findall(r'^\| \[(part-\d{3}\.md)\]\(part-\d{3}\.md\) \| (\d+) \| `([0-9a-f]{64})` \|$', text, re.MULTILINE)
            if len({row[0] for row in rows}) != len(rows):
                raise ValueError('duplicate snapshot part')
            parts = {name: (int(length), digest) for name, length, digest in rows}
        except SearchBudgetExceeded:
            raise
        except Exception:
            status = 'unknown'
            parts = {}
        cache[root] = (status, parts)
    status, parts = cache[root]
    if status == 'unknown':
        return status
    name = path.rsplit('/', 1)[-1]
    if name == 'index.md':
        return status
    expected = parts.get(name)
    if expected is None or expected != (len(source['text'].encode('utf-8')), source['revision']):
        return 'integrity mismatch'
    return status


def search_evidence(call, query, *, limit=10, offset=0, fingerprint=None):
    """Return bounded full source records and explicit coverage uncertainty."""
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError('query must be nonempty text')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('limit must be an integer from 1 to 50')
    if type(offset) is not int or not 0 <= offset <= 3 * _MAX_CANDIDATES:
        raise ValueError('invalid offset')
    if (offset and (not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint))) or (not offset and fingerprint is not None):
        raise ValueError('invalid result-set fingerprint')
    query = query.strip()
    terms = _terms(query)
    results, errors, warnings, seen = [], [], [], set()
    queues = {}
    snapshot_cache = {}
    truncated = False
    index_limited = False
    for store in ('brain', 'memory', 'missions'):
        hits = []
        successful = False
        failed = False
        direct_memory_ok = True
        try:
            for term in terms:
                if store == 'memory':
                    try:
                        payload = call('memory_index_search', {'query': term})
                        if isinstance(payload, dict) and isinstance(payload.get('result'), dict):
                            payload = payload['result']
                        if (not isinstance(payload, dict) or not isinstance(payload.get('results'), list)
                                or type(payload.get('has_more')) is not bool):
                            raise ValueError('invalid index response')
                        hits.extend(payload['results'])
                        index_limited = index_limited or payload['has_more']
                        successful = True
                    except SearchBudgetExceeded:
                        raise
                    except Exception:
                        failed = True
                    try:
                        found = call('search', {'store': 'memory', 'query': term})
                        if not isinstance(found, list):
                            raise ValueError('invalid direct search response')
                        hits.extend({**hit, 'read_path': hit.get('path')} if isinstance(hit, dict) else hit for hit in found)
                        successful = True
                    except SearchBudgetExceeded:
                        raise
                    except Exception:
                        direct_memory_ok = False
                else:
                    found = call('search', {'store': store, 'query': term})
                    if not isinstance(found, list):
                        raise ValueError('invalid search response')
                    hits.extend(found)
                    successful = True
        except SearchBudgetExceeded:
            raise
        except Exception:
            failed = True
        if failed:
            errors.append({'store': store, 'reason': 'search_unavailable'})
        if store == 'memory' and not direct_memory_ok:
            errors.append({'store': 'memory', 'reason': 'direct_search_unavailable'})
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
            except SearchBudgetExceeded:
                raise
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
        queues[store] = deque(unique[:_MAX_CANDIDATES])
        if len(unique) > _MAX_CANDIDATES:
            truncated = True
            warnings.append({'store': store, 'reason': 'candidate_limit'})
    ordered = []
    while queues:
        for store in tuple(queues):
            hits = queues[store]
            if not hits:
                del queues[store]
                continue
            ordered.append((store, hits.popleft()))
    candidate_keys = []
    for store, hit in ordered:
        path = hit.get('read_path' if store == 'memory' else 'path') if isinstance(hit, dict) else None
        candidate_keys.append((store, path if isinstance(path, str) else None))
    current_fingerprint = hashlib.sha256(json.dumps([query, terms, candidate_keys], ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()
    if offset and fingerprint != current_fingerprint:
        return {'query': query, 'terms': terms, 'results': [],
                'errors': [{'store': 'all', 'reason': 'result_set_changed'}], 'warnings': warnings,
                'truncated': True, 'next_offset': None, 'fingerprint': current_fingerprint}
    if index_limited:
        warnings.append({'store': 'memory', 'reason': 'index_results_limited'})
    next_offset = None
    for position in range(offset, min(offset + limit, len(ordered))):
        store, hit = ordered[position]
        path = hit.get('read_path' if store == 'memory' else 'path') if isinstance(hit, dict) else None
        if not _path_ok(path, _PREFIX[store]):
            errors.append({'store': store, 'reason': 'invalid_result_path'})
            continue
        if path in seen:
            continue
        seen.add(path)
        try:
            try:
                source = call('read', {'relative': path})
            except SearchBudgetExceeded:
                raise
            except Exception:
                if store != 'missions':
                    raise
                source = _snapshot_source(call, path, hit.get('revision'))
            if (not isinstance(source, dict) or source.get('path') != path or
                not isinstance(source.get('text'), str) or not isinstance(source.get('revision'), str)):
                raise ValueError('invalid read response')
        except SearchBudgetExceeded:
            next_offset = position
            truncated = True
            break
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
        try:
            snapshot_status = _snapshot_status(call, path, source, snapshot_cache) if store == 'missions' else None
        except SearchBudgetExceeded:
            next_offset = position
            truncated = True
            break
        row = {'store': store, 'path': path, 'revision': source['revision'],
               'candidate_offset': position,
               'text': source['text'], 'read_method': source.get('read_method', 'full_read'), 'classification': _kind(meta),
               'snapshot_status': snapshot_status,
               'declared_status': meta.get('status'), 'evidence_status': meta.get('evidence_status'),
               'recorded_at': meta.get('recorded_at'), 'last_touched': meta.get('last_touched'),
               'decision_date': meta.get('decision_date'), 'verified_at': meta.get('verified_at'),
               'review_due': meta.get('review_due'), 'review_overdue': _overdue(meta.get('review_due')),
               'stale_after': meta.get('stale_after'), 'stale': _overdue(meta.get('stale_after')),
               'changed_since_search': drift}
        results.append(row)
    if next_offset is None and offset + limit < len(ordered):
        next_offset = offset + limit
    if next_offset is not None or index_limited:
        truncated = True
    return {'query': query, 'terms': terms, 'results': results, 'errors': errors,
            'warnings': warnings, 'truncated': truncated, 'next_offset': next_offset,
            'fingerprint': current_fingerprint}
