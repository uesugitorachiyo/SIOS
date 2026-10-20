"""Authenticated central completion, disabled by default; validation instances only.

Adapter scopes authenticate remote principals. SQLite retains coordinator controls,
producer receipts/reports and approvals outside ordinary write/read namespaces.
Trusted local callers/filesystem remain outside the remote-principal boundary.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re

from .store import ValidationError, ConflictError, MAX_BYTES, SECTIONS, STATUSES, _hash, _json, _text


def now():
    return datetime.now(timezone.utc)


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS completion_enrollment(mission TEXT PRIMARY KEY, project TEXT NOT NULL, coordinator TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS completion_controls(id TEXT PRIMARY KEY, mission TEXT NOT NULL, actor TEXT NOT NULL, control TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)')
    db.execute('CREATE TABLE IF NOT EXISTS completion_evidence(id TEXT NOT NULL, reference TEXT NOT NULL, actor TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(id,reference))')
    db.execute('CREATE TABLE IF NOT EXISTS completion_reports(id TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(id,kind))')
    db.execute('CREATE TABLE IF NOT EXISTS completion_approvals(id TEXT PRIMARY KEY, actor TEXT NOT NULL, digest TEXT NOT NULL, expires TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS completion_runs(operation_id TEXT PRIMARY KEY, verification_id TEXT UNIQUE NOT NULL, security_run_id TEXT UNIQUE NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL)')


def engine():
    try:
        from . import verification
        return verification
    except ImportError as exc:
        raise ValidationError('Qualified verification engine unavailable') from exc


def mission_status(content):
    match = re.match(r'\A---\r?\n(.*?)\r?\n---\r?\n', content, re.S)
    fields = {}
    for line in match[1].splitlines() if match else []:
        row = re.fullmatch(r'([a-z][a-z0-9_]*): ?([^\r\n]*)', line)
        if not row or row[1] in fields:
            raise ValidationError('Mission requires canonical unique flat frontmatter')
        fields[row[1]] = row[2]
    if fields.get('status') not in STATUSES:
        raise ValidationError('Mission requires one valid status')
    return fields['status']


def enrolled(db, name):
    return db.execute('SELECT project,coordinator FROM completion_enrollment WHERE mission=?', (name,)).fetchone()


def guard_ordinary(db, path, status):
    parts = path.split('/')
    if len(parts) == 3 and parts[0] == 'Missions' and parts[2] == 'mission.md':
        if status == 'completed' and enrolled(db, parts[1]):
            raise ValidationError('Enrolled completion requires central verification')


def require_validation(store):
    if not store.completion_validation:
        raise ValidationError('Central completion validation is disabled')


def bounded(value):
    encoded = engine().canonical(value).decode('utf-8')
    _text(encoded, 'completion input', MAX_BYTES)
    return encoded


def load_control(db, vid):
    row = db.execute('SELECT control,actor,revoked FROM completion_controls WHERE id=?', (vid,)).fetchone()
    if row is None:
        raise ValidationError('Retained completion authority unavailable')
    if row[2]:
        raise PermissionError('Completion authority revoked')
    return json.loads(row[0]), row[1]


def current_binding(store, control):
    try:
        return _current_binding(store, control)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValidationError('Current completion inputs unavailable or invalid') from exc


def _current_binding(store, control):
    v = engine()
    b = dict(control['request']['binding'])
    name = b['mission_id']
    b['mission_revision'] = _hash(store._bytes(f'Missions/{name}/mission.md'))
    root = store._path(v.path_name(control['candidate_root']))
    if not root.is_dir():
        raise ValidationError('Current candidate unavailable')
    excluded = control['candidate_exclusions']
    if not isinstance(excluded, list) or len(set(excluded)) != len(excluded):
        raise ValidationError('Invalid exclusions')
    for entry in excluded:
        v.path_name(entry)
    names, size = [], 0
    def failed_walk(error):
        raise ValidationError('Incomplete candidate inventory') from error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed_walk):
        for item in list(dirs):
            p = root.__class__(directory) / item
            store._check_node(p)
            if p.relative_to(root).as_posix() in excluded:
                dirs.remove(item)
        for item in files:
            p = root.__class__(directory) / item
            store._check_node(p)
            relative = p.relative_to(root).as_posix()
            if relative in excluded:
                continue
            names.append(v.path_name(relative))
            size += p.stat().st_size
            if size > 32 * 1024 * 1024 or len(names) > 5000:
                raise ValidationError('Candidate inventory budget exceeded')
    names.sort()
    if names != control['candidate_files']:
        raise ValidationError('Current candidate inventory differs')
    b['content_sha256'] = v.digest(v.capture(root, names, b['project_id'], b['snapshot_id']))
    b['scope_sha256'] = v.digest({'files': names, 'excluded': excluded})
    keys = {'spec_sha256', 'acceptance_sha256', 'policy_sha256', 'profile_sha256', 'rules_sha256',
            'runner_sha256', 'permissions_sha256', 'adapter_sha256'}
    if b['corpus_sha256'] is not None:
        keys.add('corpus_sha256')
    v.exact(control['bound_files'], keys)
    for key, name in control['bound_files'].items():
        # capture performs stable regular-file/reparse/size/identity checks.
        name = v.path_name(name)
        if store._read_path(name).stat().st_size > MAX_BYTES:
            raise ValidationError('Oversized bound material')
        b[key] = v.capture(store.root, [name], b['project_id'], b['snapshot_id'])['entries'][0]['content_sha256']
    return b


def register(store, control, actor):
    require_validation(store)
    v = engine()
    encoded = bounded(control)
    v.exact(control, {'schema_version', 'request', 'candidate_root', 'candidate_files', 'candidate_exclusions',
                      'bound_files', 'desired_mission_sha256', 'submitter', 'report_actors',
                      'receipt_producers', 'approval_required', 'approval_actor'})
    if control['schema_version'] != '1.0' or type(control['approval_required']) is not bool:
        raise ValidationError('Invalid completion control')
    route = v.validate_request(control['request'])
    b = control['request']['binding']
    if b['stage'] != 'complete' or b['artifact_sha256'] is not None:
        raise ValidationError('Only development completion supported')
    v.opaque(control['submitter'])
    v.exact(control['report_actors'], {'qa', 'security'})
    for person in control['report_actors'].values():
        v.opaque(person)
    if route['reviewer_dispatch'] == 'REQUIRED' and control['report_actors']['qa'] != control['request']['reviewer_actor']:
        raise ValidationError('Review report principal differs from approved reviewer')
    if control['approval_required']:
        v.opaque(control['approval_actor'])
        if control['approval_actor'] in {actor, control['submitter'], *control['report_actors'].values()}:
            raise ValidationError('Approval identity must be independent')
    elif control['approval_actor'] is not None:
        raise ValidationError('Unexpected approval identity')
    if not v.SHA.fullmatch(control['desired_mission_sha256']):
        raise ValidationError('Desired mission digest required')
    if not isinstance(control['receipt_producers'], dict) or not control['receipt_producers']:
        raise ValidationError('Receipt producers required')
    for reference, producer in control['receipt_producers'].items():
        v.opaque(reference)
        v.exact(producer, {'actor', 'session', 'kind'})
        for key in ('actor', 'session'):
            v.opaque(producer[key])
        if producer['kind'] not in ('context', 'mechanical', 'automated', 'independent_review'):
            raise ValidationError('Unsupported producer kind')
    vid = control['request']['request_id']
    with store._connection() as db:
        if (db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='release_controls'").fetchone()
                and db.execute('SELECT 1 FROM release_controls WHERE id=?', (vid,)).fetchone()):
            raise ConflictError('Verification ID belongs to release validation')
        previous = db.execute('SELECT control,actor,revoked FROM completion_controls WHERE id=?', (vid,)).fetchone()
        if previous:
            if previous != (encoded, actor, 0):
                raise ConflictError('Verification ID already registered or revoked')
            return {'verification_id': vid, 'control_digest': v.digest(control), 'enforcement_active': False}
        registration = enrolled(db, b['mission_id'])
        if registration and registration != (b['project_id'], actor):
            raise PermissionError('Wrong enrollment project/coordinator')
        if current_binding(store, control) != b or not v.stamp(control['request']['issued_at']) <= now() < v.stamp(control['request']['expires_at']):
            raise ValidationError('Registration requires current candidate and validity')
        db.execute('INSERT OR IGNORE INTO completion_enrollment VALUES(?,?,?)', (b['mission_id'], b['project_id'], actor))
        db.execute('INSERT INTO completion_controls(id,mission,actor,control) VALUES(?,?,?,?)', (vid, b['mission_id'], actor, encoded))
    return {'verification_id': vid, 'control_digest': v.digest(control), 'enforcement_active': False}


def evidence(store, vid, reference, record, actor):
    require_validation(store)
    v = engine(); encoded = bounded(record)
    with store._connection() as db:
        control, _ = load_control(db, vid)
        expected = control['receipt_producers'].get(reference)
        if expected is None or actor != expected['actor']:
            raise PermissionError('Wrong evidence principal/reference')
        try:
            v.exact(record, {'bytes', 'sha256', 'binding', 'actor', 'session', 'kind', 'count', 'observations', 'started_at', 'finished_at'})
        except ValueError as exc:
            raise ValidationError('Evidence record has missing or unknown fields') from exc
        if any(record[k] != expected[k] for k in ('actor', 'session', 'kind')) or record['binding'] != control['request']['binding']:
            raise ValidationError('Evidence identity differs from retained control')
        if not isinstance(record['bytes'], str) or hashlib.sha256(record['bytes'].encode()).hexdigest() != record['sha256']:
            raise ValidationError('Altered receipt bytes')
        _immutable(db, 'completion_evidence', ('id', 'reference'), (vid, reference), 'record', encoded, actor)
    return {'retained': reference, 'authenticated_actor': actor}


def _immutable(db, table, keys, values, column, encoded, actor):
    where = ' AND '.join(k + '=?' for k in keys)
    old = db.execute(f'SELECT {column},actor FROM {table} WHERE {where}', values).fetchone()
    if old:
        if old != (encoded, actor):
            raise ConflictError('Retained result is immutable; submit a new verification')
    else:
        fields = ','.join((*keys, column, 'actor'))
        placeholders = ','.join('?' for _ in range(len(keys) + 2))
        db.execute(f'INSERT INTO {table}({fields}) VALUES({placeholders})', (*values, encoded, actor))


def report(store, vid, kind, result, actor):
    require_validation(store)
    encoded = bounded(result)
    with store._connection() as db:
        control, _ = load_control(db, vid)
        if kind not in control['report_actors'] or control['report_actors'][kind] != actor:
            raise PermissionError('Wrong report principal')
        if kind == 'qa' and result.get('actor') != actor:
            raise ValidationError('QA report cannot override authenticated principal')
        _immutable(db, 'completion_reports', ('id', 'kind'), (vid, kind), 'result', encoded, actor)
    return {'retained': kind, 'authenticated_actor': actor}


def approve(store, vid, control_digest, expires_at, actor):
    require_validation(store)
    v = engine()
    with store._connection() as db:
        control, _ = load_control(db, vid)
        if not control['approval_required'] or control['approval_actor'] != actor:
            raise PermissionError('Wrong approval principal')
        if v.digest(control) != control_digest or not now() < v.stamp(expires_at) <= v.stamp(control['request']['expires_at']):
            raise ValidationError('Approval target or validity differs')
        if current_binding(store, control) != control['request']['binding']:
            raise ValidationError('Approval candidate no longer current')
        old = db.execute('SELECT actor,digest,expires FROM completion_approvals WHERE id=?', (vid,)).fetchone()
        if old and old != (actor, control_digest, expires_at):
            raise ConflictError('Approval is immutable')
        db.execute('INSERT OR IGNORE INTO completion_approvals VALUES(?,?,?,?)', (vid, actor, control_digest, expires_at))
    return {'approved_scope': 'development_completion', 'enforcement_active': False}


def revoke(store, vid, actor):
    require_validation(store)
    with store._connection() as db:
        _, coordinator = load_control(db, vid)
        if actor != coordinator:
            raise PermissionError('Wrong coordinator')
        db.execute('UPDATE completion_controls SET revoked=1 WHERE id=?', (vid,))
    return {'revoked': vid}


def validate(store, db, vid, content, actor):
    require_validation(store)
    v = engine(); control, coordinator = load_control(db, vid)
    b = control['request']['binding']
    if actor != control['submitter'] or enrolled(db, b['mission_id']) != (b['project_id'], coordinator):
        raise PermissionError('Wrong completion principal/enrollment')
    if _hash(content.encode()) != control['desired_mission_sha256'] or mission_status(content) != 'completed':
        raise ValidationError('Completion target content differs')
    results = {kind: json.loads(encoded) for kind, encoded in db.execute('SELECT kind,result FROM completion_reports WHERE id=?', (vid,))}
    if set(results) != {'qa', 'security'}:
        raise ValidationError('Required producer reports missing')
    registry = {ref: json.loads(encoded) for ref, encoded in db.execute('SELECT reference,record FROM completion_evidence WHERE id=?', (vid,))}
    for r in registry.values():
        r['bytes'] = r['bytes'].encode()
    current = current_binding(store, control)
    result = v.assess(control['request'], results['qa'], results['security'], registry, current, now=now())
    if not result['advisory_eligible']:
        raise ValidationError('Required verification blocked: ' + '; '.join(result['blockers']))
    if current_binding(store, control) != current or current != b:
        raise ValidationError('Identity changed during final evaluation')
    approval = None
    if control['approval_required']:
        approval = db.execute('SELECT actor,digest,expires FROM completion_approvals WHERE id=?', (vid,)).fetchone()
        if not approval or approval[:2] != (control['approval_actor'], v.digest(control)):
            raise PermissionError('Required authentic scoped approval missing')
    instant = now()
    if not v.stamp(control['request']['issued_at']) <= instant < v.stamp(control['request']['expires_at']):
        raise ValidationError('Request expired during validation')
    if approval and instant >= v.stamp(approval[2]):
        raise PermissionError('Approval expired during validation')
    for r in registry.values():
        if v.stamp(r['finished_at']) > instant or (instant - v.stamp(r['finished_at'])).total_seconds() > control['request']['max_age_seconds']:
            raise ValidationError('Evidence expired during validation')
    return results['security']['run_id'], b


def complete(store, name, content, request_id, vid, expected_revision, actor):
    require_validation(store)
    _text(content)
    if any(not re.search(r'^## ' + re.escape(h) + r'\r?$', content, re.M) for h in SECTIONS):
        raise ValidationError('Missing mission section')
    v = engine(); v.opaque(name); v.opaque(vid)
    payload = {'operation': 'mission_complete', 'name': name, 'content': content,
               'verification_id': vid, 'expected_revision': expected_revision}
    def build(db):
        security_run, binding = validate(store, db, vid, content, actor)
        if name != binding['mission_id'] or expected_revision != binding['mission_revision']:
            raise ValidationError('Wrong mission/revision')
        if (db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='release_runs'").fetchone()
                and db.execute('SELECT 1 FROM release_runs WHERE security_run_id=?', (security_run,)).fetchone()):
            raise ConflictError('Security run already reserved by release validation')
        try:
            db.execute('INSERT INTO completion_runs VALUES(?,?,?,?,?,?)', (request_id, vid, security_run, actor, _json(payload), 'prepared'))
        except Exception as exc:
            import sqlite3
            if isinstance(exc, sqlite3.IntegrityError):
                raise ConflictError('Verification or Security run already reserved') from exc
            raise
        return [(f'Missions/{name}/mission.md', content)], {
            'path': f'Missions/{name}/mission.md', 'revision': _hash(content.encode()),
            'verification_id': vid, 'authenticated_actor': actor,
            'boundary': 'authenticated_remote_principals_trusted_local_writer',
            'enforcement_active': False, 'validation_instance': True,
            'release_ready': False, 'released': False}
    return store._operation(request_id, actor, payload, build, with_db=True)


def recovery_guard(store, db, request_id, intent):
    managed = db.execute('SELECT verification_id,actor,payload FROM completion_runs WHERE operation_id=?', (request_id,)).fetchone()
    if managed:
        vid, actor, payload = managed; data = json.loads(payload)
        if intent != [{'path': f"Missions/{data['name']}/mission.md", 'before': data['expected_revision'], 'text': data['content']}]:
            raise ValidationError('Managed recovery intent differs')
        if _hash(store._bytes(intent[0]['path'])) == _hash(data['content'].encode()):
            return None  # Historical replacement already occurred; no new authority.
        return lambda: validate(store, db, vid, data['content'], actor)
    for item in intent:
        if item['path'].startswith('Missions/') and item['path'].endswith('/mission.md'):
            guard_ordinary(db, item['path'], mission_status(item['text']))
    return None


def reject(db, request_id, reason):
    db.execute('UPDATE operations SET complete=-1,receipt=? WHERE request_id=?',
               (_json({'request_id': request_id, 'result': {'state': 'rejected', 'reason': reason}}), request_id))
    db.execute("UPDATE completion_runs SET state='rejected' WHERE operation_id=?", (request_id,))


def applied(db, request_id):
    db.execute("UPDATE completion_runs SET state='applied' WHERE operation_id=?", (request_id,))
