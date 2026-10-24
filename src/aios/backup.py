"""Bounded encrypted local snapshots; optional ``cryptography`` dependency.

AES-256-GCM protects a versioned envelope with a fresh 96-bit random nonce.
The caller supplies a 32-byte key; keys/configuration/credentials are never
included. Snapshots contain the instance marker, Markdown domain files, ordinary
Mission attachments, registered Guidance, run manifests and the SQLite receipt journal. Top-level Logs, caches,
the external Basic Memory database and host coordination bindings are excluded.
The external database may contain note content; verify its source-file parity
before treating this snapshot as a complete knowledge recovery. Schema 2 retains
current registered completion inputs and safe executable identity; schema 1 remains
readable. Input namespaces are resolved from the same journal snapshot, never from
restore caller paths. The unencrypted JSON archive is limited to 1 GiB
and held in memory (peak memory is several times that limit). SQLite is copied
through its backup API under the Store writer lock, after pending recovery.
Large archives require at least 4 GiB of available host memory, increasing
with the planned size, before the first large allocation. Missing memory
telemetry fails closed.
Manual concurrent edits and hostile path swaps are outside the trust model.

Restore authenticates and validates all members before creating a staging
sibling, then renames it into a previously absent target. It preserves identity
and does not configure, start or promote a server. Staging contains plaintext;
normal failures clean it up, but process termination can leave a staging folder
for deliberate local cleanup. Backup output uses exclusive creation; synchronous
failures remove it, but abrupt termination can leave an incomplete encrypted file
that restore rejects. Directory fsync is best effort; power-loss atomicity across
all restored files is not promised.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import uuid
import unicodedata
from zoneinfo import ZoneInfo

from .identifiers import safe_relative
from .store import BRAIN_GUIDANCE_PATHS, Store, ValidationError

MAGIC = b'AIOS-BACKUP\x00\x01'
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
LARGE_ARCHIVE_BYTES = 64 * 1024 * 1024
MIN_LARGE_ARCHIVE_AVAILABLE_BYTES = 4 * 1024 * 1024 * 1024
MAX_FILES = 100_000
MARKER = '.aios-instance.json'
DATABASE = 'Database/state'
GUIDANCE_INDEX = 'Guidance/index.json'
HOST_MARKERS = {'Runs/.coordination.json', 'Runs/.state.lock'}


def _crypto(key):
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValidationError('Backup key must contain exactly 32 bytes')
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise ValidationError('Encrypted backup requires the aios-core[backup] extra') from exc
    return AESGCM(key)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError('Duplicate archive JSON key')
        result[key] = value
    return result


def _decode_json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as exc:
        raise ValidationError('Invalid backup JSON') from exc


def _external(path):
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        Store._check_node(node)
        if (node / '.git').exists() or (node / '.git').is_symlink() or (node / 'src' / 'aios').is_dir():
            raise ValidationError('Backup and restore paths must be outside source checkouts')
    return path


def _input_name(name):
    if (not isinstance(name, str) or not name or name.startswith('/')
            or '\\' in name or ':' in name
            or any(part in ('', '.', '..') for part in name.split('/'))
            or any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} for c in name)
            or unicodedata.normalize('NFC', name) != name):
        raise ValidationError('Unsafe completion snapshot path')
    if name in HOST_MARKERS or name.startswith(('Database/', 'Logs/')) or name == MARKER:
        raise ValidationError('Completion inputs overlap host-only snapshot state')
    return name


def _selection(db):
    """Resolve optional input namespaces from the same retained authority snapshot."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='completion_controls'").fetchone():
        return [], set()
    roots, bound = [], set()
    try:
        for (encoded,) in db.execute('SELECT control FROM completion_controls'):
            control = _decode_json(encoded)
            root = _input_name(control['candidate_root'])
            safe_relative(root)
            exclusions = control['candidate_exclusions']
            if not isinstance(exclusions, list) or not isinstance(control['bound_files'], dict):
                raise ValueError()
            roots.append((root, {_input_name(p) for p in exclusions}))
            bound.update(_input_name(p) for p in control['bound_files'].values())
            if len(roots) + len(bound) > MAX_FILES:
                raise ValidationError('Too many completion snapshot inputs')
    except (TypeError, KeyError, ValueError) as exc:
        raise ValidationError('Invalid retained completion snapshot selection') from exc
    return roots, bound


def _selected(name, selection):
    roots, bound = selection
    if name in bound:
        return True
    for root, excluded in roots:
        if name.startswith(root + '/'):
            relative = name[len(root) + 1:]
            if not any(relative == p or relative.startswith(p + '/') for p in excluded):
                return True
    return False


def _input_path(store, name):
    _input_name(name)
    path = store.root
    for part in name.split('/'):
        path /= part
        Store._check_node(path)
    return path


def _guidance_members(data):
    if data is None:
        return set()
    index = _decode_json(data)
    if (not isinstance(index, dict) or type(index.get('version')) is not int
            or index['version'] != 1 or not isinstance(index.get('documents'), list)
            or len(index['documents']) > 100):
        raise ValidationError('Invalid guidance registry in backup')
    paths = set()
    for item in index['documents']:
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or not isinstance(item.get('when'), str) or not item['when'].strip()):
            raise ValidationError('Invalid guidance registry document in backup')
        path = item['path']
        if (not path.endswith('.md') or not (
                path in {'AGENTS.md', 'DESIGN-vercel.md'}
                or path in BRAIN_GUIDANCE_PATHS
                or path.startswith(('Policies/', 'UIUX/'))
                or re.fullmatch(r'\.agents/skills/[a-z0-9-]+/SKILL\.md', path))):
            raise ValidationError('Guidance registry path outside bounded roots')
        try:
            Store._read_relative(path)
        except (ValidationError, ValueError, TypeError) as exc:
            raise ValidationError('Unsafe guidance registry path') from exc
        if path in paths:
            raise ValidationError('Duplicate guidance registry path')
        paths.add(path)
    return paths


def _member(name, selection=None, guidance=None):
    if name in (MARKER, DATABASE):
        return name
    if guidance is not None and (name == GUIDANCE_INDEX or name in guidance):
        return name
    if selection is not None and _selected(name, selection):
        return _input_name(name)
    if isinstance(name, str) and name.startswith('Missions/') and not name.endswith('.md'):
        # Mission evidence may be binary. Keep its path portable while allowing
        # extensions other than the Markdown/JSON set used by safe_relative.
        parent, _, filename = name.rpartition('/')
        match = re.fullmatch(r'([a-z0-9][a-z0-9_-]{0,99})\.([a-z0-9]{1,12})', filename)
        if match:
            try:
                safe_relative(parent)
                safe_relative(parent + '/' + match.group(1) + '.md')
                return name
            except (ValueError, TypeError):
                pass
        raise ValidationError('Unsafe Mission attachment path')
    if not isinstance(name, str) or not (
        name.startswith(('Brain/', 'Memory/', 'Missions/', 'Runs/')) and name.endswith('.md')
        or name.startswith('Runs/') and name.endswith('.json')
    ):
        raise ValidationError('Backup member is outside the permitted domain files')
    try:
        Store._read_relative(name)
    except (ValidationError, ValueError, TypeError) as exc:
        raise ValidationError('Unsafe backup member path') from exc
    # Historical knowledge files retain spaces and Unicode spelling. Restrict
    # only names that cannot be restored as ordinary files on Windows.
    reserved = {'con', 'prn', 'aux', 'nul'} | {
        f'{prefix}{number}' for prefix in ('com', 'lpt') for number in range(1, 10)
    }
    if (unicodedata.normalize('NFC', name) != name or re.search(r'[<>"|?*]', name)
            or any(part.casefold().split('.', 1)[0] in reserved for part in name.split('/'))):
        raise ValidationError('Nonportable backup member path')
    return name


def _marker(data):
    document = _decode_json(data)
    try:
        if set(document) != {'schema_version', 'instance_id'} or document['schema_version'] != 1:
            raise ValueError()
        instance = str(uuid.UUID(document['instance_id']))
        if instance != document['instance_id']:
            raise ValueError()
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        raise ValidationError('Invalid or missing instance marker') from exc
    return instance


def _read_bounded(path, remaining):
    if remaining < 0 or path.stat().st_size > remaining:
        raise ValidationError('Backup exceeds the 1 GiB archive limit')
    with path.open('rb') as handle:
        data = handle.read(remaining + 1)
    if len(data) > remaining:
        raise ValidationError('Backup exceeds the 1 GiB archive limit')
    return data


def _admit_large_archive(expected_bytes):
    """Hold large in-memory snapshots when host memory cannot be established."""
    if expected_bytes <= LARGE_ARCHIVE_BYTES:
        return
    try:
        import psutil
        available = psutil.virtual_memory().available
    except Exception as exc:
        raise ValidationError('Large backup memory telemetry unavailable') from exc
    required = max(MIN_LARGE_ARCHIVE_AVAILABLE_BYTES, expected_bytes * 8)
    if type(available) is not int or available < required:
        raise ValidationError('Insufficient available memory for large backup')


def _fsync_directory(path):
    if os.name == 'posix':
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


def backup(store: Store, destination: Path, key: bytes) -> dict:
    cipher = _crypto(key)
    destination = _external(destination)
    if destination == store.root or store.root in destination.parents:
        raise ValidationError('Backup destination must be outside the data root')
    if destination.exists():
        raise ValidationError('Backup destination already exists')
    if not destination.parent.is_dir():
        raise ValidationError('Backup destination parent must exist')
    files = {}
    executable = {}
    directories = set()
    total = 0
    with store._connection() as lock:
        # Commit recovered receipt state before the separate snapshot reader.
        while True:
            store._recover(lock)
            lock.commit()
            lock.execute('BEGIN IMMEDIATE')
            if lock.execute('SELECT count(*) FROM operations WHERE complete=0').fetchone()[0] == 0:
                break
        marker_path = store.root / MARKER
        Store._check_node(marker_path)
        if not marker_path.is_file():
            raise ValidationError('Backup requires an initialized instance marker')
        marker = _read_bounded(marker_path, MAX_ARCHIVE_BYTES)
        instance_id = _marker(marker)
        files[MARKER] = marker
        executable[MARKER] = False
        total += len(marker)
        guidance_path = store.root / GUIDANCE_INDEX
        Store._check_node(guidance_path)
        if guidance_path.exists():
            guidance = store._guidance_paths()
            _admit_large_archive(total + guidance_path.stat().st_size)
            index_data = _read_bounded(guidance_path, MAX_ARCHIVE_BYTES - total)
            if guidance != _guidance_members(index_data):
                raise ValidationError('Guidance registry changed during backup')
            for relative in (GUIDANCE_INDEX, *sorted(guidance)):
                path = store._read_path(relative)
                if not path.is_file():
                    raise ValidationError('Registered guidance document is missing')
                _admit_large_archive(total + path.stat().st_size)
                data = index_data if relative == GUIDANCE_INDEX else _read_bounded(path, MAX_ARCHIVE_BYTES - total)
                files[relative] = data
                executable[relative] = False
                total += len(data)
                if len(files) > MAX_FILES:
                    raise ValidationError('Backup has too many files')
        for domain in ('Brain', 'Memory', 'Missions', 'Runs'):
            base = store._path(domain)
            if not base.exists():
                continue
            if not base.is_dir():
                raise ValidationError('Domain snapshot root is not a directory')
            def walk_error(error):
                raise ValidationError('Incomplete domain snapshot inventory') from error
            for directory, dirs, names in os.walk(base, followlinks=False, onerror=walk_error):
                for name in dirs:
                    relative = (Path(directory) / name).relative_to(store.root).as_posix()
                    store._read_path(relative)
                for name in names:
                    relative = (Path(directory) / name).relative_to(store.root).as_posix()
                    if domain == 'Missions' and not name.endswith('.md'):
                        _member(relative)
                        Store._check_node(Path(directory) / name)
                    elif relative in HOST_MARKERS:
                        Store._check_node(Path(directory) / name)
                    else:
                        store._read_path(relative)
                for name in sorted(names):
                    path = Path(directory) / name
                    relative = path.relative_to(store.root).as_posix()
                    if relative in HOST_MARKERS:
                        continue
                    # Every ordinary Mission file is retained, including evidence.
                    if not (name.endswith('.md') or domain == 'Missions'
                            or domain == 'Runs' and name.endswith('.json')):
                        continue
                    _member(relative)
                    _admit_large_archive(total + path.stat().st_size)
                    data = _read_bounded(path, MAX_ARCHIVE_BYTES - total)
                    files[relative] = data
                    executable[relative] = bool(path.stat().st_mode & 0o111)
                    total += len(data)
                    if len(files) > MAX_FILES:
                        raise ValidationError('Backup has too many files')
        db_path = store._path(DATABASE)
        if db_path.stat().st_size > MAX_ARCHIVE_BYTES - total:
            raise ValidationError('Backup exceeds the 1 GiB archive limit')
        _admit_large_archive(db_path.stat().st_size + total)
        with contextlib.closing(sqlite3.connect(db_path)) as reader:
            with contextlib.closing(sqlite3.connect(':memory:')) as snapshot:
                reader.backup(snapshot)
                if snapshot.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValidationError('SQLite snapshot failed integrity check')
                files[DATABASE] = snapshot.serialize()
                executable[DATABASE] = False
                selection = _selection(snapshot)
        # Preserve the actual current inventory, including unexpected files, so
        # restore cannot turn a stale/invalid candidate into an eligible one.
        inputs = set(selection[1])
        for relative, excluded in selection[0]:
            base = _input_path(store, relative)
            if not base.exists():
                continue  # Historical/missing input remains missing, never rebuilt.
            if not base.is_dir():
                raise ValidationError('Completion candidate root is not a directory')
            directories.add(relative)
            def walk_error(error):
                raise ValidationError('Incomplete completion snapshot inventory') from error
            for directory, dirs, names in os.walk(base, followlinks=False, onerror=walk_error):
                for name in list(dirs):
                    path = Path(directory) / name
                    _input_path(store, path.relative_to(store.root).as_posix())
                    if path.relative_to(base).as_posix() in excluded:
                        dirs.remove(name)
                for name in names:
                    path = Path(directory) / name
                    selected = path.relative_to(store.root).as_posix()
                    _input_path(store, selected)
                    if _selected(selected, selection):
                        inputs.add(selected)
                    if len(inputs) + len(files) > MAX_FILES:
                        raise ValidationError('Backup has too many files')
        for relative in sorted(inputs):
            _member(relative, selection)
            path = _input_path(store, relative)
            if not path.exists():
                continue
            if not path.is_file():
                raise ValidationError('Completion input is not an ordinary file')
            if relative in files:
                continue
            _admit_large_archive(total + len(files[DATABASE]) + path.stat().st_size)
            data = _read_bounded(path, MAX_ARCHIVE_BYTES - total - len(files[DATABASE]))
            files[relative] = data
            executable[relative] = bool(path.stat().st_mode & 0o111)
            total += len(data)
            if len(files) > MAX_FILES:
                raise ValidationError('Backup has too many files')
        if total + len(files[DATABASE]) > MAX_ARCHIVE_BYTES:
            raise ValidationError('Backup exceeds the 1 GiB archive limit')
    _admit_large_archive(total + len(files[DATABASE]))
    archive = _json({'schema_version': 2, 'instance_id': instance_id, 'timezone': str(store.timezone),
                     'directories': sorted(directories),
                     'files': {name: {'sha256': _digest(data), 'bytes': len(data),
                                      'executable': executable[name],
                                      'content': base64.b64encode(data).decode('ascii')}
                               for name, data in sorted(files.items())}})
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ValidationError('Backup exceeds the 1 GiB archive limit after encoding')
    nonce = os.urandom(12)
    encrypted = MAGIC + nonce + cipher.encrypt(nonce, archive, MAGIC)
    _external(destination)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(destination.parent)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return {'path': str(destination), 'instance_id': instance_id, 'files': len(files),
            'bytes': len(encrypted), 'sha256': _digest(encrypted)}


def _validate_archive(data):
    archive = _decode_json(data)
    if not isinstance(archive, dict) or type(archive.get('schema_version')) is not int or archive['schema_version'] not in (1, 2):
        raise ValidationError('Unsupported backup schema')
    version = archive['schema_version']
    if set(archive) != {'schema_version', 'instance_id', 'timezone', 'files'} | ({'directories'} if version == 2 else set()):
        raise ValidationError('Unsupported backup schema')
    try:
        ZoneInfo(archive['timezone'])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValidationError('Invalid backup timezone') from exc
    encoded_files = archive['files']
    if not isinstance(encoded_files, dict) or not 2 <= len(encoded_files) <= MAX_FILES:
        raise ValidationError('Invalid backup file manifest')
    files = {}
    total = 0
    for name, record in encoded_files.items():
        fields = {'sha256', 'bytes', 'content'} | ({'executable'} if version == 2 else set())
        if not isinstance(record, dict) or set(record) != fields or version == 2 and type(record['executable']) is not bool:
            raise ValidationError('Invalid backup file record')
        try:
            content = base64.b64decode(record['content'], validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ValidationError('Invalid backup file encoding') from exc
        if type(record['bytes']) is not int or record['bytes'] != len(content) or record['sha256'] != _digest(content):
            raise ValidationError('Backup file hash or length mismatch')
        total += len(content)
        if total > MAX_ARCHIVE_BYTES:
            raise ValidationError('Backup exceeds the 1 GiB archive limit')
        files[name] = content
    if MARKER not in files or DATABASE not in files or _marker(files[MARKER]) != archive['instance_id']:
        raise ValidationError('Backup lacks matching instance marker or receipt journal')
    # A valid archive cannot contain a file that is also another file's parent.
    for name in files:
        if any(parent.as_posix() in files for parent in Path(name).parents if str(parent) != '.'):
            raise ValidationError('Conflicting backup member paths')
    try:
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            db.deserialize(files[DATABASE])
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValidationError('Invalid backup receipt journal')
            if db.execute('SELECT count(*) FROM operations WHERE complete=0').fetchone()[0]:
                raise ValidationError('Backup contains pending operations')
            selection = _selection(db) if version == 2 else None
            guidance = _guidance_members(files.get(GUIDANCE_INDEX))
            if guidance - files.keys():
                raise ValidationError('Backup lacks registered guidance document')
            for name in files:
                _member(name, selection, guidance if GUIDANCE_INDEX in files else None)
            directories = archive.get('directories', [])
            if (not isinstance(directories, list) or len(directories) > MAX_FILES
                    or any(not isinstance(p, str) for p in directories)
                    or len(set(directories)) != len(directories)):
                raise ValidationError('Invalid snapshot directories')
            allowed_roots = {root for root, _ in selection[0]} if selection else set()
            for name in directories:
                _input_name(name)
                if name not in allowed_roots or name in files or any(parent.as_posix() in files for parent in Path(name).parents):
                    raise ValidationError('Unselected or conflicting snapshot directory')
    except sqlite3.Error as exc:
        raise ValidationError('Invalid backup receipt journal') from exc
    return archive, files


def restore(source: Path, target: Path, key: bytes) -> dict:
    cipher = _crypto(key)
    source, target = _external(source), _external(target)
    if target.exists():
        raise ValidationError('Restore requires a new, absent destination')
    if not target.parent.is_dir():
        raise ValidationError('Restore target parent must exist')
    _admit_large_archive(source.stat().st_size)
    encrypted = _read_bounded(source, MAX_ARCHIVE_BYTES + len(MAGIC) + 28)
    if len(encrypted) < len(MAGIC) + 28 or not encrypted.startswith(MAGIC):
        raise ValidationError('Unsupported encrypted backup format')
    nonce = encrypted[len(MAGIC):len(MAGIC)+12]
    try:
        from cryptography.exceptions import InvalidTag
        archive_bytes = cipher.decrypt(nonce, encrypted[len(MAGIC)+12:], MAGIC)
    except InvalidTag as exc:
        raise ValidationError('Backup authentication failed: wrong key or damaged archive') from exc
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise ValidationError('Backup exceeds the 1 GiB archive limit')
    archive, files = _validate_archive(archive_bytes)
    staging = Path(tempfile.mkdtemp(prefix='.aios-restore-', dir=target.parent))
    try:
        for name in archive.get('directories', []):
            (staging / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, data in sorted(files.items()):
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o700 if archive['schema_version'] == 2 and archive['files'][name]['executable'] else 0o600)
        for directory, _, _ in os.walk(staging, topdown=False):
            _fsync_directory(Path(directory))
        _external(target)
        if target.exists():
            raise ValidationError('Restore destination appeared during preparation')
        staging.rename(target)
        _fsync_directory(target.parent)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {'path': str(target), 'instance_id': archive['instance_id'], 'files': len(files),
            'timezone': archive['timezone'], 'restored': True}
