"""Opt-in, bounded agent discussions in the existing authoritative Store database.

Thread bodies are untrusted data. The authenticated actor and private project ACL
come from the server, never from a message payload or display name.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .store import ConflictError, NotFoundError, ValidationError, _text

KINDS = frozenset({'question', 'answer', 'proposal', 'status', 'evidence',
                   'request', 'decision_candidate'})
PROJECT = re.compile(r'[a-z0-9][a-z0-9_-]{0,99}\Z')
PRINCIPAL = PROJECT
MAX_PARTICIPANTS = 8
MAX_MESSAGES = 100
MAX_BODY_BYTES = 8_000


class NotEnabledError(RuntimeError):
    """The optional discussion feature has not been enabled for this Store."""


def _name(value, label):
    if not isinstance(value, str) or not PROJECT.fullmatch(value):
        raise ValidationError(f'Invalid {label}')
    return value


def _thread_id(value):
    if not isinstance(value, str):
        raise ValidationError('Invalid thread ID')
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise ValidationError('Invalid thread ID') from None
    return value


def _revision(value):
    if type(value) is not int or value < 1:
        raise ValidationError('Invalid thread revision')
    return value


def _sequence(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValidationError('Invalid sequence')
    return value


def _time():
    return datetime.now(timezone.utc).isoformat()


def load_project_acl(path: Path, principals: dict[str, frozenset[str]]):
    """Load an exact private principal-to-project allowlist; no wildcard grants."""
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or
            (os.name == 'posix' and (info.st_mode & 0o077 or info.st_uid != os.geteuid()))):
        raise ValidationError('Thread ACL must be an owner-private ordinary file')
    raw = path.read_bytes()
    if len(raw) > 16_384:
        raise ValidationError('Thread ACL exceeds size limit')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValidationError('Duplicate thread ACL key')
            value[key] = item
        return value
    try:
        data = json.loads(raw, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError):
        raise ValidationError('Invalid thread ACL') from None
    if not isinstance(data, dict) or set(data) != {'version', 'projects'} or data['version'] != 1:
        raise ValidationError('Invalid thread ACL')
    projects = data['projects']
    if not isinstance(projects, dict) or not projects:
        raise ValidationError('Thread ACL requires project grants')
    result = {}
    for principal, allowed in projects.items():
        _name(principal, 'principal')
        if principal not in principals or 'threads:read' not in principals[principal]:
            raise ValidationError('Thread ACL principal lacks configured read scope')
        if (not isinstance(allowed, list) or not allowed or len(allowed) > 32
                or any(not isinstance(item, str) or not PROJECT.fullmatch(item) for item in allowed)
                or len(set(allowed)) != len(allowed)):
            raise ValidationError('Invalid thread project grants')
        result[principal] = frozenset(allowed)
    return result


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS agent_threads (
        thread_id TEXT PRIMARY KEY, project TEXT NOT NULL, purpose TEXT NOT NULL,
        owner TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
        next_seq INTEGER NOT NULL, max_messages INTEGER NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS agent_thread_participants (
        thread_id TEXT NOT NULL, principal TEXT NOT NULL, role TEXT NOT NULL,
        last_ack INTEGER NOT NULL DEFAULT 0, revoked_at TEXT,
        last_fetched INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(thread_id, principal))''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(agent_thread_participants)')}
    if 'last_fetched' not in columns:
        db.execute('ALTER TABLE agent_thread_participants ADD COLUMN last_fetched INTEGER NOT NULL DEFAULT 0')
    db.execute('''CREATE TABLE IF NOT EXISTS agent_thread_messages (
        thread_id TEXT NOT NULL, seq INTEGER NOT NULL, message_id TEXT NOT NULL UNIQUE,
        principal TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
        parent_message_id TEXT, recipients TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(thread_id, seq))''')
    db.execute('CREATE INDEX IF NOT EXISTS agent_thread_participant_lookup ON agent_thread_participants(principal,thread_id)')


class AgentThreads:
    def __init__(self, store):
        if not store.agent_threads_enabled:
            raise NotEnabledError('not_enabled')
        self.store = store

    def _project(self, actor, project):
        _name(actor, 'principal')
        _name(project, 'project')
        if project not in self.store.thread_projects.get(actor, frozenset()):
            raise PermissionError('Project is outside principal scope')

    def _visible(self, db, thread_id, actor):
        row = db.execute('''SELECT t.project,t.owner,t.status,t.revision,t.next_seq,t.max_messages,p.role,p.last_ack
                            FROM agent_threads t JOIN agent_thread_participants p ON t.thread_id=p.thread_id
                            WHERE t.thread_id=? AND p.principal=? AND p.revoked_at IS NULL''', (thread_id, actor)).fetchone()
        if row is None:
            raise NotFoundError('Thread not found or not visible')
        self._project(actor, row[0])
        return row

    def create(self, project, purpose, participants, request_id, max_messages=12, *, actor):
        self._project(actor, project)
        _text(purpose, 'purpose', 300)
        if type(max_messages) is not int or not 1 <= max_messages <= MAX_MESSAGES:
            raise ValidationError('Invalid message cap')
        if (not isinstance(participants, list) or len(participants) > MAX_PARTICIPANTS - 1
                or any(not isinstance(item, str) for item in participants)):
            raise ValidationError('Invalid participants')
        if len(set(participants)) != len(participants) or actor in participants:
            raise ValidationError('Duplicate participant')
        members = [actor]
        for principal in participants:
            _name(principal, 'participant')
            if (principal not in self.store.thread_principals
                    or 'threads:read' not in self.store.thread_principals[principal]
                    or project not in self.store.thread_projects.get(principal, frozenset())):
                raise PermissionError('Participant lacks project read grant')
            members.append(principal)

        def build(db):
            now = _time()
            thread_id = str(uuid.uuid4())
            db.execute('''INSERT INTO agent_threads VALUES(?,?,?,?,?,?,?,?,?,?)''',
                       (thread_id, project, purpose, actor, 'open', 1, 1,
                        max_messages, now, now))
            for principal in members:
                db.execute('INSERT INTO agent_thread_participants(thread_id,principal,role) VALUES(?,?,?)',
                           (thread_id, principal, 'owner' if principal == actor else 'participant'))
            return [], {'thread_id': thread_id, 'project': project, 'revision': 1,
                        'status': 'open', 'participants': members, 'max_messages': max_messages}

        return self.store._operation(request_id, actor,
            {'operation': 'thread_create', 'project': project, 'purpose': purpose,
             'participants': members, 'max_messages': max_messages}, build, with_db=True)

    def post(self, thread_id, body, kind, request_id, parent_message_id=None,
             target_participant_ids=None, expected_revision=None, *, actor):
        _thread_id(thread_id)
        _text(body, 'message body', MAX_BODY_BYTES)
        if kind not in KINDS:
            raise ValidationError('Invalid message kind')
        if parent_message_id is not None:
            _thread_id(parent_message_id)
        if expected_revision is not None:
            _revision(expected_revision)
        targets = target_participant_ids or []
        if (not isinstance(target_participant_ids, (list, type(None))) or
                any(not isinstance(item, str) for item in targets) or
                len(targets) > MAX_PARTICIPANTS or len(set(targets)) != len(targets)):
            raise ValidationError('Invalid recipients')
        for target in targets:
            _name(target, 'recipient')

        def build(db):
            row = self._visible(db, thread_id, actor)
            project, owner, status, revision, next_seq, cap, role, last_ack = row
            if status != 'open':
                raise ConflictError('Thread is paused or closed')
            if expected_revision is not None and revision != expected_revision:
                raise ConflictError('Thread revision changed')
            if next_seq > cap:
                raise ConflictError('Thread message cap reached')
            members = {item[0] for item in db.execute(
                'SELECT principal FROM agent_thread_participants WHERE thread_id=? AND revoked_at IS NULL', (thread_id,))}
            if any(target not in members or target == actor for target in targets):
                raise PermissionError('Recipient is outside thread scope')
            if parent_message_id is not None and db.execute(
                    'SELECT 1 FROM agent_thread_messages WHERE thread_id=? AND message_id=?',
                    (thread_id, parent_message_id)).fetchone() is None:
                raise ValidationError('Parent message is unavailable')
            message_id = str(uuid.uuid4())
            now = _time()
            db.execute('''INSERT INTO agent_thread_messages VALUES(?,?,?,?,?,?,?,?,?)''',
                       (thread_id, next_seq, message_id, actor, kind, body,
                        parent_message_id, json.dumps(targets), now))
            db.execute('UPDATE agent_threads SET next_seq=?,revision=?,updated_at=? WHERE thread_id=?',
                       (next_seq + 1, revision + 1, now, thread_id))
            return [], {'thread_id': thread_id, 'message_id': message_id,
                        'seq': next_seq, 'revision': revision + 1, 'committed': True}

        return self.store._operation(request_id, actor,
            {'operation': 'thread_post', 'thread_id': thread_id, 'body': body, 'kind': kind,
             'parent_message_id': parent_message_id, 'target_participant_ids': targets,
             'expected_revision': expected_revision}, build, with_db=True,
            authorize=lambda db: self._visible(db, thread_id, actor))

    def list(self, project, limit=20, after_thread_id=None, *, actor):
        self._project(actor, project)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValidationError('Invalid list limit')
        if after_thread_id is not None:
            _thread_id(after_thread_id)
        with self.store._connection() as db:
            self.store._recover(db)
            rows = db.execute('''SELECT t.thread_id,t.purpose,t.status,t.revision,t.next_seq,p.last_ack
                 FROM agent_threads t JOIN agent_thread_participants p ON t.thread_id=p.thread_id
                 WHERE t.project=? AND p.principal=? AND p.revoked_at IS NULL AND t.thread_id>? ORDER BY t.thread_id LIMIT ?''',
                 (project, actor, after_thread_id or '', limit + 1)).fetchall()
            items = [{'thread_id': item[0], 'purpose': item[1], 'status': item[2],
                      'revision': item[3], 'unread': max(0, item[4] - 1 - item[5])}
                     for item in rows[:limit]]
            return {'threads': items, 'next_cursor': items[-1]['thread_id'] if len(rows) > limit else None}

    def read(self, thread_id, after_seq=0, limit=20, *, actor):
        _thread_id(thread_id)
        _sequence(after_seq)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValidationError('Invalid read limit')
        with self.store._connection() as db:
            self.store._recover(db)
            project, owner, status, revision, next_seq, cap, role, last_ack = self._visible(db, thread_id, actor)
            rows = db.execute('''SELECT seq,message_id,principal,kind,body,parent_message_id,recipients,created_at
                                 FROM agent_thread_messages WHERE thread_id=? AND seq>? ORDER BY seq LIMIT ?''',
                              (thread_id, after_seq, limit + 1)).fetchall()
            messages = [{'seq': row[0], 'message_id': row[1], 'principal': row[2],
                         'kind': row[3], 'body': row[4], 'parent_message_id': row[5],
                         'target_participant_ids': json.loads(row[6]), 'created_at': row[7]}
                        for row in rows[:limit]]
            last_fetched = db.execute(
                'SELECT last_fetched FROM agent_thread_participants WHERE thread_id=? AND principal=?',
                (thread_id, actor)).fetchone()[0]
            return {'thread_id': thread_id, 'project': project, 'status': status,
                    'revision': revision, 'messages': messages,
                    'next_seq': messages[-1]['seq'] if len(rows) > limit else None,
                    'last_acknowledged_seq': last_ack,
                    'last_fetched_seq': last_fetched}

    def fetch(self, thread_id, through_seq, request_id, *, actor):
        """Record the authenticated client's explicit receipt after a successful read."""
        _thread_id(thread_id)
        _sequence(through_seq, minimum=1)
        def build(db):
            row = self._visible(db, thread_id, actor)
            if through_seq > row[4] - 1:
                raise ValidationError('Cannot record fetch of an uncommitted message')
            current = db.execute(
                'SELECT last_fetched FROM agent_thread_participants WHERE thread_id=? AND principal=?',
                (thread_id, actor)).fetchone()[0]
            fetched = max(current, through_seq)
            db.execute('''UPDATE agent_thread_participants SET last_fetched=?
                          WHERE thread_id=? AND principal=?''', (fetched, thread_id, actor))
            return [], {'thread_id': thread_id, 'last_fetched_seq': fetched,
                        'last_acknowledged_seq': row[7]}
        return self.store._operation(request_id, actor,
            {'operation': 'thread_fetch', 'thread_id': thread_id, 'through_seq': through_seq},
            build, with_db=True, authorize=lambda db: self._visible(db, thread_id, actor))

    def ack(self, thread_id, through_seq, request_id, *, actor):
        _thread_id(thread_id)
        _sequence(through_seq)
        def build(db):
            row = self._visible(db, thread_id, actor)
            if through_seq > row[4] - 1:
                raise ValidationError('Cannot acknowledge an uncommitted message')
            acknowledged = max(row[7], through_seq)
            db.execute('UPDATE agent_thread_participants SET last_ack=? WHERE thread_id=? AND principal=?',
                       (acknowledged, thread_id, actor))
            return [], {'thread_id': thread_id, 'last_acknowledged_seq': acknowledged,
                        'acted_on': False, 'replied_to': False}
        return self.store._operation(request_id, actor,
            {'operation': 'thread_ack', 'thread_id': thread_id, 'through_seq': through_seq},
            build, with_db=True, authorize=lambda db: self._visible(db, thread_id, actor))

    def close(self, thread_id, action, expected_revision, request_id, *, actor):
        _thread_id(thread_id)
        _revision(expected_revision)
        if action not in {'pause', 'resume', 'close'}:
            raise ValidationError('Invalid lifecycle action')
        def build(db):
            row = self._visible(db, thread_id, actor)
            if row[1] != actor:
                raise PermissionError('Only the owner may manage a thread')
            if row[3] != expected_revision:
                raise ConflictError('Thread revision changed')
            status = row[2]
            if (action == 'pause' and status != 'open' or
                    action == 'resume' and status != 'paused' or
                    action == 'close' and status == 'closed'):
                raise ConflictError('Invalid lifecycle transition')
            new_status = {'pause': 'paused', 'resume': 'open', 'close': 'closed'}[action]
            revision = row[3] + 1
            db.execute('UPDATE agent_threads SET status=?,revision=?,updated_at=? WHERE thread_id=?',
                       (new_status, revision, _time(), thread_id))
            return [], {'thread_id': thread_id, 'status': new_status, 'revision': revision}
        def authorize(db):
            if self._visible(db, thread_id, actor)[1] != actor:
                raise PermissionError('Only the owner may manage a thread')
        return self.store._operation(request_id, actor,
            {'operation': 'thread_close', 'thread_id': thread_id, 'action': action,
             'expected_revision': expected_revision}, build, with_db=True,
            authorize=authorize)

    def revoke(self, thread_id, participant, expected_revision, request_id, *, actor):
        _thread_id(thread_id)
        _name(participant, 'participant')
        _revision(expected_revision)
        if participant == actor:
            raise ValidationError('Owner cannot revoke itself; close the thread')
        def build(db):
            row = self._visible(db, thread_id, actor)
            if row[1] != actor:
                raise PermissionError('Only the owner may revoke a participant')
            if row[3] != expected_revision:
                raise ConflictError('Thread revision changed')
            if db.execute('''SELECT 1 FROM agent_thread_participants
                             WHERE thread_id=? AND principal=? AND revoked_at IS NULL''',
                          (thread_id, participant)).fetchone() is None:
                raise NotFoundError('Participant not found or already revoked')
            now = _time()
            db.execute('UPDATE agent_thread_participants SET revoked_at=? WHERE thread_id=? AND principal=?',
                       (now, thread_id, participant))
            db.execute('UPDATE agent_threads SET revision=?,updated_at=? WHERE thread_id=?',
                       (row[3] + 1, now, thread_id))
            return [], {'thread_id': thread_id, 'participant': participant,
                        'revoked': True, 'revision': row[3] + 1}
        def authorize(db):
            if self._visible(db, thread_id, actor)[1] != actor:
                raise PermissionError('Only the owner may revoke a participant')
        return self.store._operation(request_id, actor,
            {'operation': 'thread_revoke', 'thread_id': thread_id, 'participant': participant,
             'expected_revision': expected_revision}, build, with_db=True,
            authorize=authorize)
