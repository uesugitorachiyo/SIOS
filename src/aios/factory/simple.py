"""Three-command local evidence tool; no model orchestration or sandbox claim."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid

from .contract import Blocked, digest, plain, relative, require, strict_json, utc
from .platforms.git_capture import Capture, rows, tree_identity

MAX_SOURCE = 8 * 1024 * 1024
RECEIPT_KEYS = {'kind', 'run_id', 'packet_sha256', 'reviewer_id', 'reviewer_session_id',
                'implementation_session_id', 'reviewed_utc', 'verdicts', 'evidence'}
FACETS = {'functionality', 'security', 'test_integrity', 'verification_security'}


def _sha(path):
    return digest(plain(Path(path)).read_bytes())


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii') + b'\n'


def _put(path, value):
    with Path(path).open('xb') as stream:
        stream.write(_encoded(value))
        stream.flush(); os.fsync(stream.fileno())


def _read(path):
    path = plain(Path(path))
    require(path.stat().st_size <= MAX_SOURCE, 'evidence_overflow')
    return strict_json(path.read_bytes())


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _helper():
    factory = Path(__file__).resolve().parent
    files = [factory.parent / '__init__.py'] + sorted(p for p in factory.rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py', '.json', '.md'))
    return [{'path': str(plain(p)), 'sha256': _sha(p)} for p in files]


def _source(root):
    value = rows(root, MAX_SOURCE)
    require(0 < len(value) <= 128, 'source_file_count')
    return value


def _dependency_rows(root):
    value = rows(root, 100 * 1024 * 1024)
    require(0 < len(value) <= 10000 and all(not r['path'].lower().endswith(('.pth', '.pyc'))
            for r in value), 'dependency_inventory')
    return value


def _tracked_source(root, spec):
    """Account for every checkout entry; exclude only the explicit root .git."""
    expected = spec['files']
    require(os.name != 'nt' or all(r['mode'] != '100755' for r in expected), 'windows_executable_mode_unsupported')
    names = {r['path'] for r in expected}
    require(len(names) == len(expected) == len({n.casefold() for n in names}) and
            0 < len(names) <= 128 and expected == sorted(expected, key=lambda r: r['path'].encode()) and
            all(type(r['bytes']) is int and r['bytes'] >= 0 for r in expected) and
            sum(r['bytes'] for r in expected) <= MAX_SOURCE, 'tracked_inventory')
    ancestors = {str(Path(name).parent).replace(os.sep, '/') for name in names}
    ancestors = {p for name in ancestors for p in [name, *[x.as_posix() for x in Path(name).parents]]}
    found = set(); metadata = False
    for base, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
        for name in list(dirs) + files:
            path = Path(base) / name
            rel = path.relative_to(root).as_posix()
            s = path.lstat()
            require(not stat.S_ISLNK(s.st_mode) and not getattr(s, 'st_file_attributes', 0) & 0x400, 'tracked_link')
            if rel == '.git':
                require(stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode), 'tracked_git_metadata')
                metadata = True
                if name in dirs: dirs.remove(name)
                continue
            relative(rel)
            require('.git' not in (x.casefold() for x in rel.split('/')), 'candidate_metadata')
            if stat.S_ISDIR(s.st_mode):
                require(rel in ancestors, 'untracked_checkout_entry')
            else:
                require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and rel in names, 'untracked_checkout_entry')
                found.add(rel)
    require(found == names, 'tracked_inventory')
    for row in expected:
        path = root / row['path']; s = path.stat()
        mode = '100755' if os.name != 'nt' and s.st_mode & 0o111 else '100644'
        require(row == {'path': row['path'], 'mode': mode, 'bytes': s.st_size, 'sha256': _sha(path)}, 'tracked_file_drift')
    return expected, metadata


def _runtime_binding(request):
    python = Path(sys.executable).resolve()
    require(python == Path(request['python']['path']) and
            _sha(python) == request['python']['sha256'], 'caller_python_drift')
    stdlib = Path(subprocess.__file__).resolve()
    cfg = Path(sys.prefix).resolve() / 'pyvenv.cfg'
    return {'python': {'path': str(python), 'sha256': _sha(python)},
            'subprocess': {'path': str(stdlib), 'sha256': _sha(stdlib)},
            'base_exec_prefix': str(Path(sys.base_exec_prefix).resolve()),
            'pyvenv_cfg': {'path': str(cfg), 'sha256': _sha(cfg)} if cfg.exists() or cfg.is_symlink() else None}


def _runtime_origins(prepared, request):
    require(prepared.get('runtime_binding') == _runtime_binding(request), 'runtime_binding_drift')
    expected = {r['path']: r['sha256'] for r in prepared['helper']}
    origins = {}
    for name, module in sorted(sys.modules.items()):
        if name == 'aios' or name.startswith('aios.factory') or name == 'subprocess' or name == 'psutil' or name.startswith('psutil.'):
            path = getattr(module, '__file__', None)
            require(path is not None, 'runtime_origin_unknown')
            path = str(Path(path).resolve()); checksum = _sha(path)
            if name == 'aios' or name.startswith('aios.factory'):
                require(expected.get(path) == checksum, 'mixed_helper_origin')
            origins[name] = {'path': path, 'sha256': checksum}
    require(_sha(Path(sys.executable).resolve()) == request['python']['sha256'], 'caller_python_drift')
    origins['caller_python'] = {'path': sys.executable, 'sha256': request['python']['sha256']}
    return origins


def _copy(source, target, inventory):
    target.mkdir()
    for row in inventory:
        path = target / row['path']; path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write((source / row['path']).read_bytes())
        path.chmod(0o755 if row['mode'] == '100755' else 0o644)
    require(_source(target) == inventory, 'copy_drift')


def _copy_dependencies(source, target, inventory):
    target.mkdir()
    for row in inventory:
        path = target / row['path']; path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write((source / row['path']).read_bytes())
        path.chmod(0o755 if row['mode'] == '100755' else 0o644)
    require(_dependency_rows(target) == inventory and _dependency_rows(source) == inventory, 'dependency_drift')


def _request(value):
    keys = {'kind', 'allowed_paths', 'target_modules', 'test_modules', 'required_test_ids',
            'criteria', 'implementer_id', 'implementation_session_id', 'python', 'git', 'owner_hard_stop_utc'}
    optional = {'source_root', 'dependencies', 'tracked_snapshot'}
    require(type(value) is dict and keys <= set(value) and set(value) <= keys | optional and value['kind'] == 'factory-simple-request-v1', 'request_shape')
    source_root = value.get('source_root', '.')
    require(source_root == '.' or relative(source_root) == source_root, 'source_root')
    deps = value.get('dependencies')
    if deps is not None:
        require(type(deps) is dict and set(deps) == {'root', 'files'} and type(deps['root']) is str and
                Path(deps['root']).is_absolute(), 'dependency_shape')
        require(_dependency_rows(plain(Path(deps['root']), directory=True)) == deps['files'], 'dependency_drift')
    tracked = value.get('tracked_snapshot')
    if tracked is not None:
        require(type(tracked) is dict and set(tracked) == {'git_tree', 'files'} and
                type(tracked['git_tree']) is str and re.fullmatch('[0-9a-f]{40}', tracked['git_tree']) is not None and
                type(tracked['files']) is list and all(type(r) is dict and set(r) == {'path', 'mode', 'bytes', 'sha256'}
                and type(r['path']) is str and relative(r['path']) == r['path'] for r in tracked['files']), 'tracked_snapshot_shape')
    for name in ('allowed_paths', 'target_modules', 'test_modules', 'required_test_ids', 'criteria'):
        seq = value[name]
        require(type(seq) is list and 0 < len(seq) <= 128 and
                all(type(s) is str and 0 < len(s) <= 4096 for s in seq) and len(set(seq)) == len(seq), 'request_list')
    for name in value['allowed_paths']: relative(name)
    for name in value['target_modules'] + value['test_modules']:
        require(re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', name, re.ASCII) is not None, 'module_name')
    require(not set(value['target_modules']) & set(value['test_modules']), 'module_overlap')
    for key in ('implementer_id', 'implementation_session_id'):
        require(type(value[key]) is str and value[key].strip() == value[key] and
                0 < len(value[key]) <= 256 and value[key].lower() not in ('unknown', 'unverified', 'pending'), 'implementation_identity')
    for key in ('python', 'git'):
        tool = value[key]
        require(type(tool) is dict and set(tool) == {'path', 'sha256'} and Path(tool['path']).is_absolute() and
                re.fullmatch('[0-9a-f]{64}', tool['sha256']) is not None and _sha(tool['path']) == tool['sha256'], 'tool_identity')
    utc(value['owner_hard_stop_utc'])
    return value


def _bind_request(value):
    """Fill only absent hashes for explicitly selected paths; never invoke tools."""
    require(type(value) is dict, 'request_shape')
    bound = dict(value); filled = []
    for name in ('python', 'git'):
        tool = value.get(name)
        require(type(tool) is dict and set(tool) in ({'path'}, {'path', 'sha256'}) and
                type(tool.get('path')) is str and Path(tool['path']).is_absolute(), 'tool_identity')
        selected = dict(tool)
        actual = _sha(selected['path'])
        if 'sha256' not in selected:
            selected['sha256'] = actual; filled.append(name)
        else:
            require(type(selected['sha256']) is str and selected['sha256'] == actual, 'tool_identity')
        bound[name] = selected
    return _request(bound), filled


def _verify_binding(root, prepared, request):
    binding = prepared.get('tool_binding')
    if binding is None:
        return
    require(type(binding) is dict and set(binding) == {'kind', 'input_sha256', 'filled', 'tools'} and
            binding['kind'] == 'factory-simple-explicit-tools-v1', 'tool_binding_shape')
    original = root / 'request-original.json'
    require(_sha(original) == binding['input_sha256'], 'tool_binding_input_drift')
    rebound, filled = _bind_request(_read(original))
    require(rebound == request and filled == binding['filled'] and
            binding['tools'] == {name: request[name] for name in ('python', 'git')}, 'tool_binding_drift')


def _prepared(root):
    root = plain(Path(root), directory=True)
    prepared = _read(root / 'PREPARED.json')
    require(prepared['run_root'] == str(root), 'relocated_run')
    request = _request(_read(root / 'request.json'))
    require(_sha(root / 'request.json') == prepared['request_sha256'], 'request_drift')
    _verify_binding(root, prepared, request)
    require(_helper() == prepared['helper'], 'helper_drift')
    require(_source(root / 'B') == prepared['baseline'], 'baseline_drift')
    return root, prepared, request


def _validate_review(root, prepared, request, packet, receipt):
    require(type(receipt) is dict and set(receipt) == RECEIPT_KEYS and receipt['kind'] == 'factory-simple-review-v1', 'review_shape')
    require(receipt['run_id'] == prepared['run_id'] and receipt['packet_sha256'] == _sha(root / 'REVIEW_PACKET.json') and
            receipt['implementation_session_id'] == request['implementation_session_id'], 'review_binding')
    require(type(receipt['reviewer_id']) is str and bool(receipt['reviewer_id'].strip()) and
            type(receipt['reviewer_session_id']) is str and 0 < len(receipt['reviewer_session_id']) <= 256 and
            receipt['reviewer_session_id'] == receipt['reviewer_session_id'].strip() and
            receipt['reviewer_session_id'].casefold() not in
            (request['implementation_session_id'].casefold(), 'unknown', 'unverified', 'pending'), 'review_not_independent')
    utc(receipt['reviewed_utc'])
    require(utc(packet['sealed_utc']) <= utc(receipt['reviewed_utc']) <= utc(_now()), 'review_stale')
    require(type(receipt['verdicts']) is dict and set(receipt['verdicts']) == FACETS and
            all(v in ('PASS', 'FAIL', 'UNKNOWN') for v in receipt['verdicts'].values()), 'review_facets')
    evidence = receipt['evidence']
    require(type(evidence) is dict and set(evidence) == {'path', 'sha256'}, 'review_evidence')
    relative(receipt['evidence']['path'])


def _review_packet(root, prepared, request, check_value):
    require(prepared['kind'] == 'factory-simple-prepared-v1' and prepared['run_root'] == str(root) and
            prepared['request_sha256'] == _sha(root / 'request.json'), 'predecessor_request_binding')
    _verify_binding(root, prepared, request)
    require(check_value['kind'] == 'factory-simple-check-v1' and check_value['run_id'] == prepared['run_id'] and
            check_value['passed'] is True and check_value['known_ended'] is True and
            not (root / 'FINALIZATION_FAILED.json').exists() and
            not (root.parent / 'SIMPLE_UNKNOWN.json').exists(), 'review_check_binding')
    require(_source(root / 'B') == prepared['baseline'] and
            tree_identity(root / 'B') == prepared['B_tree'] == check_value['B_tree'], 'baseline_drift')
    require(_source(root / 'work') == check_value['candidate'] == _source(root / 'C') and
            tree_identity(root / 'C') == check_value['C_tree'], 'candidate_drift')
    for row in prepared['helper']:
        require(_sha(row['path']) == row['sha256'], 'helper_drift')
    packet = _read(root / 'REVIEW_PACKET.json')
    require(packet['kind'] == 'factory-simple-review-packet-v1' and packet['run_id'] == prepared['run_id'] and
            rows(root / 'review-packet', 100 * 1024 * 1024) == packet['files'], 'review_packet_drift')
    for name in ('request.json', 'PREPARED.json', 'CHECK_STARTED.json', 'CHECK.json', 'TEST_RESULT.json', 'FULL_DIFF.patch'):
        require(_sha(root / name) == _sha(root / 'review-packet' / name), 'check_evidence_drift')
    if prepared.get('tool_binding') is not None:
        require(_sha(root / 'request-original.json') == _sha(root / 'review-packet/request-original.json'), 'tool_binding_packet_drift')
    if request.get('dependencies') is not None:
        require(_dependency_rows(root / 'review-packet' / 'dependencies') == request['dependencies']['files'], 'dependency_packet_drift')
    require(check_value['test_result_sha256'] == _sha(root / 'TEST_RESULT.json'), 'check_test_binding')
    return packet


def _retained_review(root, prepared, request, packet):
    require(not any((root / n).exists() or (root / n).is_symlink()
                    for n in ('REVIEW_FAILED.json', 'REVIEW_IMPORT.pending')), 'review_import_incomplete')
    receipt = _read(root / 'REVIEW.json')
    imported = _read(root / 'REVIEW_IMPORT.json')
    started = (root / 'REVIEW_STARTED.json').exists() or (root / 'REVIEW_STARTED.json').is_symlink()
    keys = {'receipt_sha256', 'evidence_sha256', 'source_receipt_sha256'}
    require(type(imported) is dict and set(imported) == (keys | {'started_sha256'} if started else keys) and
            all(type(v) is str and re.fullmatch('[0-9a-f]{64}', v) is not None for v in imported.values()),
            'review_import_shape')
    require(_sha(root / 'REVIEW.json') == imported['receipt_sha256'] and
            _sha(root / 'REVIEW_EVIDENCE') == imported['evidence_sha256'], 'imported_review_drift')
    _validate_review(root, prepared, request, packet, receipt)
    require(imported['evidence_sha256'] == receipt['evidence']['sha256'], 'imported_review_drift')
    expected_claim = {'run_id': prepared['run_id'], 'packet_sha256': receipt['packet_sha256'],
                      'reviewer_session_id': receipt['reviewer_session_id']}
    if started:
        marker = _start_identity(root, prepared, receipt, imported['source_receipt_sha256'])
        require(_read(root / 'REVIEW_STARTED.json') == marker and
                _sha(root / 'REVIEW_STARTED.json') == imported['started_sha256'], 'review_started_binding')
        expected_claim.update(receipt_sha256=imported['receipt_sha256'], started_sha256=imported['started_sha256'])
    claim = root.parent / 'simple-review-sessions' / (digest(receipt['reviewer_session_id'].casefold().encode()) + '.json')
    require(_read(claim) == expected_claim, 'review_claim_binding')
    return receipt, imported


def _start_identity(root, prepared, receipt, source_sha256):
    return {'kind': 'factory-simple-review-started-v1', 'run_id': prepared['run_id'],
            'packet_sha256': receipt['packet_sha256'], 'receipt_sha256': digest(_encoded(receipt)),
            'evidence_sha256': receipt['evidence']['sha256'], 'source_receipt_sha256': source_sha256,
            'reviewer_session_id': receipt['reviewer_session_id'], 'check_sha256': _sha(root / 'CHECK.json')}


def _publish_review_import(root, imported):
    # Final name is never a partially written JSON record. The visible pending
    # file also keeps an interrupted link/unlink publication nonpositive.
    pending = root / 'REVIEW_IMPORT.pending'
    _put(pending, imported)
    os.link(pending, root / 'REVIEW_IMPORT.json')
    pending.unlink()


def _saved_predecessor(root):
    prepared = _read(root / 'PREPARED.json')
    require(type(prepared) is dict and prepared.get('kind') == 'factory-simple-prepared-v1',
            'predecessor_prepared_kind')
    require(type(prepared.get('run_id')) is str and 0 < len(prepared['run_id']) <= 256 and
            prepared.get('run_root') == str(root), 'predecessor_prepared_binding')
    request = _request(_read(root / 'request.json'))
    require(prepared.get('request_sha256') == _sha(root / 'request.json'), 'predecessor_request_binding')
    _verify_binding(root, prepared, request)
    check = _read(root / 'CHECK.json')
    require(type(check) is dict and check.get('kind') == 'factory-simple-check-v1', 'predecessor_check_kind')
    require(check.get('run_id') == prepared['run_id'], 'predecessor_check_binding')
    require(type(check.get('passed')) is bool and check.get('known_ended') is True,
            'restart_predecessor_not_closed_failure')
    started = _read(root / 'CHECK_STARTED.json')
    require(type(started) is dict and started.get('run_id') == prepared['run_id'], 'predecessor_started_binding')
    return prepared, request, check


def _collection_lock(root):
    parent = plain(Path(root).absolute().parent, directory=True)
    if sys.platform == 'win32':
        from .platforms._windows_native import RunLock
    else:
        require(sys.platform == 'darwin', 'unsupported_native_host')
        from .platforms._posix import RunLock
    return RunLock(plain(parent / 'simple.lock'))


def _collection_clear(root):
    for name in ('SIMPLE_ACTIVE.json', 'SIMPLE_UNKNOWN.json'):
        path = Path(root).absolute().parent / name
        require(not path.exists() and not path.is_symlink(), 'collection_incomplete_or_unknown')


def prepare(request, baseline_dir, run_dir, predecessor=None, *, bind_tools=False):
    with _collection_lock(run_dir):
        _collection_clear(Path(run_dir))
        return _prepare_locked(request, baseline_dir, run_dir, predecessor, bind_tools=bind_tools)


def _prepare_locked(request, baseline_dir, run_dir, predecessor=None, *, bind_tools=False):
    require(type(bind_tools) is bool, 'tool_binding_flag')
    source = plain(Path(request)); require(source.stat().st_size <= MAX_SOURCE, 'evidence_overflow')
    original = source.read_bytes()
    require(len(original) <= MAX_SOURCE, 'evidence_overflow')
    raw = strict_json(original)
    if bind_tools:
        value, filled = _bind_request(raw)
    else:
        value = _request(raw)
    baseline = plain(Path(baseline_dir), directory=True)
    root = plain(Path(run_dir), directory=True)
    require(not root.exists() and root.parent.is_dir() and not root.is_relative_to(baseline) and
            not baseline.is_relative_to(root), 'run_isolation')
    if value.get('dependencies') is not None:
        dep = plain(Path(value['dependencies']['root']), directory=True)
        require(not any((dep.is_relative_to(p) or p.is_relative_to(dep)) for p in (baseline, root)), 'dependency_overlap')
    require(not (root.parent / 'SIMPLE_UNKNOWN.json').exists(), 'prior_termination_unknown')
    previous = None
    if predecessor is not None:
        old = plain(Path(predecessor), directory=True)
        old_prepared, old_request, check = _saved_predecessor(old)
        failed_finalization = (old / 'FINALIZATION_FAILED.json').is_file()
        previous = {'run_id': old_prepared['run_id'], 'check_sha256': _sha(old / 'CHECK.json')}
        require(check['known_ended'] is True, 'restart_predecessor_not_closed_failure')
        if any((old / name).exists() or (old / name).is_symlink() for name in ('REVIEW_STARTED.json', 'REVIEW_FAILED.json',
               'REVIEW_IMPORT.pending', 'REVIEW.json', 'REVIEW_IMPORT.json', 'REVIEW_EVIDENCE')):
            packet = _review_packet(old, old_prepared, old_request, check)
            receipt, imported = _retained_review(old, old_prepared, old_request, packet)
            require(any(v in ('FAIL', 'UNKNOWN') for v in receipt['verdicts'].values()),
                    'restart_predecessor_review_not_negative')
            previous.update(review_import_sha256=_sha(old / 'REVIEW_IMPORT.json'),
                            review_receipt_sha256=imported['receipt_sha256'],
                            review_evidence_sha256=imported['evidence_sha256'],
                            review_packet_sha256=_sha(old / 'REVIEW_PACKET.json'))
        else:
            require(check['passed'] is False or failed_finalization, 'restart_predecessor_not_closed_failure')
            if failed_finalization: previous['finalization_failure_sha256'] = _sha(old / 'FINALIZATION_FAILED.json')
    if value.get('tracked_snapshot') is None:
        inventory, metadata = _source(baseline), False
    else:
        inventory, metadata = _tracked_source(baseline, value['tracked_snapshot'])
    runtime_binding = _runtime_binding(value)
    root.mkdir(mode=0o700)
    _copy(baseline, root / 'B', inventory); _copy(baseline, root / 'work', inventory)
    if value.get('tracked_snapshot') is not None:
        require(tree_identity(root / 'B') == value['tracked_snapshot']['git_tree'], 'tracked_tree_mismatch')
    _put(root / 'request.json', value)
    if bind_tools:
        with (root / 'request-original.json').open('xb') as stream:
            stream.write(original); stream.flush(); os.fsync(stream.fileno())
    prepared = {'kind': 'factory-simple-prepared-v1', 'run_id': uuid.uuid4().hex, 'run_root': str(root),
                'request_sha256': _sha(root / 'request.json'), 'helper': _helper(),
                'baseline': inventory, 'B_tree': tree_identity(root / 'B'), 'predecessor': previous,
                'runtime_binding': runtime_binding, 'excluded_root_git_metadata': metadata,
                'prepared_utc': _now()}
    if bind_tools:
        prepared['tool_binding'] = {'kind': 'factory-simple-explicit-tools-v1',
                                    'input_sha256': digest(original), 'filled': filled,
                                    'tools': {name: value[name] for name in ('python', 'git')}}
    _put(root / 'PREPARED.json', prepared)
    return {'state': 'prepared', 'run_id': prepared['run_id'], 'work': str(root / 'work')}


def _capture(supervisor, git_path, root):
    capture = Capture(supervisor, git_path)
    B = capture.tree(root / 'B'); C = capture.tree(root / 'C')
    require(B == tree_identity(root / 'B') and C == tree_identity(root / 'C'), 'git_tree_drift')
    diff = capture.command(['diff', '--binary', '--no-renames', '--no-ext-diff', '--no-textconv', B, C])
    require(0 < len(diff) <= MAX_SOURCE, 'empty_or_large_diff')
    (root / 'FULL_DIFF.patch').write_bytes(diff)
    return B, C


def check(run_dir):
    with _collection_lock(run_dir):
        _collection_clear(Path(run_dir))
        return _check_locked(run_dir)


def _check_locked(run_dir):
    root, prepared, request = _prepared(run_dir)
    require(not (root / 'CHECK_STARTED.json').exists() and not (root / 'CHECK_STARTED.json').is_symlink(), 'check_consumed')
    require(not (root.parent / 'SIMPLE_UNKNOWN.json').exists(), 'prior_termination_unknown')
    anchor = time.monotonic(); now = datetime.now(timezone.utc)
    remaining = (datetime.fromisoformat(request['owner_hard_stop_utc'].replace('Z', '+00:00')) - now).total_seconds()
    hard = anchor + min(130, remaining); productive = min(anchor + 120, hard - 10)
    # Immutable absolute bounds and consumption precede all children. No renewal.
    _put(root.parent / 'SIMPLE_ACTIVE.json', {'kind': 'factory-simple-active-v1',
         'run_id': prepared['run_id'], 'run_root': str(root), 'request_sha256': prepared['request_sha256']})
    _put(root / 'CHECK_STARTED.json', {'run_id': prepared['run_id'], 'started_utc': _now(),
         'started_monotonic_ns': int(anchor * 1e9), 'productive_deadline_ns': int(productive * 1e9),
         'hard_deadline_ns': int(hard * 1e9), 'productive_seconds_max': 120,
         'cleanup_seconds_max': 10, 'owner_hard_stop_utc': request['owner_hard_stop_utc']})
    supervisor = None; result = None; reason = None; candidate = None; B = C = None; runtime_origins = None
    try:
        require(sys.platform in ('darwin', 'win32') and anchor < productive, 'check_platform_or_deadline')
        candidate = _source(root / 'work')
        old = {r['path']: r for r in prepared['baseline']}; new = {r['path']: r for r in candidate}
        changed = sorted(name for name in old.keys() | new.keys() if old.get(name) != new.get(name))
        require(changed and set(changed) <= set(request['allowed_paths']), 'diff_outside_allowlist_or_empty')
        require(all(name in new and (name not in old or old[name]['mode'] == new[name]['mode']) for name in changed), 'deletion_or_mode_change')
        _copy(root / 'work', root / 'C', candidate)
        for directory in ('home', 'tmp'): (root / directory).mkdir()
        if sys.platform == 'win32':
            from .platforms._simple_windows import Supervisor
        else:
            from .platforms._posix import Supervisor
        runtime_origins = _runtime_origins(prepared, request)
        if sys.platform == 'win32':
            supervisor = Supervisor(root, anchor=anchor, productive=productive, hard=hard,
                                    owner_utc=request['owner_hard_stop_utc'])
        else:
            supervisor = Supervisor(root, seconds=productive-anchor, started=anchor, poll=.05, grace=1)
            supervisor.cleanup_policy = {'cleanup_seconds': 10, 'term_grace_seconds': 1,
                'parent_deadline': hard, 'owner_deadline': hard, 'owner_hard_stop_utc': request['owner_hard_stop_utc']}
        B, C = _capture(supervisor, request['git']['path'], root)
        require(_helper() == prepared['helper'], 'helper_drift')
        command = [request['python']['path'], '-I', '-B', '-S', str(Path(__file__).with_name('_simple_check.py')),
                   str(root / 'C'), str(root / 'request.json'), str(root / 'TEST_RESULT.json')]
        env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'HOME': str(root / 'home'),
               'TMPDIR': str(root / 'tmp'), 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'}
        record = supervisor.call(command, cwd=root / 'C', env=env, stage='simple-check', success=False)
        result = _read(root / 'TEST_RESULT.json')
        require(record['exit_code'] == 0 and result['passed'] is True and
                type(result['run']) is int and result['run'] > 0 and
                len(result['ids']) == result['run'] == len(set(result['ids'])) and
                set(request['required_test_ids']) <= set(result['ids']) and
                all(result[k] == 0 for k in ('failures', 'errors', 'skips', 'expected_failures', 'unexpected_successes', 'failed_subtests')), 'tests_nonpositive')
        require(result['python']['sha256'] == request['python']['sha256'] and
                result['python']['base_exec_prefix'] == prepared['runtime_binding']['base_exec_prefix'] and
                result['bootstrap']['sha256'] == _sha(Path(__file__).with_name('_simple_check.py')) and
                result['source_unchanged'] is True and result['source_before'] == result['source_after'] and
                result['dependency_unchanged'] is True and
                result['unexpected_origins'] == {}, 'runner_origin_drift')
        for name in request['target_modules'] + request['test_modules']:
            base = root / 'C'
            if name in request['target_modules']: base = base / request.get('source_root', '.')
            stem = base.joinpath(*name.split('.'))
            choices = [p for p in (stem.with_suffix('.py'), stem / '__init__.py') if p.is_file()]
            require(len(choices) == 1, 'test_module_origin')
            path = choices[0]
            require(result['modules'].get(name) == {'path': str(path), 'sha256': _sha(path)}, 'test_module_origin')
        require(time.monotonic() < productive, 'check_deadline')
    except (Exception, KeyboardInterrupt) as exc:
        reason = exc.code if isinstance(exc, Blocked) else type(exc).__name__
    known = supervisor is None or (not supervisor.unknown and all(r['known_ended'] for r in supervisor.records))
    try:
        require(_helper() == prepared['helper'], 'helper_drift')
        if runtime_origins is not None:
            require(prepared.get('runtime_binding') == _runtime_binding(request), 'runtime_binding_drift')
            require(_sha(Path(sys.executable).resolve()) == request['python']['sha256'], 'caller_python_drift')
            subprocess_origin = runtime_origins.get('subprocess')
            require(type(subprocess_origin) is dict and
                    str(Path(sys.modules['subprocess'].__file__).resolve()) == subprocess_origin['path'] and
                    _sha(Path(subprocess_origin['path'])) == subprocess_origin['sha256'], 'stdlib_origin_drift')
        require(_source(root / 'B') == prepared['baseline'], 'baseline_drift')
        if request.get('dependencies') is not None:
            require(_dependency_rows(Path(request['dependencies']['root'])) == request['dependencies']['files'], 'dependency_drift')
        if candidate is not None:
            require(_source(root / 'work') == candidate and _source(root / 'C') == candidate, 'checked_source_changed')
    except (Exception, KeyboardInterrupt):
        reason = reason or 'source_or_helper_changed'
    if time.monotonic() >= productive:
        reason = reason or 'check_deadline'
    value = {'kind': 'factory-simple-check-v1', 'run_id': prepared['run_id'], 'passed': reason is None and known,
             'reason': reason, 'known_ended': known, 'B_tree': B, 'C_tree': C, 'candidate': candidate,
             'ended_utc': _now(), 'process_records': supervisor.records if supervisor is not None else [],
             'runtime_origins': runtime_origins,
             'test_result_sha256': _sha(root / 'TEST_RESULT.json') if (root / 'TEST_RESULT.json').is_file() else None}
    if not known:
        _put(root.parent / 'SIMPLE_UNKNOWN.json', {'run_id': prepared['run_id'], 'reason': 'termination_unknown'})
    if value['passed']:
        try:
            packet = root / 'review-packet'; packet.mkdir()
            for name in ('request.json', 'PREPARED.json', 'CHECK_STARTED.json', 'TEST_RESULT.json', 'FULL_DIFF.patch'):
                shutil.copyfile(root / name, packet / name)
            if prepared.get('tool_binding') is not None:
                shutil.copyfile(root / 'request-original.json', packet / 'request-original.json')
            _copy(root / 'B', packet / 'B', prepared['baseline']); _copy(root / 'C', packet / 'C', candidate)
            if request.get('dependencies') is not None:
                _copy_dependencies(Path(request['dependencies']['root']), packet / 'dependencies', request['dependencies']['files'])
            # Raw streams and exact helper bytes accompany the summarized result.
            shutil.copytree(root / 'logs', packet / 'logs')
            package_root = Path(__file__).resolve().parents[1]
            for row in prepared['helper']:
                source = Path(row['path'])
                target = packet / 'helper' / 'aios' / source.relative_to(package_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            require(time.monotonic() < productive, 'packet_deadline')
            _put(root / 'CHECK.json', value)
            shutil.copyfile(root / 'CHECK.json', packet / 'CHECK.json')
            manifest = {'kind': 'factory-simple-review-packet-v1', 'run_id': prepared['run_id'], 'sealed_utc': _now(),
                        'files': rows(packet, 100 * 1024 * 1024)}
            _put(root / 'REVIEW_PACKET.json', manifest)
            # Validate the just-sealed packet against its run before returning
            # its hash. The persisted CHECK and packet remain non-circular.
            _review_packet(root, prepared, request, value)
            packet_sha256 = _sha(root / 'REVIEW_PACKET.json')
            require(time.monotonic() < productive, 'packet_deadline')
            value = {**value, 'packet_sha256': packet_sha256}
        except (Exception, KeyboardInterrupt) as exc:
            value = {**value, 'passed': False, 'reason': 'packet_finalization_failed'}
            if (root / 'CHECK.json').exists():
                _put(root / 'FINALIZATION_FAILED.json', {'reason': type(exc).__name__})
            else:
                _put(root / 'CHECK.json', value)
    else:
        _put(root / 'CHECK.json', value)
    if known and (root / 'CHECK.json').is_file():
        terminal = _read(root / 'CHECK.json')
        marker = _read(root.parent / 'SIMPLE_ACTIVE.json')
        require(terminal.get('run_id') == prepared['run_id'] and terminal.get('known_ended') is True and
                marker == {'kind': 'factory-simple-active-v1', 'run_id': prepared['run_id'],
                           'run_root': str(root), 'request_sha256': prepared['request_sha256']},
                'collection_terminal_mismatch')
        (root.parent / 'SIMPLE_ACTIVE.json').unlink()
    return value


def report(run_dir, review_receipt=None):
    with _collection_lock(run_dir):
        try:
            _collection_clear(Path(run_dir))
        except Blocked as error:
            if error.code != 'collection_incomplete_or_unknown': raise
            return {'state': 'held_unknown', 'reason': error.code,
                    'action': 'preserve the collection; no retry or parent switch until termination is independently established'}
        return _report_locked(run_dir, review_receipt)


def _report_locked(run_dir, review_receipt=None):
    root, prepared, request = _prepared(run_dir)
    check_value = _read(root / 'CHECK.json')
    require(not (root / 'FINALIZATION_FAILED.json').exists(), 'packet_finalization_failed')
    if check_value['passed'] is not True or check_value['known_ended'] is not True:
        return {'state': 'restart_required' if check_value['known_ended'] is True else 'held_unknown',
                'run_id': prepared['run_id'], 'reason': check_value['reason'],
                'action': 'fresh prepare with explicit predecessor; never reuse this check' if check_value['known_ended'] else
                          'stop; unknown termination cannot restart or report positive'}
    require(not (root.parent / 'SIMPLE_UNKNOWN.json').exists(), 'prior_termination_unknown')
    packet = _review_packet(root, prepared, request, check_value)
    artifacts = ('REVIEW_STARTED.json', 'REVIEW_FAILED.json', 'REVIEW_IMPORT.pending',
                 'REVIEW.json', 'REVIEW_IMPORT.json', 'REVIEW_EVIDENCE')
    present = lambda name: (root / name).exists() or (root / name).is_symlink()
    if review_receipt is not None:
        require(not any(present(n) for n in ('REVIEW_STARTED.json', 'REVIEW_FAILED.json', 'REVIEW_IMPORT.pending')),
                'review_import_consumed')
        require(not present('REVIEW.json'), 'review_already_imported')
        require(not any(present(n) for n in artifacts), 'review_import_consumed')
        source = plain(Path(review_receipt))
        source_raw = source.read_bytes(); require(len(source_raw) <= MAX_SOURCE, 'evidence_overflow')
        receipt = strict_json(source_raw)
        _validate_review(root, prepared, request, packet, receipt)
        evidence = receipt['evidence']; path = plain(source.parent / relative(evidence['path']))
        raw = path.read_bytes(); require(len(raw) <= MAX_SOURCE, 'review_evidence_overflow')
        require(digest(raw) == evidence['sha256'], 'review_evidence_drift')
        sessions = plain(root.parent / 'simple-review-sessions', directory=True)
        claim = sessions / (digest(receipt['reviewer_session_id'].casefold().encode()) + '.json')
        require(not claim.exists() and not claim.is_symlink(), 'review_session_consumed')
        marker = _start_identity(root, prepared, receipt, digest(source_raw))
        try:
            # Whole-run admission is exclusive and precedes claim or publication.
            _put(root / 'REVIEW_STARTED.json', marker)
            started_sha = _sha(root / 'REVIEW_STARTED.json')
            sessions.mkdir(exist_ok=True)
            _put(claim, {'run_id': prepared['run_id'], 'packet_sha256': receipt['packet_sha256'],
                         'reviewer_session_id': receipt['reviewer_session_id'],
                         'receipt_sha256': marker['receipt_sha256'], 'started_sha256': started_sha})
            with (root / 'REVIEW_EVIDENCE').open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            _put(root / 'REVIEW.json', receipt)
            _publish_review_import(root, {'receipt_sha256': _sha(root / 'REVIEW.json'),
                 'evidence_sha256': _sha(root / 'REVIEW_EVIDENCE'),
                 'source_receipt_sha256': marker['source_receipt_sha256'], 'started_sha256': started_sha})
            _retained_review(root, prepared, request, packet)
        except BaseException as exc:
            # Preserve the first error. If storage cannot retain even this
            # failure record, visible admission still forbids every reimport.
            if present('REVIEW_STARTED.json'):
                try:
                    _put(root / 'REVIEW_FAILED.json', {**marker, 'kind': 'factory-simple-review-failed-v1',
                                                      'error': type(exc).__name__})
                except BaseException:
                    pass
            raise
    if not any(present(n) for n in artifacts):
        return {'state': 'unmet', 'run_id': prepared['run_id'], 'missing': ['independent_review'], 'packet_sha256': _sha(root / 'REVIEW_PACKET.json')}
    try:
        receipt, imported = _retained_review(root, prepared, request, packet)
    except (Blocked, OSError, ValueError, KeyError, TypeError):
        return {'state': 'review_import_incomplete', 'run_id': prepared['run_id'],
                'reason': 'retained_review_import_not_complete_or_valid',
                'action': 'stop; preserve all files; no reimport/session switch or positive report; operator resolution required'}
    positive = all(v == 'PASS' for v in receipt['verdicts'].values())
    return {'state': 'PASS' if positive else 'unmet', 'run_id': prepared['run_id'], 'C_tree': check_value['C_tree'],
            'packet_sha256': _sha(root / 'REVIEW_PACKET.json'), 'verdicts': receipt['verdicts'],
            'reviewer_session_id': receipt['reviewer_session_id'],
            'action': 'retain immutable successful run' if positive else
                      'review rejected or unknown: fresh prepare --predecessor this-run; new run/check/reviewer only',
            'trust': 'local_developer_and_operator_supplied_review_provenance'}
