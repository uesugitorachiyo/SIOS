"""Local Markdown state with SQLite-coordinated, recoverable file transactions.

Requires a trusted local filesystem. Cooperating processes serialize through SQLite;
manual concurrent filesystem changes and hostile path swaps are not supported.
Prepared intents survive process interruption; multi-file visibility to external
readers is not atomic. File fsync is required; directory fsync is best effort.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import unicodedata
from zoneinfo import ZoneInfo

from .identifiers import portable_component, safe_relative


class ValidationError(ValueError):
    pass


class ConflictError(ValueError):
    pass


class RecoveryError(ValueError):
    pass


class NotFoundError(FileNotFoundError):
    pass


MAX_BYTES = 2_000_000
MAX_MISSION_SEARCH_BYTES = 16_000_000
MAX_MISSION_SEARCH_TOTAL_BYTES = 100_000_000
MAX_MISSION_CATALOG_ITEMS = 512
MAX_MISSION_CATALOG_BYTES = 65_536
MAX_READ_REVISION_BYTES = 128 * 1024 * 1024
BRAIN_GUIDANCE_PATHS = frozenset({
    'Brain/AGENTS.md',
    'Brain/workflows/review-candidates.md',
    'Brain/workflows/promote-memory.md',
})
GUIDANCE_INDEX = 'Guidance/index.json'
STATUSES = {'planned', 'in_progress', 'blocked', 'resource_constrained', 'completed', 'cancelled'}
SECTIONS = ('Outcome', 'Completion criteria', 'Scope and authority', 'Verified progress',
            'Blockers and decisions', 'Artifacts and evidence', 'Next executable action', 'Work budget')


def _hash(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _text(value, label='content', limit=MAX_BYTES):
    if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > limit or '\x00' in value:
        raise ValidationError(f'{label} must be nonempty UTF-8 text of at most {limit} bytes without NUL')
    return value


def _component(value):
    try:
        value = portable_component(value)
    except (ValueError, TypeError) as exc:
        raise ValidationError(str(exc)) from exc
    return value


class Store:
    def __init__(self, data_root: Path, timezone='UTC', *, completion_validation=False, release_validation=False,
                 thread_projects=None, thread_principals=None, completion_missions=()):
        self.root = Path(os.path.abspath(data_root))
        self.completion_validation = completion_validation is True
        if not isinstance(completion_missions, (list, tuple, set, frozenset)):
            raise ValidationError('Completion Mission allowlist must be a finite collection')
        self.completion_missions = frozenset(_component(name) for name in completion_missions)
        self.release_validation = release_validation is True
        self.agent_threads_enabled = thread_projects is not None and thread_principals is not None
        self.thread_projects = {key: frozenset(value) for key, value in (thread_projects or {}).items()}
        self.thread_principals = {key: frozenset(value) for key, value in (thread_principals or {}).items()}
        try:
            self.timezone = ZoneInfo(timezone)
        except (ValueError, KeyError) as exc:
            raise ValidationError('Unknown timezone') from exc
        for path in (self.root, *self.root.parents):
            self._check_node(path)
            if (path / '.git').exists() or (path / '.git').is_symlink() or (path / 'src' / 'aios').is_dir():
                raise ValidationError('Runtime state must be outside source checkouts')
        self.root.mkdir(parents=True, exist_ok=True)
        self._path('Database').mkdir(exist_ok=True)
        with self._connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS operations (request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, intent TEXT NOT NULL, receipt TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0)')
            from .index_retry import initialize as initialize_index_retry
            initialize_index_retry(db)
            from .completion import initialize
            initialize(db)
            if self.release_validation:
                from .release import initialize as initialize_release
                initialize_release(db)
            if self.agent_threads_enabled:
                from .agent_threads import initialize as initialize_threads
                initialize_threads(db)

    @staticmethod
    def _check_node(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValidationError('Symlinks and reparse points are forbidden in state paths')
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValidationError('State paths must be ordinary files or directories')

    def _path(self, relative):
        try:
            relative = safe_relative(relative.as_posix() if isinstance(relative, Path) else relative)
        except (ValueError, TypeError) as exc:
            raise ValidationError(str(exc)) from exc
        current = self.root
        for node in (*reversed(self.root.parents), self.root):
            self._check_node(node)
        for part in Path(relative).parts:
            current /= part
            self._check_node(current)
        return current

    @staticmethod
    def _read_relative(relative):
        # Existing legacy names retain their spelling. Portable creation and
        # journal/recovery paths continue to use the stricter _path validator.
        raw = relative.as_posix() if isinstance(relative, Path) else relative
        if not isinstance(raw, str) or not raw or "\\" in raw or ":" in raw:
            raise ValidationError('Invalid read path')
        if any(unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} for char in raw):
            raise ValidationError('Control characters are forbidden in read paths')
        if re.fullmatch(r'\.agents/skills/[a-z0-9-]+/SKILL\.md', raw):
            return raw
        parts = raw.split('/')
        if any(not part or part.startswith('.') or part.endswith(('.', ' ')) for part in parts):
            raise ValidationError('Read paths must contain nonhidden ordinary names')
        return raw

    def _read_path(self, relative):
        relative = self._read_relative(relative)
        current = self.root
        for node in (*reversed(self.root.parents), self.root):
            self._check_node(node)
        for part in relative.split('/'):
            current /= part
            self._check_node(current)
        return current

    def _read_bytes(self, relative, *, limit=MAX_BYTES):
        path = self._read_path(relative)
        if not path.exists():
            return None
        if not path.is_file():
            raise ValidationError('Expected an ordinary file')
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValidationError('State file exceeds size limit')
        return data

    @contextlib.contextmanager
    def _connection(self):
        path = self._path('Database/state')
        for suffix in ('-journal', '-wal', '-shm'):
            self._path('Database/state' + suffix)
        db = sqlite3.connect(path, timeout=30, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _bytes(self, relative):
        path = self._path(relative)
        if not path.exists():
            return None
        if not path.is_file():
            raise ValidationError('Expected an ordinary file')
        data = path.read_bytes()
        if len(data) > MAX_BYTES:
            raise ValidationError('State file exceeds size limit')
        return data

    def _atomic_replace(self, relative, content, before_replace=None):
        path = self._path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path(relative)
        # A guarded completion inventories the exact candidate directory.
        # Stage in its parent (same filesystem) so the guard can run after
        # fsync, immediately before replace, without seeing our own file.
        staging_dir = path.parent.parent if before_replace is not None else path.parent
        if before_replace is not None:
            if staging_dir != self.root and self.root not in staging_dir.parents:
                raise ValidationError('Guarded staging must remain in the data root')
            if staging_dir.stat().st_dev != path.parent.stat().st_dev:
                raise ValidationError('Guarded staging requires the same filesystem')
        fd, temporary = tempfile.mkstemp(prefix='.aios-', dir=staging_dir)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content.encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
            self._path(relative)
            if before_replace is not None:
                before_replace()
            os.replace(temporary, path)
            if os.name == 'posix':
                try:
                    directory = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                    if staging_dir != path.parent:
                        directory = os.open(staging_dir, os.O_RDONLY)
                        try:
                            os.fsync(directory)
                        finally:
                            os.close(directory)
                except OSError:
                    pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _recover(self, db):
        pending = db.execute('SELECT request_id,intent FROM operations WHERE complete=0 ORDER BY rowid').fetchall()
        for request_id, encoded in pending:
            intent = json.loads(encoded)
            from .completion import recovery_guard, reject, applied
            operation = json.loads(db.execute('SELECT payload FROM operations WHERE request_id=?',
                                              (request_id,)).fetchone()[0]).get('operation')
            if operation == 'release_check' and not self.release_validation:
                reject(db, request_id, 'ValidationError')
                from .release import reject as release_reject
                release_reject(db, request_id)
                continue
            if self.release_validation:
                from .release import recovery_guard as release_guard, reject as release_reject, applied as release_applied
            else:
                release_guard = lambda *_: None
                release_reject = lambda *_: None
                release_applied = lambda *_: None
            try:
                guard = release_guard(self, db, request_id, intent)
                if guard is None:
                    guard = recovery_guard(self, db, request_id, intent)
            except (ValidationError, PermissionError, ConflictError) as exc:
                reject(db, request_id, type(exc).__name__)
                release_reject(db, request_id)
                continue
            # Check the entire intent before changing any file.
            for item in intent:
                actual = _hash(self._bytes(item['path']))
                desired = _hash(item['text'].encode('utf-8'))
                if actual not in (item['before'], desired):
                    raise RecoveryError(f"Unexpected external edit during recovery: {item['path']}")
            for item in intent:
                if _hash(self._bytes(item['path'])) != _hash(item['text'].encode('utf-8')):
                    if guard is None:
                        self._atomic_replace(item['path'], item['text'])
                    else:
                        try:
                            self._atomic_replace(item['path'], item['text'], before_replace=guard)
                        except (ValidationError, PermissionError, ConflictError) as exc:
                            reject(db, request_id, type(exc).__name__)
                            release_reject(db, request_id)
                            break
            else:
                db.execute('UPDATE operations SET complete=1 WHERE request_id=?', (request_id,))
                applied(db, request_id)
                release_applied(db, request_id)

    def _operation(self, request_id, actor, payload, builder, *, with_db=False, authorize=None):
        _text(request_id, 'request_id', 200)
        _text(actor, 'actor', 200)
        identity = _json({'actor': actor, **payload})
        with self._connection() as db:
            while db.execute('SELECT count(*) FROM operations WHERE complete=0').fetchone()[0]:
                self._recover(db)
                # Recovery outcomes must survive a later invalid submission.
                db.commit()
                db.execute('BEGIN IMMEDIATE')
            if authorize is not None:
                authorize(db)
            previous = db.execute('SELECT payload,receipt,complete FROM operations WHERE request_id=?', (request_id,)).fetchone()
            if previous:
                if previous[0] != identity:
                    raise ConflictError('Request ID was already used for a different payload or actor')
                if previous[2] == -1:
                    raise ConflictError('Prepared operation rejected during recovery; use new verified evidence')
                return json.loads(previous[1])
            writes, result = builder(db) if with_db else builder()
            intent = []
            for path, text in writes:
                _text(text)
                intent.append({'path': path, 'before': _hash(self._bytes(path)), 'text': text})
            receipt = {'request_id': request_id, 'result': result}
            db.execute('INSERT INTO operations(request_id,payload,intent,receipt) VALUES(?,?,?,?)',
                       (request_id, identity, _json(intent), _json(receipt)))
            from .index_retry import enqueue_intents
            enqueue_intents(db, intent)
            # This durable commit MUST precede every filesystem mutation.
            db.commit()
            db.execute('BEGIN IMMEDIATE')
            self._recover(db)
            if db.execute('SELECT complete FROM operations WHERE request_id=?', (request_id,)).fetchone()[0] == -1:
                db.commit()
                raise ConflictError('Prepared operation rejected at finalization')
            return receipt

    def _guidance_paths(self):
        # The owner-maintained index grants exact read paths and is loaded on
        # every read. It never changes the stricter write-path validator.
        data = self._read_bytes(GUIDANCE_INDEX)
        if data is None:
            return frozenset()

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValidationError('Duplicate guidance registry key')
                result[key] = value
            return result

        try:
            index = json.loads(data.decode('utf-8'), object_pairs_hook=unique_object)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError('Invalid guidance registry') from exc
        if (not isinstance(index, dict) or type(index.get('version')) is not int or
                index['version'] != 1 or not isinstance(index.get('documents'), list) or
                len(index['documents']) > 100):
            raise ValidationError('Invalid guidance registry')
        paths = set()
        for item in index['documents']:
            if (not isinstance(item, dict) or not isinstance(item.get('path'), str) or
                    not isinstance(item.get('when'), str) or not item['when'].strip()):
                raise ValidationError('Invalid guidance registry document')
            path = self._read_relative(item['path'])
            if not path.endswith('.md') or not (
                path in {'AGENTS.md', 'DESIGN-vercel.md'} or
                path in BRAIN_GUIDANCE_PATHS or
                path.startswith(('Policies/', 'UIUX/')) or
                re.fullmatch(r'\.agents/skills/[a-z0-9-]+/SKILL\.md', path)
            ):
                raise ValidationError('Guidance registry path outside bounded guidance roots')
            if path in paths:
                raise ValidationError('Duplicate guidance registry path')
            paths.add(path)
        return frozenset(paths)

    def read(self, relative):
        path = self._read_relative(relative)
        guidance = self._guidance_paths()
        if not (path.endswith('.md') or path == GUIDANCE_INDEX or (path.startswith('Runs/') and path.endswith('.json'))):
            raise ValidationError('Read requires Markdown or run metadata JSON')
        if path not in guidance and path != GUIDANCE_INDEX and not path.startswith(('Brain/brain/', 'Brain/working/', 'Brain/candidates/', 'Brain/inbox/', 'Brain/sources/', 'Memory/', 'Missions/', 'Runs/')):
            raise ValidationError('Read path must be in an allowed knowledge store or exact guidance document')
        with self._connection() as db:
            self._recover(db)
            data = self._read_bytes(path)
            if data is None:
                raise NotFoundError(path)
            result = {'path': path, 'revision': _hash(data), 'text': data.decode('utf-8')}
            if path.startswith('Brain/brain/'):
                result['authority_status'] = 'canonical'
            elif path.startswith(('Brain/working/', 'Brain/candidates/', 'Brain/inbox/', 'Brain/sources/')):
                result['authority_status'] = 'pending_or_source_not_canonical'
            return result

    def read_revision(self, relative):
        """Stream an authorized Markdown file's SHA without returning its body.

        This makes oversized Mission logs comparable with an existing read
        snapshot while retaining the ordinary full-read limit.
        """
        path = self._read_relative(relative)
        if not path.endswith('.md') or not path.startswith('Missions/'):
            raise ValidationError('Revision read requires a Mission Markdown path')
        with self._connection() as db:
            self._recover(db)
            body = self._read_path(path)
            if not body.exists():
                raise NotFoundError(path)
            if not body.is_file():
                raise ValidationError('Expected an ordinary file')
            digest = hashlib.sha256()
            size = 0
            with body.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    size += len(block)
                    if size > MAX_READ_REVISION_BYTES:
                        raise ValidationError('Revision read exceeds bounded limit')
                    digest.update(block)
            return {'path': path, 'revision': digest.hexdigest(), 'bytes': size,
                    'content_returned': False}

    def catalog(self, store):
        """List bounded paths already readable with the caller's read scope."""
        if store == 'brain-review':
            with self._connection() as db:
                self._recover(db)
                paths = []
                encoded_bytes = 0
                for folder, pending in (('Brain/working/candidates', True), ('Brain/inbox', False)):
                    base = self._read_path(folder)
                    if not base.is_dir():
                        raise ValidationError('Brain review catalog root unavailable')
                    try:
                        entries = sorted(base.iterdir(), key=lambda path: path.name)
                    except OSError as exc:
                        raise ValidationError('Incomplete Brain review catalog') from exc
                    for node in entries:
                        self._check_node(node)
                        if node.name.startswith('.') or node.name == 'README.md' or (pending and node.name.startswith('REVIEW-')):
                            continue
                        if node.is_dir():
                            # Historical promoted candidates are not pending work.
                            if pending and node.name.startswith('promoted-'):
                                continue
                            raise ValidationError('Nested Brain review items require explicit review')
                        if not node.is_file() or node.suffix != '.md':
                            continue
                        relative = node.relative_to(self.root).as_posix()
                        body = self._read_path(relative)
                        try:
                            if body.stat().st_size > MAX_BYTES:
                                raise ValidationError('Brain review item exceeds read limit')
                            with body.open('rb') as stream:
                                stream.read(1)
                        except OSError as exc:
                            raise ValidationError('Incomplete Brain review catalog') from exc
                        paths.append(relative)
                        encoded_bytes += len(relative.encode('utf-8')) + 3
                        if len(paths) > MAX_MISSION_CATALOG_ITEMS or encoded_bytes > MAX_MISSION_CATALOG_BYTES:
                            raise ValidationError('Brain review catalog exceeds response limit')
                return {'store': store, 'paths': paths, 'authority_status': 'pending_or_source_not_canonical'}
        if store != 'missions':
            raise ValidationError('Unknown catalog store')
        with self._connection() as db:
            self._recover(db)
            base = self._read_path('Missions')
            if not base.is_dir():
                raise ValidationError('Mission catalog root unavailable')
            try:
                entries = sorted(base.iterdir(), key=lambda path: path.name)
            except OSError as exc:
                raise ValidationError('Incomplete Mission catalog') from exc
            paths = []
            encoded_bytes = 0
            for node in entries:
                self._check_node(node)
                if node.name.startswith('.'):
                    continue
                self._read_path(node.relative_to(self.root))
                if not node.is_dir():
                    continue
                relative = (node / 'mission.md').relative_to(self.root).as_posix()
                body = self._read_path(relative)
                if not body.exists():
                    continue
                if not body.is_file():
                    raise ValidationError('Mission body is not an ordinary file')
                try:
                    if body.stat().st_size > MAX_BYTES:
                        raise ValidationError('Mission body exceeds read limit')
                    with body.open('rb') as stream:
                        stream.read(1)
                except OSError as exc:
                    raise ValidationError('Incomplete Mission catalog') from exc
                paths.append(relative)
                encoded_bytes += len(relative.encode('utf-8')) + 3
                if len(paths) > MAX_MISSION_CATALOG_ITEMS or encoded_bytes > MAX_MISSION_CATALOG_BYTES:
                    raise ValidationError('Mission catalog exceeds response limit')
            return {'store': 'missions', 'paths': paths}

    def search(self, store, query):
        roots = {'brain': 'Brain/brain', 'memory': 'Memory', 'missions': 'Missions'}
        if store not in roots:
            raise ValidationError('Unknown store')
        _text(query, 'query', 1000)
        with self._connection() as db:
            self._recover(db)
            results = []
            mission_bytes = 0
            base = self._read_path(roots[store])
            if not base.is_dir():
                raise ValidationError('Search root unavailable')
            def failed_walk(error):
                raise ValidationError('Incomplete search') from error
            for directory, dirs, files in os.walk(base, followlinks=False, onerror=failed_walk):
                for name in dirs + files:
                    node = Path(directory) / name
                    # Hidden ordinary metadata is ignored, but links and
                    # special nodes cannot hide behind a dot-prefixed name.
                    self._check_node(node)
                    if not name.startswith('.'):
                        self._read_path(node.relative_to(self.root))
                dirs[:] = [name for name in dirs if not name.startswith('.')]
                for name in sorted(files):
                    if name.startswith('.') or not name.endswith('.md'):
                        continue
                    relative = (Path(directory) / name).relative_to(self.root).as_posix()
                    data = self._read_bytes(relative, limit=MAX_MISSION_SEARCH_BYTES if store == 'missions' else MAX_BYTES)
                    if data is None:
                        raise NotFoundError(relative)
                    if store == 'missions':
                        mission_bytes += len(data)
                        if mission_bytes > MAX_MISSION_SEARCH_TOTAL_BYTES:
                            raise ValidationError('Mission search exceeds total size limit')
                    text = data.decode('utf-8')
                    index = text.casefold().find(query.casefold())
                    if index >= 0:
                        results.append({'path': relative, 'revision': _hash(data), 'excerpt': text[max(0,index-80):index+240]})
            return sorted(results, key=lambda x: x['path'])

    def _write(self, kind, path, content, request_id, expected_revision, create, actor):
        _text(content)
        if not isinstance(create, bool) or (create and expected_revision is not None):
            raise ValidationError('Create requires create=true and no expected revision')
        if not create and (not isinstance(expected_revision, str) or not re.fullmatch('[0-9a-f]{64}', expected_revision)):
            raise ValidationError('Replacement requires a SHA-256 expected_revision')
        def build(db):
            if kind == 'mission_write':
                from .completion import mission_status, guard_ordinary
                guard_ordinary(db, path, mission_status(content))
            old = self._bytes(path)
            if (create and old is not None) or (not create and (old is None or _hash(old) != expected_revision)):
                raise ConflictError('State conflict: reread and reconcile')
            return [(path, content)], {'path': path, 'revision': _hash(content.encode('utf-8'))}
        return self._operation(request_id, actor, {'operation': kind, 'path': path, 'content': content,
                               'expected_revision': expected_revision, 'create': create}, build, with_db=True)

    def write_memory(self, identifier, content, request_id, expected_revision=None, create=False, actor='local'):
        return self._write('write_memory', f'Memory/notes/{_component(identifier)}.md', content,
                           request_id, expected_revision, create, actor)

    def mission_write(self, name, content, request_id, expected_revision=None, create=False, actor='local'):
        _text(content)
        from .completion import mission_status
        mission_status(content)
        for heading in SECTIONS:
            if not re.search(r'^## ' + re.escape(heading) + r'\r?$', content, re.M):
                raise ValidationError(f'Missing mission section: {heading}')
        return self._write('mission_write', f'Missions/{_component(name)}/mission.md', content,
                           request_id, expected_revision, create, actor)

    def completion_register(self, control, actor='local'):
        from .completion import register
        return register(self, control, actor)

    def completion_evidence(self, verification_id, reference, record, actor='local'):
        from .completion import evidence
        return evidence(self, verification_id, reference, record, actor)

    def completion_report(self, verification_id, kind, result, actor='local'):
        from .completion import report
        return report(self, verification_id, kind, result, actor)

    def completion_approve(self, verification_id, control_digest, expires_at, actor='local'):
        from .completion import approve
        return approve(self, verification_id, control_digest, expires_at, actor)

    def completion_review(self, verification_id, content, actor='local'):
        from .completion import review
        return review(self, verification_id, content, actor)

    def completion_revoke(self, verification_id, actor='local'):
        from .completion import revoke
        return revoke(self, verification_id, actor)

    def mission_complete(self, name, content, request_id, verification_id, expected_revision, actor='local'):
        from .completion import complete
        return complete(self, name, content, request_id, verification_id, expected_revision, actor)

    def release_register(self, control, actor='local'):
        from .release import register
        return register(self, control, actor)

    def release_evidence(self, verification_id, reference, record, actor='local'):
        from .release import evidence
        return evidence(self, verification_id, reference, record, actor)

    def release_report(self, verification_id, kind, result, actor='local'):
        from .release import report
        return report(self, verification_id, kind, result, actor)

    def release_approve(self, verification_id, control_digest, expires_at, actor='local'):
        from .release import approve
        return approve(self, verification_id, control_digest, expires_at, actor)

    def release_revoke(self, verification_id, actor='local'):
        from .release import revoke
        return revoke(self, verification_id, actor)

    def release_check(self, verification_id, request_id, expected_mission_revision, actor='local'):
        from .release import check
        return check(self, verification_id, request_id, expected_mission_revision, actor)

    def completion_check_legacy(self, db, name, status):
        from .completion import enrolled
        if status == 'completed' and enrolled(db, name):
            raise ValidationError('Central enrollment requires the authenticated completion consumer')

    def mission_log(self, name, entry, request_id, actor='local', day=None):
        name = _component(name)
        _text(entry, 'entry')
        if day is not None:
            try:
                if dt.date.fromisoformat(day).isoformat() != day:
                    raise ValueError()
            except (ValueError, TypeError) as exc:
                raise ValidationError('day must be YYYY-MM-DD') from exc
        def build():
            if self._bytes(f'Missions/{name}/mission.md') is None:
                raise NotFoundError(name)
            date = day or dt.datetime.now(self.timezone).date().isoformat()
            path = f'Missions/{name}/log.md'
            old = self._bytes(path)
            content = (old.decode('utf-8') if old is not None else f'# {name} log\n') + f'\n## {date}\n\n{entry}\n'
            return [(path, content)], {'path': path, 'revision': _hash(content.encode('utf-8'))}
        return self._operation(request_id, actor, {'operation':'mission_log','name':name,'entry':entry,'day':day}, build)

    def publish(self, routine, run_id, report, summary, status, request_id, items=None, started_at=None, actor='local'):
        routine = _component(routine)
        run_id = _component(run_id)
        _text(report, 'report')
        _text(summary, 'summary', 4000)
        if '\n' in summary or '\r' in summary or '<!--' in summary:
            raise ValidationError('Summary must be one line without HTML comments')
        if status not in {'completed', 'partial', 'failed'}:
            raise ValidationError('Invalid report status')
        items = [] if items is None else items
        if not isinstance(items, list) or len(items) > 10000:
            raise ValidationError('items must be a list of at most 10000 strings')
        for item in items:
            _text(item, 'item', 2000)
        items = sorted(set(items))
        if started_at is not None:
            try:
                instant = dt.datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                if instant.utcoffset() is None:
                    raise ValueError()
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValidationError('started_at requires ISO-8601 with timezone') from exc
        payload = {'routine':routine,'run_id':run_id,'report_sha256':_hash(report.encode('utf-8')),
                   'summary':summary,'status':status,'items':items}
        fingerprint = _hash(_json(payload).encode('utf-8'))
        def build():
            meta_path = f'Runs/{routine}/{run_id}.json'
            previous = self._bytes(meta_path)
            if previous is not None:
                metadata = json.loads(previous)
                if metadata.get('payload_sha256') != fingerprint or metadata.get('publication') != 'complete':
                    raise ConflictError('Run ID already exists with another payload or incomplete manifest')
                if started_at is not None and dt.datetime.fromisoformat(metadata['started_at']) != instant:
                    raise ConflictError('Run ID start time changed')
                if _hash(self._bytes(metadata['report'])) != payload['report_sha256']:
                    raise ConflictError('Published report was externally modified')
                journal = self._bytes(metadata['journal'])
                if journal is None or journal.decode('utf-8').count(metadata['journal_entry']) != 1:
                    raise ConflictError('Published journal entry was externally modified')
                return [], metadata
            timestamp = started_at or dt.datetime.now(dt.timezone.utc).isoformat()
            date = dt.datetime.fromisoformat(timestamp.replace('Z','+00:00')).astimezone(self.timezone).date().isoformat()
            report_path = f'Memory/reports/{routine}/{run_id}.md'
            journal_path = f'Memory/journal/{date}.md'
            old_report = self._bytes(report_path)
            if old_report is not None and old_report != report.encode('utf-8'):
                raise ConflictError('Report path already has different content')
            old_journal = self._bytes(journal_path)
            journal = old_journal.decode('utf-8') if old_journal is not None else f'---\ntitle: Journal {date}\ntype: note\npermalink: journal/{date}\n---\n\n# Journal {date}\n'
            marker = f'<!-- aios-run:{routine}/{run_id} -->'
            if marker in journal:
                raise ConflictError('Journal already contains this run marker')
            entry = f'{marker}\n- [{status}] {summary} [[reports/{routine}/{run_id}]]\n'
            journal += '\n' + entry
            metadata = {**payload,'payload_sha256':fingerprint,'started_at':timestamp,
                        'journal_date':date,'report':report_path,'journal':journal_path,'journal_entry':entry,
                        'publication':'complete','usage':None,'path':report_path,'revision':payload['report_sha256']}
            return [(report_path,report),(journal_path,journal),(meta_path,_json(metadata)+'\n')], metadata
        return self._operation(request_id, actor, {'operation':'publish', **payload, 'started_at':started_at}, build)

    def history(self, routine):
        routine = _component(routine)
        with self._connection() as db:
            self._recover(db)
            runs = []
            for path in sorted(self._path(f'Runs/{routine}').glob('*.json')):
                value = json.loads(self._bytes(path.relative_to(self.root)))
                if value.get('publication') == 'complete':
                    runs.append(value)
            return {'routine':routine,'runs':runs,'items':sorted({item for run in runs for item in run['items']})}

    def health(self):
        with self._connection() as db:
            self._recover(db)
            check = db.execute('PRAGMA quick_check').fetchone()[0]
            return {'ok':check == 'ok','database':check,'data_root':str(self.root),'timezone':str(self.timezone),
                    'pending_operations':db.execute('SELECT count(*) FROM operations WHERE complete=0').fetchone()[0],
                    'index_retry_pending':db.execute('SELECT count(*) FROM index_retry_jobs').fetchone()[0]}
