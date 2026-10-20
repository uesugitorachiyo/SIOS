"""Selective capture for a trusted SIOS Store and authenticated actor.

Adapted from an earlier selective-memory implementation.
The legacy record marker and permalink are retained for format compatibility.
This module never creates a Store or grants a permission. source_actor is
attributed evidence, not the authenticated submitting principal.
"""
import hashlib
import json
from pathlib import Path
import re
import uuid

SOURCE_TYPES = {'direct_user', 'observed', 'external', 'assistant_inference'}
EVIDENCE_STATUSES = {'stated', 'observed', 'verified', 'unverified'}
SLUG = re.compile(r'[a-z0-9][a-z0-9_-]{0,99}\Z')
REF = re.compile(r'(?:Memory|Missions|Runs|Working|Policies|Brain)/[A-Za-z0-9_./-]+\Z')
NOTE_PREFIX = '<!-- aios-selective-memory-v1 -->\n'


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object')
    return value


def _string(value, label, *, max_length=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > max_length or '\x00' in value:
        raise ValueError(f'Invalid {label}')
    return value.strip()


def _slug(value, label):
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise ValueError(f'Invalid {label}')
    return value


def _date(value, label):
    if value is None:
        return None
    from datetime import datetime
    text = _string(value, label, max_length=40)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f'Invalid {label}') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f'{label} needs a timezone')
    return text


def _reference(value, label):
    value = _string(value, label, max_length=500)
    if value.startswith('codex://threads/'):
        raw_id = value.removeprefix('codex://threads/')
        try:
            if str(uuid.UUID(raw_id)) == raw_id:
                return value
        except ValueError:
            pass
        raise ValueError(f'Invalid {label}')
    if value.startswith('https://'):
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if parsed.hostname and not parsed.username and not parsed.password:
            return value
        raise ValueError(f'Invalid {label}')
    if not REF.fullmatch(value) or '..' in Path(value).parts or value.startswith('Brain/'):
        raise ValueError(f'Invalid {label}')
    return value


def _note_id(record):
    identity = {key: record[key] for key in ('meaning_id', 'evidence_instance', 'source_type', 'source_actor',
                                            'project', 'person', 'task', 'conditions')}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'selective-' + hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _render(record):
    data = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2)
    identifier = _note_id(record)
    frontmatter = (f'---\ntitle: {identifier}\ntype: note\n'
                   f'permalink: aios/notes/{identifier}\n---\n\n')
    return frontmatter + NOTE_PREFIX + '# Selective Memory: ' + record['meaning_id'] + '\n\n```json\n' + data + '\n```\n\n' + record['claim'] + '\n'


def _parse(text):
    frontmatter = None
    if text.startswith('---\n'):
        header, separator, body = text.partition('\n---\n\n')
        if not separator:
            raise ValueError('Malformed selective Memory frontmatter')
        lines = header.split('\n')
        if len(lines) != 4 or lines[0] != '---':
            raise ValueError('Malformed selective Memory frontmatter')
        frontmatter = {}
        for line in lines[1:]:
            key, marker, value = line.partition(': ')
            if not marker or key in frontmatter or key not in {'title', 'type', 'permalink'}:
                raise ValueError('Invalid selective Memory frontmatter')
            frontmatter[key] = value
        if set(frontmatter) != {'title', 'type', 'permalink'}:
            raise ValueError('Invalid selective Memory frontmatter')
        text = body
    if not text.startswith(NOTE_PREFIX):
        raise ValueError('Existing note is not a selective Memory record')
    match = re.search(r'\n```json\n(.*?)\n```\n', text, re.S)
    if not match:
        raise ValueError('Malformed selective Memory record')
    record = json.loads(match.group(1), object_pairs_hook=_unique_object,
                        parse_constant=_reject_nonfinite)
    if not isinstance(record, dict):
        raise ValueError('Malformed selective Memory record')
    if frontmatter is not None:
        identifier = _note_id(record)
        if frontmatter != {'title': identifier, 'type': 'note',
                           'permalink': f'aios/notes/{identifier}'}:
            raise ValueError('Selective Memory frontmatter conflicts with record identity')
    return record


def _record(data):
    _object(data, 'request')
    allowed = {'retain', 'kind', 'request_id', 'meaning_id', 'claim', 'source_type',
               'source_actor', 'source_reference', 'project', 'person', 'task',
               'conditions', 'outcome', 'evidence_status', 'recorded_at',
               'occurred_at', 'verified_at', 'supersedes', 'retrieval_cue',
               'mission_reference', 'evidence_instance', 'equivalence_of'}
    if set(data) - allowed:
        raise ValueError('Unsupported capture field')
    request_id = _string(data.get('request_id'), 'request_id', max_length=183)
    if request_id.startswith('selective-write-'):
        raise ValueError('Reserved request_id prefix')
    source_type = data.get('source_type')
    evidence_status = data.get('evidence_status')
    if source_type not in SOURCE_TYPES or evidence_status not in EVIDENCE_STATUSES:
        raise ValueError('Invalid source type or evidence status')
    if source_type == 'assistant_inference' and evidence_status != 'unverified':
        raise ValueError('Assistant inference cannot be verified or adopted here')
    if source_type == 'direct_user' and evidence_status != 'stated':
        raise ValueError('Direct user statement has stated evidence status')
    if source_type == 'observed' and evidence_status not in {'observed', 'verified'}:
        raise ValueError('Observed source needs observed or verified evidence status')
    if source_type == 'external' and evidence_status not in {'stated', 'unverified'}:
        raise ValueError('External source is attributed, not locally verified')
    source_reference = _reference(data.get('source_reference'), 'source_reference')
    verified_at = _date(data.get('verified_at'), 'verified_at')
    if (evidence_status == 'verified') != (verified_at is not None):
        raise ValueError('verified_at must match verified evidence status')
    conditions = data.get('conditions')
    if not isinstance(conditions, list) or len(conditions) > 12:
        raise ValueError('conditions must be a short list')
    conditions = [_string(item, 'condition', max_length=300) for item in conditions]
    if len(conditions) != len(set(conditions)):
        raise ValueError('Duplicate conditions')
    record = {
        'schema': 'aios-selective-memory-v1',
        'meaning_id': _slug(data.get('meaning_id'), 'meaning_id'),
        'evidence_instance': _slug(data.get('evidence_instance', 'original'), 'evidence_instance'),
        'claim': _string(data.get('claim'), 'claim', max_length=8000),
        'source_type': source_type,
        'source_actor': _string(data.get('source_actor'), 'source_actor', max_length=120),
        'source_reference': source_reference,
        'project': _slug(data.get('project'), 'project'),
        'person': _slug(data['person'], 'person') if data.get('person') is not None else None,
        'task': _slug(data['task'], 'task') if data.get('task') is not None else None,
        'conditions': sorted(conditions),
        'outcome': _string(data.get('outcome'), 'outcome', max_length=500),
        'evidence_status': evidence_status,
        'adoption_status': 'proposal' if source_type == 'assistant_inference' else 'historical_evidence',
        'recorded_at': _date(data.get('recorded_at'), 'recorded_at'),
        'occurred_at': _date(data.get('occurred_at'), 'occurred_at'),
        'verified_at': verified_at,
        'supersedes': _slug(data['supersedes'], 'supersedes') if data.get('supersedes') else None,
        'retrieval_cue': _string(data.get('retrieval_cue'), 'retrieval_cue', max_length=500),
        'mission_reference': _reference(data['mission_reference'], 'mission_reference') if data.get('mission_reference') else None,
    }
    if record['recorded_at'] is None:
        raise ValueError('recorded_at is required; retain it across retries')
    if record['mission_reference'] and not re.fullmatch(r'Missions/[a-z0-9_-]+/(?:mission|log)\.md', record['mission_reference']):
        raise ValueError('mission_reference must identify an existing Mission record')
    if record['supersedes'] and record['supersedes'] == _note_id(record):
        raise ValueError('A record cannot supersede itself')
    equivalence = data.get('equivalence_of')
    if equivalence is not None:
        equivalence = _slug(equivalence, 'equivalence_of')
    return request_id, record, equivalence


def _same_meaning(existing, record, *, equivalence_of=None, note_id=None):
    # A paraphrase with the same judged meaning and provenance is not new
    # evidence. All material evidence fields must stay identical.
    ignore = {'claim', 'recorded_at', 'retrieval_cue', 'source_reference'}
    metadata_equal = {k: v for k, v in existing.items() if k not in ignore} == {
        k: v for k, v in record.items() if k not in ignore}
    same_surface = (existing.get('claim') == record['claim']
                    and existing.get('source_reference') == record['source_reference'])
    return metadata_equal and (same_surface or equivalence_of == note_id)


def _prepare_capture(data):
    """Validate selection before resolving or touching a Store."""
    _object(data, 'request')
    if data.get('retain') is False:
        return {'status': 'skipped', 'reason': 'explicit_nonretention'}
    if data.get('kind') == 'transient':
        return {'status': 'skipped', 'reason': 'transient'}
    if data.get('retain') is not True or data.get('kind') != 'delta':
        raise ValueError('Capture requires retain=true and kind=delta')
    request_id, record, equivalence_of = _record(data)
    identifier = _note_id(record)
    if equivalence_of is not None and equivalence_of != identifier:
        raise ValueError('equivalence_of must identify the existing semantic note')
    return request_id, record, equivalence_of, identifier


def capture_with_store(data, *, store, actor):
    """Reuse capture with an already admitted Store and trusted authenticated actor.

    The adapter must authorize the operation before calling. This function does
    not construct a Store, authenticate a caller, or grant a scope. source_actor
    remains attributed evidence metadata, separate from the journal actor.
    """
    _slug(actor, 'actor')
    _object(data, 'request')
    if {'actor', 'root', 'store'} & set(data):
        raise ValueError('Capture cannot override trusted context')
    prepared = _prepare_capture(data)
    if isinstance(prepared, dict):
        return prepared
    if store is None:
        raise ValueError('Existing AIOS coordination Store is required')
    return _capture_prepared(prepared, store=store, actor=actor)


def _capture_prepared(prepared, *, store, actor):
    request_id, record, equivalence_of, identifier = prepared
    root = Path(store.root).resolve(strict=True)
    for reference in (record['source_reference'], record['mission_reference']):
        if reference and not reference.startswith(('https://', 'codex://threads/')):
            path_reference = root / reference
            if (not path_reference.is_file() or any(node.is_symlink() for node in (path_reference, *path_reference.parents))
                    or not path_reference.resolve().is_relative_to(root)):
                raise ValueError('Local evidence reference is unavailable')
    if record['supersedes']:
        prior = store.read(f"Memory/notes/{record['supersedes']}.md")
        old = _parse(prior['text'])
        if old.get('project') != record['project'] or old.get('person') != record['person']:
            raise ValueError('Superseded note scope differs')
    # The installed Store journal reserves the caller's stable request ID even
    # when the substantive note is a duplicate. A digest avoids retaining an
    # extra copy of a repeated claim. Exact retry matches; altered reuse fails.
    submission = {'record': record, 'equivalence_of': equivalence_of}
    submitted = json.dumps(submission, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    submission_digest = hashlib.sha256(submitted.encode('utf-8')).hexdigest()
    store._operation(request_id, actor,
                     {'operation': 'selective_capture_request', 'sha256': submission_digest},
                     lambda: ([], {'sha256': submission_digest}))
    path = f'Memory/notes/{identifier}.md'
    text = _render(record)
    try:
        existing = store.read(path)
    except Exception as exc:
        if type(exc).__name__ != 'NotFoundError':
            raise
        existing = None
    if equivalence_of is not None and existing is None:
        raise ValueError('equivalence_of requires an existing selective note')
    if existing:
        old = _parse(existing['text'])
        if not _same_meaning(old, record, equivalence_of=equivalence_of, note_id=identifier):
            raise ValueError('Existing semantic key has changed evidence; use a linked correction with a new meaning_id')
        return {'status': 'duplicate', 'path': path, 'revision': existing['revision']}
    try:
        receipt = store.write_memory(identifier, text, 'selective-write-' + request_id, create=True, actor=actor)
    except Exception as exc:
        if type(exc).__name__ != 'ConflictError':
            raise
        current = store.read(path)
        if not _same_meaning(_parse(current['text']), record, equivalence_of=equivalence_of, note_id=identifier):
            raise ValueError('Concurrent semantic-key conflict; reread and reconcile') from exc
        return {'status': 'duplicate', 'path': path, 'revision': current['revision']}
    current = store.read(path)
    if current['text'] != text:
        raise ValueError('Committed Memory content differs from requested capture')
    return {'status': 'captured', 'path': path, 'revision': current['revision'], 'receipt': receipt}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _reject_nonfinite(value):
    raise ValueError('Nonfinite JSON value')


def schema():
    """Small machine-readable contract for the controlled MCP operation."""
    return {
        'schema_version': 1,
        'operation': 'memory_capture(request_json)',
        'skip': {'retain': False, 'reason': 'explicit_nonretention',
                 'or': {'kind': 'transient', 'reason': 'transient'}},
        'capture_required': ['retain', 'kind', 'request_id', 'meaning_id', 'claim',
                             'source_type', 'source_actor', 'source_reference', 'project',
                             'conditions', 'outcome', 'evidence_status', 'recorded_at',
                             'retrieval_cue'],
        'capture_values': {'retain': True, 'kind': 'delta',
                           'source_type': sorted(SOURCE_TYPES),
                           'evidence_status': sorted(EVIDENCE_STATUSES)},
        'optional': ['person', 'task', 'occurred_at', 'verified_at', 'supersedes',
                     'mission_reference', 'evidence_instance', 'equivalence_of'],
        'reference': 'Existing central relative file, HTTPS URL, or codex://threads/<canonical UUID>. A reference is provenance, not approval.',
        'dates': 'Timezone-aware ISO 8601; recorded_at is required and stable across retries. verified_at is required exactly for verified evidence.',
        'identity': 'Agent assigns stable meaning_id and evidence_instance. Use a distinct evidence_instance for an independent test/source event even when meaning, conditions, and outcome match. A changed claim or source reference under one identity requires equivalence_of=<existing note ID> to explicitly judge a derivative paraphrase; otherwise capture rejects it. A correction uses a new meaning_id and supersedes the old selective note ID.',
        'limits': 'Semantic equivalence, source authentication, materiality, privacy, and correct application remain agent judgments.',
    }


MAX_CAPTURE_BYTES = 65536


def parse_capture_json(request_json):
    """Bound and validate the original selective JSON string before Store access.

    Keeping it opaque through generic RPC/MCP framing preserves duplicate-key
    detection here. Excluded content should never be transmitted in the first place.
    """
    if not isinstance(request_json, str):
        raise ValueError('Invalid capture JSON')
    raw = request_json.encode('utf-8', errors='strict')
    if not 0 < len(raw) <= MAX_CAPTURE_BYTES:
        raise ValueError('Capture JSON size limit')
    def finite_float(value):
        import math
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError('Nonfinite JSON value')
        return parsed

    data = json.loads(raw, object_pairs_hook=_unique_object,
                      parse_constant=_reject_nonfinite, parse_float=finite_float)
    _object(data, 'request')
    contract = schema()
    allowed = set(contract['capture_required']) | set(contract['optional'])
    if set(data) - allowed:
        raise ValueError('Unsupported capture field')
    return data
