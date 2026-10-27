"""Minimal evidence interface checks; native/Git transports are explicit seams."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import importlib.machinery
import json
import os
from pathlib import Path
import stat
import sys
import types
import unittest
import subprocess
from unittest.mock import patch

from aios.factory import simple, _simple_check, cli
from aios.factory.contract import Blocked
from contextlib import contextmanager
import tempfile
import time

@contextmanager
def directory():
    with tempfile.TemporaryDirectory(prefix='factory-test-', dir=Path(tempfile.gettempdir()).resolve()) as path:
        yield Path(path)


class SimpleEvidenceChecks(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(simple, '_collection_lock', return_value=contextlib.nullcontext())
        patcher.start(); self.addCleanup(patcher.stop)

    @contextlib.contextmanager
    def refusal(self, code):
        with self.assertRaises(Blocked) as seen:
            yield
        self.assertEqual(seen.exception.code, code)

    def request(self, root):
        tool_path = str(Path(sys.executable).resolve())
        tool = {'path': tool_path, 'sha256': simple._sha(tool_path)}
        return {'kind': 'factory-simple-request-v1', 'allowed_paths': ['minimal_target.py'],
                'target_modules': ['minimal_target'], 'test_modules': ['test_minimal_target'],
                'required_test_ids': ['test_minimal_target.Check.test_ok'], 'criteria': ['addition works'],
                'implementer_id': 'local-implementer', 'implementation_session_id': 'implementation-session-1',
                'python': dict(tool), 'git': dict(tool),
                'owner_hard_stop_utc': (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace('+00:00', 'Z')}

    def fixture(self, root, test=None, *, bind=False):
        baseline = root / 'baseline'; baseline.mkdir()
        (baseline / 'minimal_target.py').write_text('def add(a,b): return a-b\n')
        (baseline / 'test_minimal_target.py').write_text(test or
            'import unittest,minimal_target\nclass Check(unittest.TestCase):\n def test_ok(self): self.assertEqual(minimal_target.add(2,3),5)\n')
        request = root / 'request.json'; value=self.request(root)
        if bind:
            value['python'].pop('sha256'); value['git'].pop('sha256')
        simple._put(request, value)
        run = root / 'run'
        simple.prepare(request, baseline, run, bind_tools=bind)
        (run / 'work/minimal_target.py').write_text('def add(a,b): return a+b\n')
        return run

    def bootstrap(self, root, request):
        # Unit-only clean module-map seam: production uses a separate -I -B -S
        # interpreter. No child is launched by this test.
        stdlib = Path(os.__file__).resolve().parent
        clean = {name: module for name, module in sys.modules.items()
                 if not getattr(module, '__file__', None) or
                 (Path(module.__file__).resolve().is_relative_to(stdlib) and
                  'site-packages' not in Path(module.__file__).parts)}
        with patch.dict(sys.modules, clean, clear=True), patch.object(sys, 'path', list(sys.path)):
            return _simple_check.run(root, request)

    def checked(self, root, *, unknown=False, fail=False, bind=False, factory=None):
        if sys.platform == 'win32':
            from aios.factory.platforms import _simple_windows as p
        else:
            from aios.factory.platforms import _posix as p
        run = factory(root) if factory is not None else self.fixture(root,bind=bind)
        outer = self
        class Supervisor:
            def __init__(self, path, **kw):
                self.run = path; self.records = []; self.unknown = False
                self.logs = path / 'logs'; self.logs.mkdir(); self.started = kw.get('started', kw.get('anchor'))
                self.deadline = kw.get('productive', self.started + kw.get('seconds', 0))
            def call(self, command, **kw):
                outer.assertEqual(command[1:4], ['-I', '-B', '-S'])
                outer.assertEqual(set(kw['env']), {'PATH','LANG','HOME','TMPDIR','PYTHONDONTWRITEBYTECODE','PYTHONNOUSERSITE'})
                outer.assertEqual(kw['cwd'], run / 'C')
                if unknown:
                    self.unknown = True; self.records.append({'known_ended': False})
                    raise Blocked('synthetic_unknown')
                result = outer.bootstrap(run / 'C', simple._read(run / 'request.json'))
                if fail: result['passed'] = False
                simple._put(run / 'TEST_RESULT.json', result)
                out = self.logs / '0001.stdout'; out.write_bytes(b'actual unit bootstrap result retained separately\n')
                err = self.logs / '0001.stderr'; err.write_bytes(b'')
                record = {'known_ended': True, 'exit_code': 0 if result['passed'] else 2,
                          'pid': 990001, 'stdout': str(out), 'stderr': str(err)}
                self.records.append(record); simple._put(self.logs / '0001.json', record)
                return record
        def capture(supervisor, git, root):
            (root / 'FULL_DIFF.patch').write_bytes(b'unit Git transport seam: actual pure tree identities below\n')
            return simple.tree_identity(root / 'B'), simple.tree_identity(root / 'C')
        with patch.object(p, 'Supervisor', Supervisor), patch.object(p, 'RunLock', return_value=contextlib.nullcontext()), \
             patch.object(simple, '_runtime_origins', return_value={'subprocess': {'path': str(Path(subprocess.__file__).resolve()), 'sha256': simple._sha(Path(subprocess.__file__).resolve())}}), \
             patch.object(simple, '_capture', side_effect=capture):
            result = simple.check(run)
        return run, result

    def receipt(self, run, dest):
        packet = simple._read(run / 'REVIEW_PACKET.json'); prepared = simple._read(run / 'PREPARED.json')
        evidence = dest / 'review.md'; evidence.write_text('External review fixture; not real independent acceptance.\n')
        return {'kind': 'factory-simple-review-v1', 'run_id': prepared['run_id'],
                'packet_sha256': simple._sha(run / 'REVIEW_PACKET.json'), 'reviewer_id': 'external-reviewer',
                'reviewer_session_id': 'review-session-2', 'implementation_session_id': 'implementation-session-1',
                'reviewed_utc': simple._now(), 'verdicts': {k:'PASS' for k in simple.FACETS},
                'evidence': {'path': 'review.md', 'sha256': simple._sha(evidence)}}

    def test_prepare_copies_exact_isolated_baseline_request_and_work(self):
        with directory() as root:
            run = self.fixture(root); prepared = simple._read(run / 'PREPARED.json')
            self.assertEqual(prepared['runtime_binding'], simple._runtime_binding(simple._read(run / 'request.json')))
            self.assertNotEqual(simple._source(run / 'B'), simple._source(run / 'work'))
            self.assertEqual(prepared['baseline'], simple._source(run / 'B'))
            self.assertEqual(prepared['B_tree'], simple.tree_identity(run / 'B'))
            with self.refusal('run_isolation'): simple.prepare(root/'request.json',root/'baseline',run)

    def test_abrupt_check_admission_holds_collection_before_any_child(self):
        with directory() as root:
            run = self.fixture(root); original = simple._put
            def interrupted(path, value):
                if Path(path).name == 'CHECK_STARTED.json': raise OSError('synthetic interruption')
                return original(path, value)
            with patch.object(simple, '_put', side_effect=interrupted):
                with self.assertRaises(OSError): simple.check(run)
            marker = root / 'SIMPLE_ACTIVE.json'
            self.assertEqual(simple._read(marker)['run_id'], simple._read(run / 'PREPARED.json')['run_id'])
            with self.refusal('collection_incomplete_or_unknown'): simple.check(run)
            with self.refusal('collection_incomplete_or_unknown'):
                simple.prepare(root / 'request.json', root / 'baseline', root / 'another-run')
            self.assertEqual(simple.report(run)['state'], 'held_unknown')
            self.assertTrue(marker.is_file())

    def test_failed_terminal_publication_leaves_collection_held(self):
        with directory() as root:
            original = simple._put
            def interrupted(path, value):
                if Path(path).name == 'CHECK.json': raise OSError('synthetic terminal publication failure')
                return original(path, value)
            with patch.object(simple, '_put', side_effect=interrupted):
                with self.assertRaises(OSError): self.checked(root)
            self.assertTrue((root / 'SIMPLE_ACTIVE.json').exists())
            with self.refusal('collection_incomplete_or_unknown'):
                simple.prepare(root / 'request.json', root / 'baseline', root / 'another')

    def test_known_ended_failed_check_releases_marker(self):
        with directory() as root:
            run, result = self.checked(root, fail=True)
            self.assertFalse(result['passed']); self.assertTrue(result['known_ended'])
            self.assertFalse((root / 'SIMPLE_ACTIVE.json').exists())
            simple.prepare(root / 'request.json', root / 'baseline', root / 'next', run)

    def test_tracked_snapshot_accounts_for_dotfiles_and_excludes_git_metadata(self):
        with directory() as root:
            baseline = root / 'baseline'; baseline.mkdir()
            (baseline / '.gitignore').write_text('ignored\n')
            (baseline / 'minimal_target.py').write_text('value = 1\n')
            tracked = simple._source(baseline); tree = simple.tree_identity(baseline)
            (baseline / '.git').mkdir(); (baseline / '.git' / 'private').write_text('metadata')
            value = self.request(root); value['tracked_snapshot'] = {'git_tree': tree, 'files': tracked}
            request = root / 'request.json'; simple._put(request, value)
            run = root / 'run'; simple.prepare(request, baseline, run)
            self.assertEqual((run / 'B' / '.gitignore').read_text(), 'ignored\n')
            self.assertFalse((run / 'B' / '.git').exists())
            self.assertTrue(simple._read(run / 'PREPARED.json')['excluded_root_git_metadata'])
            (baseline / '.untracked').write_text('unlisted')
            with self.refusal('untracked_checkout_entry'):
                simple.prepare(request, baseline, root / 'another')

    def test_windows_executable_tracked_mode_is_explicitly_refused(self):
        with directory() as root:
            with patch.object(simple, 'os', types.SimpleNamespace(name='nt')):
                with self.refusal('windows_executable_mode_unsupported'):
                    simple._tracked_source(root, {'files': [{'mode': '100755'}]})

    def test_source_root_and_pinned_dependency_imports(self):
        with directory() as root:
            baseline = root / 'baseline'; baseline.mkdir()
            (baseline / 'src' / 'samplepkg').mkdir(parents=True)
            (baseline / 'src' / 'samplepkg' / '__init__.py').write_text('import sampledep, nsfixture.child.part\nvalue = sampledep.value + nsfixture.child.part.value\n')
            (baseline / 'tests').mkdir(); (baseline / 'tests' / '__init__.py').write_text('')
            (baseline / 'tests' / 'test_sample.py').write_text('import unittest, samplepkg\nclass Check(unittest.TestCase):\n def test_ok(self): self.assertEqual(samplepkg.value, 7)\n')
            deps = root / 'deps'; deps.mkdir(); (deps / 'sampledep.py').write_text('value = 7\n')
            (deps / 'tests' / 'data').mkdir(parents=True); (deps / 'tests' / 'data' / 'README.txt').write_text('fixture data\n')
            (deps / 'bin').mkdir(); (deps / 'bin' / 'tool').write_text('non-importable data\n')
            (deps / 'nsfixture' / 'child').mkdir(parents=True); (deps / 'nsfixture' / 'child' / 'part.py').write_text('value = 0\n')
            value = self.request(root); value.update(source_root='src', target_modules=['samplepkg'],
                test_modules=['tests.test_sample'], required_test_ids=['tests.test_sample.Check.test_ok'],
                dependencies={'root': str(deps), 'files': simple._dependency_rows(deps)})
            request = root / 'request.json'; simple._put(request, value)
            run = root / 'run'; simple.prepare(request, baseline, run)
            result = self.bootstrap(run / 'work', simple._read(run / 'request.json'))
            self.assertTrue(result['passed'])
            self.assertEqual(result['modules']['samplepkg']['path'], str(run / 'work' / 'src' / 'samplepkg' / '__init__.py'))
            self.assertIn('sampledep', result['dependency_origins'])
            self.assertEqual(result['dependency_origins']['nsfixture']['namespace_paths'], [str(deps / 'nsfixture')])
            self.assertEqual(result['dependency_origins']['nsfixture.child']['namespace_paths'], [str(deps / 'nsfixture' / 'child')])
            for suffix in ('.py', '.pyc'):
                shadow = run / 'work' / ('sampledep' + suffix); shadow.write_bytes(b'shadow')
                with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, 'dependency_shadow'):
                    self.bootstrap(run / 'work', simple._read(run / 'request.json'))
                shadow.unlink()
            shadow_dir = run / 'work' / 'nsfixture'; shadow_dir.mkdir()
            with self.assertRaisesRegex(ValueError, 'dependency_shadow'):
                self.bootstrap(run / 'work', simple._read(run / 'request.json'))
            shadow_dir.rmdir()
            (deps / 'sampledep.py').write_text('value = 8\n')
            with self.refusal('dependency_drift'): simple._request(value)

    def test_dependency_roots_use_real_import_suffixes_and_namespace_contents(self):
        inventory = [{'path': name} for name in ('tests/data/README.txt', 'bin/tool',
            'pkg/__init__.py', 'namespace/child/part.py', 'extra.pyw',
            'compiled.cp312-win_amd64.pyd', 'bytecode.pyc')]
        with patch.object(importlib.machinery, 'all_suffixes',
                          return_value=['.py', '.pyw', '.pyc', '.cp312-win_amd64.pyd', '.pyd']):
            names = _simple_check.dependency_module_roots(inventory)
            self.assertEqual(names, {'pkg', 'namespace', 'extra', 'compiled', 'bytecode'})
            self.assertEqual(_simple_check.importable_file_name('compiled.cp312-win_amd64.pyd'), 'compiled')
            self.assertIsNone(_simple_check.importable_file_name('note.txt'))

    def test_windows_pyw_dependency_cannot_be_shadowed_by_candidate_python(self):
        with directory() as root:
            run = self.fixture(root)
            deps = root / 'deps'; deps.mkdir(); (deps / 'sampledep.pyw').write_text('value = 7\n')
            (run / 'work' / 'sampledep.py').write_text('value = 99\n')
            request = simple._read(run / 'request.json')
            request['dependencies'] = {'root': str(deps), 'files': simple._dependency_rows(deps)}
            with patch.object(importlib.machinery, 'all_suffixes', return_value=['.py', '.pyw', '.pyc']):
                with self.assertRaisesRegex(ValueError, 'dependency_shadow'):
                    self.bootstrap(run / 'work', request)

    def test_imported_data_only_namespaces_cannot_mix_candidate_and_dependency_roots(self):
        with directory() as root:
            run = self.fixture(root)
            candidate_ns = run / 'work' / 'datanamespace'; candidate_ns.mkdir()
            (candidate_ns / 'note.txt').write_text('candidate data\n')
            deps = root / 'deps'; deps.mkdir(); dep_ns = deps / 'datanamespace'; dep_ns.mkdir()
            (dep_ns / 'note.txt').write_text('dependency data\n')
            (run / 'work' / 'minimal_target.py').write_text('import datanamespace\ndef add(a,b): return a+b\n')
            request = simple._read(run / 'request.json')
            request['dependencies'] = {'root': str(deps), 'files': simple._dependency_rows(deps)}
            result = self.bootstrap(run / 'work', request)
            self.assertFalse(result['passed'])
            self.assertEqual(result['unexpected_origins']['datanamespace'], 'mixed_dependency_namespace')

    def test_source_root_rejects_root_shadow_and_pth_dependencies(self):
        with directory() as root:
            baseline = root / 'baseline'; baseline.mkdir()
            (baseline / 'src').mkdir(); (baseline / 'src' / 'samplepkg.py').write_text('value = 1\n')
            (baseline / 'samplepkg.py').write_text('value = 2\n')
            (baseline / 'test_sample.py').write_text('import unittest, samplepkg\nclass Check(unittest.TestCase):\n def test_ok(self): pass\n')
            value = self.request(root); value.update(source_root='src', target_modules=['samplepkg'],
                test_modules=['test_sample'], required_test_ids=['test_sample.Check.test_ok'])
            request = root / 'request.json'; simple._put(request, value)
            run = root / 'run'; simple.prepare(request, baseline, run)
            with self.assertRaisesRegex(ValueError, 'module_origin_mismatch'):
                self.bootstrap(run / 'work', simple._read(run / 'request.json'))
            deps = root / 'deps'; deps.mkdir(); (deps / 'extra.pth').write_text('unsafe\n')
            with self.refusal('dependency_inventory'): simple._dependency_rows(deps)

    def test_check_src_dependency_packet_and_live_dependency_constraints(self):
        with directory() as root:
            def setup(base):
                baseline = base / 'baseline'; baseline.mkdir()
                (baseline / 'src' / 'samplepkg').mkdir(parents=True)
                (baseline / 'src' / 'samplepkg' / '__init__.py').write_text('value = 0\n')
                (baseline / 'tests').mkdir(); (baseline / 'tests' / '__init__.py').write_text('')
                (baseline / 'tests' / 'test_sample.py').write_text('import unittest, samplepkg\nclass Check(unittest.TestCase):\n def test_ok(self): self.assertEqual(samplepkg.value, 7)\n')
                deps = base / 'deps'; deps.mkdir(); (deps / 'sampledep.py').write_text('value = 7\n')
                value = self.request(base); value.update(source_root='src', allowed_paths=['src/samplepkg/__init__.py'],
                    target_modules=['samplepkg'], test_modules=['tests.test_sample'],
                    required_test_ids=['tests.test_sample.Check.test_ok'],
                    dependencies={'root': str(deps), 'files': simple._dependency_rows(deps)})
                simple._put(base / 'request.json', value)
                run = base / 'run'; simple.prepare(base / 'request.json', baseline, run)
                (run / 'work' / 'src' / 'samplepkg' / '__init__.py').write_text('import sampledep\nvalue = sampledep.value\n')
                return run
            run, result = self.checked(root, factory=setup)
            self.assertTrue(result['passed'])
            packet_dep = run / 'review-packet' / 'dependencies' / 'sampledep.py'
            self.assertEqual(packet_dep.read_bytes(), (root / 'deps' / 'sampledep.py').read_bytes())
            receipt = self.receipt(run, root); receipt['verdicts']['security'] = 'FAIL'
            simple._put(root / 'receipt.json', receipt)
            self.assertEqual(simple.report(run, root / 'receipt.json')['state'], 'unmet')
            (root / 'deps' / 'sampledep.py').write_text('value = 8\n')
            with self.refusal('dependency_drift'): simple.report(run)
            with self.refusal('dependency_drift'):
                simple.prepare(root / 'request.json', root / 'baseline', root / 'next', run)
            (root / 'deps' / 'sampledep.py').write_text('value = 7\n')
            packet_dep.write_text('value = 9\n')
            manifest = simple._read(run / 'REVIEW_PACKET.json')
            manifest['files'] = simple.rows(run / 'review-packet')
            (run / 'REVIEW_PACKET.json').write_bytes(simple._encoded(manifest))
            with self.refusal('dependency_packet_drift'): simple.report(run)

    def test_runtime_binding_refuses_changed_python_or_stdlib(self):
        with directory() as root:
            run = self.fixture(root)
            prepared = simple._read(run / 'PREPARED.json')
            request = simple._read(run / 'request.json')
            changed = dict(prepared)
            changed['runtime_binding'] = {**prepared['runtime_binding'],
                'subprocess': {**prepared['runtime_binding']['subprocess'], 'sha256': '0' * 64}}
            with self.refusal('runtime_binding_drift'):
                simple._runtime_origins(changed, request)
            with self.refusal('caller_python_drift'):
                simple._runtime_binding({**request, 'python': {**request['python'], 'sha256': '0' * 64}})

    def test_runtime_drift_after_checks_prevents_positive_result(self):
        with directory() as root:
            actual = simple._runtime_binding
            calls = []
            def binding(request):
                value = actual(request)
                calls.append(1)
                if len(calls) > 1:
                    value['subprocess']['sha256'] = '0' * 64
                return value
            with patch.object(simple, '_runtime_binding', side_effect=binding):
                _, result = self.checked(root)
            self.assertFalse(result['passed'])
            self.assertEqual(result['reason'], 'source_or_helper_changed')

    def test_request_rejects_unbounded_paths_empty_ids_unknown_session_and_replaced_tool(self):
        with directory() as root:
            request = self.request(root)
            for key, value, code in [('allowed_paths',['../outside'],'invalid_path'),('test_modules',['bad/name'],'module_name'),
                               ('required_test_ids',[],'request_list'),('implementation_session_id','unknown','implementation_identity'),
                               ('python',dict(path=request['python']['path'],sha256='0'*64),'tool_identity')]:
                with self.subTest(key=key), self.refusal(code):
                    simple._request({**request,key:value})

    def test_actual_bootstrap_origins_counts_and_source_immutability(self):
        with directory() as root:
            run = self.fixture(root); request = simple._read(run/'request.json')
            result = self.bootstrap(run/'work',request)
            self.assertTrue(result['passed']); self.assertEqual(result['run'],1)
            self.assertEqual(result['ids'],request['required_test_ids'])
            self.assertEqual(result['modules']['minimal_target']['path'],str(run/'work/minimal_target.py'))
            self.assertEqual(result['source_before'],result['source_after'])
            self.assertEqual(result['unexpected_origins'],{})

    def test_windows_stdlib_hashlib_origin_is_narrow_and_detects_byte_drift(self):
        with directory() as root:
            dlls = root / 'DLLs'; dlls.mkdir()
            library = dlls / '_hashlib.pyd'; library.write_bytes(b'initial')
            loader = importlib.machinery.ExtensionFileLoader('_hashlib', str(library))
            module = types.SimpleNamespace(__file__=str(library),
                __spec__=types.SimpleNamespace(loader=loader, origin=str(library)))
            first = _simple_check.windows_hashlib_origin('_hashlib', module, root, True)
            self.assertEqual(first, {'path': str(library), 'sha256': simple.digest(b'initial')})
            library.write_bytes(b'tampered')
            self.assertNotEqual(first, _simple_check.windows_hashlib_origin('_hashlib', module, root, True))
            self.assertIsNone(_simple_check.windows_hashlib_origin('_socket', module, root, True))
            self.assertIsNone(_simple_check.windows_hashlib_origin('_hashlib', module, root, False))
            module.__spec__.origin = str(root / 'other.pyd')
            self.assertIsNone(_simple_check.windows_hashlib_origin('_hashlib', module, root, True))
            module.__spec__.origin = str(library)
            outside = root / 'site-packages'; outside.mkdir()
            foreign = outside / '_hashlib.pyd'; foreign.write_bytes(b'initial')
            module.__file__ = str(foreign); module.__spec__.origin = str(foreign)
            self.assertIsNone(_simple_check.windows_hashlib_origin('_hashlib', module, root, True))

    def test_windows_extension_set_rejects_linked_or_unlisted_file(self):
        with directory() as root:
            dlls = root / 'DLLs'; dlls.mkdir()
            library = dlls / '_socket.pyd'; library.write_bytes(b'fixture')
            loader = importlib.machinery.ExtensionFileLoader('_socket', str(library))
            module = types.SimpleNamespace(__file__=str(library),
                __spec__=types.SimpleNamespace(loader=loader, origin=str(library)))
            self.assertEqual(_simple_check.windows_extension_origin('_socket', module, root, True)['sha256'], simple.digest(b'fixture'))
            self.assertIsNone(_simple_check.windows_extension_origin('unlisted', module, root, True))
            real_lstat = Path.lstat
            def linked(path):
                if path == library: return types.SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
                return real_lstat(path)
            with patch.object(Path, 'lstat', linked):
                self.assertIsNone(_simple_check.windows_extension_origin('_socket', module, root, True))

    def test_windows_extension_drift_blocks_bootstrap(self):
        with directory() as root:
            run = self.fixture(root)
            initial = {'path': 'fixed', 'sha256': 'a' * 64}
            changed = {'path': 'fixed', 'sha256': 'b' * 64}
            with patch.object(_simple_check, 'WINDOWS_STDLIB_EXTENSIONS', {'_hashlib'}), \
                 patch.object(_simple_check, 'windows_extension_file', side_effect=[initial, changed]), \
                 patch.object(_simple_check, 'windows_extension_origin', return_value=initial):
                result = self.bootstrap(run / 'work', simple._read(run / 'request.json'))
            self.assertFalse(result['passed'])
            self.assertFalse(result['invalid_windows_extension_before'])
            self.assertEqual(result['unexpected_origins'], {})
            self.assertEqual((result['run'], result['failures'], result['errors'], result['skips']), (1, 0, 0, 0))
            self.assertTrue(result['source_unchanged'] and result['dependency_unchanged'])
            self.assertFalse(result['trusted_windows_extensions_unchanged'])
            self.assertEqual(result['trusted_windows_extensions_before']['_hashlib'], initial)
            self.assertEqual(result['trusted_windows_extensions']['_hashlib'], changed)

    def test_bootstrap_skip_error_missing_ids_and_zero_tests_are_nonpositive(self):
        variants = ["import unittest\nclass Check(unittest.TestCase):\n @unittest.skip('no')\n def test_ok(self): pass\n",
                    "import unittest\nclass Check(unittest.TestCase):\n def test_ok(self): raise RuntimeError('error')\n",
                    "import unittest\nclass Check(unittest.TestCase):\n def test_other(self): pass\n",
                    'import unittest\n']
        for test in variants:
            with self.subTest(test=test),directory() as root:
                run=self.fixture(root,test)
                self.assertFalse(self.bootstrap(run/'work',simple._read(run/'request.json'))['passed'])

    def test_bootstrap_target_source_mutation_is_nonpositive(self):
        test="import unittest,minimal_target\nfrom pathlib import Path\nclass Check(unittest.TestCase):\n def test_ok(self): Path(minimal_target.__file__).write_text('changed')\n"
        with directory() as root:
            run=self.fixture(root,test);result=self.bootstrap(run/'work',simple._read(run/'request.json'))
            self.assertFalse(result['passed']);self.assertFalse(result['source_unchanged'])

    def test_consumption_precedes_children_and_second_check_cannot_launch(self):
        with directory() as root:
            run,result=self.checked(root);self.assertTrue(result['passed'])
            self.assertEqual(result['packet_sha256'],simple._sha(run/'REVIEW_PACKET.json'))
            self.assertNotIn('packet_sha256',simple._read(run/'CHECK.json'))
            bound=simple._read(run/'CHECK_STARTED.json')
            self.assertLessEqual(bound['productive_deadline_ns']-bound['started_monotonic_ns'],120_000_000_000)
            self.assertLessEqual(bound['hard_deadline_ns']-bound['started_monotonic_ns'],130_000_000_000)
            with self.refusal('check_consumed'):simple.check(run)
            self.assertEqual(simple.report(run)['state'],'unmet')

    def test_packet_validation_failure_never_returns_a_packet_hash(self):
        with directory() as root:
            with patch.object(simple,'_review_packet',side_effect=Blocked('review_packet_drift')):
                run,result=self.checked(root)
            self.assertFalse(result['passed']);self.assertEqual(result['reason'],'packet_finalization_failed')
            self.assertNotIn('packet_sha256',result)
            self.assertTrue((run/'FINALIZATION_FAILED.json').is_file())
            with self.refusal('packet_finalization_failed'):simple.report(run)

    def test_allowed_diff_empty_and_foreign_path_reject_before_capture(self):
        for mode in ('empty','foreign'):
            with self.subTest(mode=mode),directory() as root:
                run=self.fixture(root)
                if mode=='empty':(run/'work/minimal_target.py').write_bytes((run/'B/minimal_target.py').read_bytes())
                else:(run/'work/foreign.py').write_text('x=1')
                with patch.object(simple,'_capture') as capture:result=simple.check(run)
                capture.assert_not_called();self.assertFalse(result['passed']);self.assertTrue(result['known_ended'])
                self.assertEqual(simple.report(run)['state'],'restart_required')

    def test_unknown_termination_latches_parent_and_blocks_fresh_prepare(self):
        with directory() as root:
            run,result=self.checked(root,unknown=True)
            self.assertFalse(result['passed']);self.assertFalse(result['known_ended'])
            self.assertEqual(simple.report(run)['state'],'held_unknown')
            with self.refusal('collection_incomplete_or_unknown'):
                simple.prepare(root/'request.json',root/'baseline',root/'new',run)

    def test_manual_restart_is_new_identity_after_closed_failed_check(self):
        with directory() as root:
            run,result=self.checked(root,fail=True);self.assertFalse(result['passed'])
            frozen=simple._sha(run/'CHECK.json')
            fresh=root/'fresh';simple.prepare(root/'request.json',root/'baseline',fresh,run)
            self.assertNotEqual(simple._read(run/'PREPARED.json')['run_id'],simple._read(fresh/'PREPARED.json')['run_id'])
            self.assertEqual(simple._read(fresh/'PREPARED.json')['predecessor']['check_sha256'],frozen)
            self.assertFalse((fresh/'CHECK_STARTED.json').exists());self.assertEqual(simple._sha(run/'CHECK.json'),frozen)

    def test_independent_receipt_binds_exact_packet_and_is_imported_once(self):
        with directory() as root:
            run,result=self.checked(root);receipt=self.receipt(run,root);path=root/'receipt.json';simple._put(path,receipt)
            self.assertEqual(simple.report(run,path)['state'],'PASS')
            self.assertEqual(simple.report(run)['state'],'PASS')
            with self.refusal('review_import_consumed'):simple.report(run,path)
            (run/'work/minimal_target.py').write_text('changed')
            with self.refusal('candidate_drift'):simple.report(run)

    def test_same_session_stale_mismatched_and_missing_facets_refuse(self):
        changes=[({'reviewer_session_id':'implementation-session-1'},'review_not_independent'), ({'packet_sha256':'0'*64},'review_binding'),
                 ({'run_id':'different'},'review_binding'), ({'implementation_session_id':'different'},'review_binding'),
                 ({'reviewed_utc':'2000-01-01T00:00:00Z'},'review_stale'), ({'verdicts':{'functionality':'PASS'}},'review_facets')]
        for mutation, code in changes:
            with self.subTest(mutation=mutation),directory() as root:
                run,_=self.checked(root);receipt={**self.receipt(run,root),**mutation}
                path=root/'receipt.json';simple._put(path,receipt)
                with self.refusal(code):simple.report(run,path)
                self.assertFalse((run/'REVIEW.json').exists())

    def test_negative_review_and_imported_receipt_mutation_cannot_pass(self):
        with directory() as root:
            run,_=self.checked(root);receipt=self.receipt(run,root);receipt['verdicts']['security']='UNKNOWN'
            path=root/'receipt.json';simple._put(path,receipt)
            self.assertEqual(simple.report(run,path)['state'],'unmet')
            saved=simple._read(run/'REVIEW.json');saved['verdicts']['security']='PASS'
            (run/'REVIEW.json').write_text(json.dumps(saved))
            self.assertEqual(simple.report(run)['state'],'review_import_incomplete')
            with self.refusal('imported_review_drift'):
                simple.prepare(root/'request.json',root/'baseline',root/'bad-receipt',run)

    def test_previously_claimed_reviewer_session_and_relocated_run_refuse(self):
        import shutil
        with directory() as root:
            run,_=self.checked(root);receipt=self.receipt(run,root)
            sessions=root/'simple-review-sessions';sessions.mkdir()
            simple._put(sessions/(simple.digest(receipt['reviewer_session_id'].encode())+'.json'),
                        {'run_id':'previous-run','packet_sha256':'0'*64})
            path=root/'receipt.json';simple._put(path,receipt)
            with self.refusal('review_session_consumed'):simple.report(run,path)
            self.assertFalse((run/'REVIEW.json').exists())
            moved=root/'copied-run';shutil.copytree(run,moved)
            with self.refusal('relocated_run'):simple.report(moved)

    def test_expired_original_owner_bound_consumes_without_capture(self):
        with directory() as root:
            self.fixture(root);request=self.request(root);request['owner_hard_stop_utc']='2000-01-01T00:00:00Z'
            path=root/'expired.json';simple._put(path,request)
            run=root/'expired';simple.prepare(path,root/'baseline',run)
            with patch.object(simple,'_capture') as capture:result=simple.check(run)
            capture.assert_not_called();self.assertFalse(result['passed']);self.assertTrue(result['known_ended'])
            self.assertTrue((run/'CHECK_STARTED.json').exists())
            with self.refusal('check_consumed'):simple.check(run)

    def test_packet_publication_error_stops_positive_report_without_republish(self):
        with directory() as root:
            original=simple._put
            def put(path,value):
                if Path(path).name=='REVIEW_PACKET.json':raise OSError('synthetic evidence failure')
                return original(path,value)
            with patch.object(simple,'_put',side_effect=put):run,result=self.checked(root)
            self.assertFalse(result['passed']);self.assertTrue((run/'FINALIZATION_FAILED.json').is_file())
            with self.refusal('packet_finalization_failed'):simple.report(run)

    def test_source_helper_and_packet_drift_refuse(self):
        for mode in ('helper','packet','request'):
            with self.subTest(mode=mode),directory() as root:
                run,_=self.checked(root)
                if mode=='helper':
                    with patch.object(simple,'_helper',return_value=[]),self.refusal('helper_drift'):simple.report(run)
                elif mode=='packet':
                    (run/'review-packet/FULL_DIFF.patch').write_text('changed')
                    with self.refusal('review_packet_drift'):simple.report(run)
                else:
                    (run/'request.json').write_text('{}')
                    with self.refusal('request_shape'):simple.report(run)

    def test_mixed_helper_origin_is_rejected(self):
        with directory() as root:
            prepared={'helper':simple._helper(), 'runtime_binding':simple._runtime_binding(self.request(root))}
            foreign=root/'foreign.py';foreign.write_text('x=1')
            module=types.SimpleNamespace(__file__=str(foreign))
            with patch.dict(sys.modules,{'aios.factory.foreign':module}),self.refusal('mixed_helper_origin'):
                simple._runtime_origins(prepared,self.request(root))

    def test_restart_after_validated_fail_or_unknown_review_binds_retained_evidence(self):
        for verdict in ('FAIL','UNKNOWN'):
            with self.subTest(verdict=verdict),directory() as root:
                run,_=self.checked(root);receipt=self.receipt(run,root)
                receipt['verdicts']['functionality']=verdict
                path=root/'receipt.json';simple._put(path,receipt)
                result=simple.report(run,path)
                self.assertEqual(result['state'],'unmet');self.assertIn('--predecessor',result['action'])
                names=('CHECK.json','REVIEW.json','REVIEW_IMPORT.json','REVIEW_EVIDENCE','REVIEW_PACKET.json')
                before={name:simple._sha(run/name) for name in names}
                fresh=root/'after-review';simple.prepare(root/'request.json',root/'baseline',fresh,run)
                pred=simple._read(fresh/'PREPARED.json')['predecessor']
                self.assertEqual(pred['check_sha256'],before['CHECK.json'])
                self.assertEqual(pred['review_import_sha256'],before['REVIEW_IMPORT.json'])
                self.assertEqual(pred['review_receipt_sha256'],before['REVIEW.json'])
                self.assertEqual(pred['review_evidence_sha256'],before['REVIEW_EVIDENCE'])
                self.assertEqual(pred['review_packet_sha256'],before['REVIEW_PACKET.json'])
                self.assertEqual(before,{name:simple._sha(run/name) for name in names})
                self.assertFalse((fresh/'CHECK_STARTED.json').exists())
                self.assertNotEqual(simple._read(fresh/'PREPARED.json')['run_id'],pred['run_id'])

    def test_negative_review_restart_refuses_tampered_incomplete_or_unbound_evidence(self):
        variants=('receipt','import','evidence','missing_import','presence_only','self_session',
                  'packet_binding','run_binding','implementation_binding','invalid_facets','unknown_end','check_binding')
        for mode in variants:
            with self.subTest(mode=mode),directory() as root:
                run,_=self.checked(root);receipt=self.receipt(run,root)
                receipt['verdicts']['security']='FAIL'
                path=root/'receipt.json';simple._put(path,receipt);simple.report(run,path)
                if mode=='evidence':(run/'REVIEW_EVIDENCE').write_bytes(b'tampered')
                elif mode=='missing_import':(run/'REVIEW_IMPORT.json').unlink()
                elif mode=='presence_only':
                    (run/'REVIEW_IMPORT.json').unlink();(run/'REVIEW_EVIDENCE').unlink()
                elif mode=='import':(run/'REVIEW_IMPORT.json').write_text('{}')
                elif mode in ('unknown_end','check_binding'):
                    value=simple._read(run/'CHECK.json')
                    value['known_ended' if mode=='unknown_end' else 'run_id']=False if mode=='unknown_end' else 'different'
                    (run/'CHECK.json').write_text(json.dumps(value))
                else:
                    value=simple._read(run/'REVIEW.json')
                    if mode=='receipt':value['reviewer_id']='tampered'
                    if mode=='self_session':value['reviewer_session_id']='implementation-session-1'
                    if mode=='packet_binding':value['packet_sha256']='0'*64
                    if mode=='run_binding':value['run_id']='different'
                    if mode=='implementation_binding':value['implementation_session_id']='different'
                    if mode=='invalid_facets':value['verdicts']={'security':'FAIL'}
                    (run/'REVIEW.json').write_text(json.dumps(value))
                    if mode!='receipt':
                        imported=simple._read(run/'REVIEW_IMPORT.json')
                        imported['receipt_sha256']=simple._sha(run/'REVIEW.json')
                        (run/'REVIEW_IMPORT.json').write_text(json.dumps(imported))
                fresh=root/'rejected-restart'
                if mode in ('missing_import','presence_only'):
                    with self.assertRaises(FileNotFoundError) as seen:
                        simple.prepare(root/'request.json',root/'baseline',fresh,run)
                    self.assertEqual(seen.exception.filename,str(run/'REVIEW_IMPORT.json'))
                else:
                    codes={'receipt':'imported_review_drift','import':'review_import_shape',
                           'evidence':'imported_review_drift','self_session':'review_not_independent',
                           'packet_binding':'review_binding','run_binding':'review_binding',
                           'implementation_binding':'review_binding','invalid_facets':'review_facets',
                           'unknown_end':'restart_predecessor_not_closed_failure','check_binding':'predecessor_check_binding'}
                    with self.refusal(codes[mode]):
                        simple.prepare(root/'request.json',root/'baseline',fresh,run)
                self.assertFalse(fresh.exists())

    def test_all_pass_review_cannot_be_a_rejected_predecessor(self):
        with directory() as root:
            run,_=self.checked(root);path=root/'receipt.json';simple._put(path,self.receipt(run,root))
            self.assertEqual(simple.report(run,path)['state'],'PASS')
            with self.refusal('restart_predecessor_review_not_negative'):
                simple.prepare(root/'request.json',root/'baseline',root/'not-a-failure',run)

    def test_review_copy_failure_consumes_claim_without_trusting_negative_review(self):
        with directory() as root:
            run,_=self.checked(root);receipt=self.receipt(run,root);receipt['verdicts']['security']='FAIL'
            path=root/'receipt.json';simple._put(path,receipt)
            original=Path.open
            def opened(path,*args,**kw):
                if path==run/'REVIEW_EVIDENCE':raise OSError('synthetic copy failure')
                return original(path,*args,**kw)
            with patch.object(Path,'open',opened),self.assertRaises(OSError) as seen:simple.report(run,path)
            self.assertEqual(seen.exception.args,('synthetic copy failure',))
            self.assertEqual(len(list((root/'simple-review-sessions').glob('*.json'))),1)
            self.assertFalse((run/'REVIEW.json').exists())
            with self.refusal('review_import_consumed'):simple.report(run,path)
            with self.refusal('review_import_incomplete'):
                simple.prepare(root/'request.json',root/'baseline',root/'not-imported',run)

    def test_failed_finalization_restart_still_binds_original_failure(self):
        with directory() as root:
            original=simple._put
            def put(path,value):
                if Path(path).name=='REVIEW_PACKET.json':raise OSError('synthetic seal failure')
                return original(path,value)
            with patch.object(simple,'_put',side_effect=put):run,result=self.checked(root)
            self.assertFalse(result['passed'])
            fresh=root/'after-finalization';simple.prepare(root/'request.json',root/'baseline',fresh,run)
            self.assertEqual(simple._read(fresh/'PREPARED.json')['predecessor']['finalization_failure_sha256'],
                             simple._sha(run/'FINALIZATION_FAILED.json'))

    def test_cli_refuses_legacy_commands(self):
        for command in sorted(cli.LEGACY):
            with self.subTest(command=command),contextlib.redirect_stderr(io.StringIO()) as out:
                self.assertEqual(cli.main([command]),2)
                self.assertEqual(json.loads(out.getvalue())['reason'],'unsupported_frozen_legacy')
        self.assertEqual(set(cli.parser()._subparsers._group_actions[0].choices),{'prepare','check','report'})

    def test_import_publication_faults_consume_whole_run_and_preserve_first_error(self):
        import hashlib
        stages=[part+'-'+when for part in ('marker','claim','receipt','import') for when in ('before','partial','full')]
        stages += ['raw-before','raw-partial','raw-full','link','unlink','publish-after']
        for stage in stages:
            with self.subTest(stage=stage),directory() as root:
                run,_=self.checked(root);path=root/'receipt.json';simple._put(path,self.receipt(run,root))
                original_put=simple._put;original_open=Path.open;original_unlink=Path.unlink
                original_publish=simple._publish_review_import
                fault=OSError('SYNTHETIC-'+stage)
                def put(path,value):
                    path=Path(path)
                    part={'REVIEW_STARTED.json':'marker','REVIEW.json':'receipt','REVIEW_IMPORT.pending':'import'}.get(path.name)
                    if path.parent.name=='simple-review-sessions':part='claim'
                    if part and stage.startswith(part+'-'):
                        when=stage.split('-')[1]
                        if when=='partial':path.write_bytes(b'{')
                        elif when=='full':original_put(path,value)
                        raise fault
                    return original_put(path,value)
                class Writer:
                    def __init__(self,stream):self.stream=stream
                    def __enter__(self):return self
                    def __exit__(self,*args):return self.stream.__exit__(*args)
                    def write(self,raw):
                        self.stream.write(raw[:3] if stage=='raw-partial' else raw);self.stream.flush()
                        raise fault
                    def __getattr__(self,name):return getattr(self.stream,name)
                def opened(path,*args,**kw):
                    if path==run/'REVIEW_EVIDENCE' and stage.startswith('raw-'):
                        if stage=='raw-before':raise fault
                        return Writer(original_open(path,*args,**kw))
                    return original_open(path,*args,**kw)
                def unlink(path,*args,**kw):
                    if stage=='unlink' and path==run/'REVIEW_IMPORT.pending':raise fault
                    return original_unlink(path,*args,**kw)
                def publish(root,imported):
                    original_publish(root,imported)
                    if stage=='publish-after':raise fault
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(simple,'_put',side_effect=put))
                    stack.enter_context(patch.object(Path,'open',opened))
                    stack.enter_context(patch.object(Path,'unlink',unlink))
                    stack.enter_context(patch.object(simple,'_publish_review_import',side_effect=publish))
                    if stage=='link':stack.enter_context(patch.object(simple.os,'link',side_effect=fault))
                    with self.assertRaises(OSError) as seen:simple.report(run,path)
                    self.assertIs(seen.exception,fault)
                if stage=='marker-before':
                    self.assertFalse((run/'REVIEW_STARTED.json').exists())
                    self.assertFalse((root/'simple-review-sessions').exists())
                    self.assertEqual(simple.report(run)['state'],'unmet')
                    continue # No durable admission can be claimed when create failed.
                def snapshot():
                    files=list(run.glob('REVIEW*'))+list((root/'simple-review-sessions').glob('*.json'))
                    return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
                saved=snapshot()
                self.assertTrue((run/'REVIEW_STARTED.json').exists())
                self.assertEqual(simple.report(run)['state'],'review_import_incomplete')
                with self.refusal('review_import_consumed'):simple.report(run,path)
                switched={**self.receipt(run,root),'reviewer_session_id':'different-SYNTHETIC-session'}
                other=root/'other.json';simple._put(other,switched)
                with self.refusal('review_import_consumed'):simple.report(run,other)
                self.assertEqual(snapshot(),saved)

    def test_prevalidation_is_unconsumed_and_new_marker_binds_complete_import(self):
        with directory() as root:
            run,_=self.checked(root);value=self.receipt(run,root);value['packet_sha256']='0'*64
            path=root/'bad.json';simple._put(path,value)
            with self.refusal('review_binding'):simple.report(run,path)
            self.assertFalse((run/'REVIEW_STARTED.json').exists())
            good=root/'good.json';simple._put(good,self.receipt(run,root))
            self.assertEqual(simple.report(run,good)['state'],'PASS')
            marker=simple._read(run/'REVIEW_STARTED.json');imported=simple._read(run/'REVIEW_IMPORT.json')
            self.assertEqual(marker['receipt_sha256'],simple._sha(run/'REVIEW.json'))
            self.assertEqual(imported['started_sha256'],simple._sha(run/'REVIEW_STARTED.json'))
            self.assertEqual(marker['check_sha256'],simple._sha(run/'CHECK.json'))
            saved=(run/'REVIEW_STARTED.json').read_bytes()
            self.assertEqual(simple.report(run)['state'],'PASS')
            self.assertEqual((run/'REVIEW_STARTED.json').read_bytes(),saved)

    def test_historical_complete_negative_import_without_marker_remains_valid(self):
        with directory() as root:
            run,_=self.checked(root);value=self.receipt(run,root);value['verdicts']['security']='FAIL'
            path=root/'receipt.json';simple._put(path,value);simple.report(run,path)
            # Labelled compatibility fixture matching the prior three-field import.
            (run/'REVIEW_STARTED.json').unlink()
            imported=simple._read(run/'REVIEW_IMPORT.json');imported.pop('started_sha256')
            (run/'REVIEW_IMPORT.json').write_bytes(simple._encoded(imported))
            claim=next((root/'simple-review-sessions').glob('*.json'))
            saved=simple._read(claim);saved.pop('started_sha256');saved.pop('receipt_sha256')
            claim.write_bytes(simple._encoded(saved))
            self.assertEqual(simple.report(run)['state'],'unmet')
            fresh=root/'historical-restart';simple.prepare(root/'request.json',root/'baseline',fresh,run)
            self.assertEqual(simple._read(fresh/'PREPARED.json')['predecessor']['review_import_sha256'],simple._sha(run/'REVIEW_IMPORT.json'))

    def test_failed_predecessor_bindings_refuse_exactly_before_allocation(self):
        variants=[('PREPARED.json','kind','wrong','predecessor_prepared_kind'),
                  ('PREPARED.json','run_root','/different','predecessor_prepared_binding'),
                  ('PREPARED.json','run_id','different','predecessor_check_binding'),
                  ('PREPARED.json','request_sha256','0'*64,'predecessor_request_binding'),
                  ('CHECK.json','kind','wrong','predecessor_check_kind'),
                  ('CHECK.json','run_id','different','predecessor_check_binding'),
                  ('CHECK.json','known_ended',1,'restart_predecessor_not_closed_failure'),
                  ('CHECK.json','known_ended',False,'restart_predecessor_not_closed_failure'),
                  ('CHECK.json','passed',0,'restart_predecessor_not_closed_failure'),
                  ('CHECK_STARTED.json','run_id','different','predecessor_started_binding'),
                  ('request.json','criteria',['changed'],'predecessor_request_binding')]
        for name,key,value,code in variants:
            with self.subTest(name=name,key=key,value=value),directory() as root:
                run,_=self.checked(root,fail=True)
                saved=simple._read(run/name);saved[key]=value;(run/name).write_bytes(simple._encoded(saved))
                fresh=root/'must-not-exist'
                with self.refusal(code):simple.prepare(root/'request.json',root/'baseline',fresh,run)
                self.assertFalse(fresh.exists())

    def test_start_marker_mismatch_and_missing_started_check_are_precise(self):
        with directory() as root:
            run,_=self.checked(root);value=self.receipt(run,root);value['verdicts']['security']='UNKNOWN'
            path=root/'receipt.json';simple._put(path,value);simple.report(run,path)
            marker=simple._read(run/'REVIEW_STARTED.json');marker['run_id']='different'
            (run/'REVIEW_STARTED.json').write_bytes(simple._encoded(marker))
            with self.refusal('review_started_binding'):
                simple.prepare(root/'request.json',root/'baseline',root/'bad-marker',run)
            self.assertEqual(simple.report(run)['state'],'review_import_incomplete')
        with directory() as root:
            run,_=self.checked(root,fail=True);(run/'CHECK_STARTED.json').unlink()
            with self.assertRaises(FileNotFoundError) as seen:
                simple.prepare(root/'request.json',root/'baseline',root/'missing-start',run)
            self.assertEqual(seen.exception.filename,str(run/'CHECK_STARTED.json'))

    def test_dangling_visible_marker_names_are_incomplete_and_consumed(self):
        for name in ('REVIEW_STARTED.json','REVIEW_FAILED.json','REVIEW_IMPORT.pending'):
            with self.subTest(name=name),directory() as root:
                run,_=self.checked(root);path=root/'receipt.json';simple._put(path,self.receipt(run,root))
                self.assertEqual(simple.report(run,path)['state'],'PASS')
                target=run/name
                if target.exists():target.unlink()
                target.symlink_to(root/'deliberately-absent-SYNTHETIC-marker')
                self.assertFalse(target.exists());self.assertTrue(target.is_symlink())
                self.assertEqual(simple.report(run)['state'],'review_import_incomplete')
                with self.refusal('review_import_consumed'):simple.report(run,path)

    def test_opt_in_binds_only_missing_hashes_and_preserves_original_request(self):
        with directory() as root:
            run=self.fixture(root,bind=True);prepared=simple._read(run/'PREPARED.json')
            original=simple._read(run/'request-original.json');bound=simple._read(run/'request.json')
            self.assertEqual(prepared['tool_binding']['filled'],['python','git'])
            self.assertEqual(prepared['tool_binding']['tools'],{k:bound[k] for k in ('python','git')})
            self.assertEqual((run/'request-original.json').read_bytes(),(root/'request.json').read_bytes())
            for key in original:
                if key not in ('python','git'):self.assertEqual(original[key],bound[key])
            for key in ('python','git'):
                self.assertEqual(original[key]['path'],bound[key]['path'])
                self.assertEqual(bound[key]['sha256'],simple._sha(bound[key]['path']))
            simple._prepared(run)

    def test_binding_default_strict_and_invalid_inputs_never_allocate_run(self):
        with directory() as root:
            self.fixture(root);base=self.request(root)
            variants=[('missing_default',{'path':base['python']['path']},False,'tool_identity'),
                      ('wrong',{'path':base['python']['path'],'sha256':'0'*64},True,'tool_identity'),
                      ('null',{'path':base['python']['path'],'sha256':None},True,'tool_identity'),
                      ('relative',{'path':'python'},True,'tool_identity')]
            link=root/'linked-python';link.symlink_to(base['python']['path'])
            variants.append(('linked',{'path':str(link)},True,'linked_path'))
            for name,tool,enabled,code in variants:
                with self.subTest(name=name):
                    request={**base,'python':tool};path=root/(name+'.json');simple._put(path,request)
                    destination=root/('destination-'+name)
                    with self.refusal(code):simple.prepare(path,root/'baseline',destination,bind_tools=enabled)
                    self.assertFalse(destination.exists())

    def test_binding_correct_supplied_hash_and_input_or_packet_tamper(self):
        with directory() as root:
            self.fixture(root);request=self.request(root);request['git'].pop('sha256')
            path=root/'mixed.json';simple._put(path,request);run=root/'mixed'
            simple.prepare(path,root/'baseline',run,bind_tools=True)
            self.assertEqual(simple._read(run/'PREPARED.json')['tool_binding']['filled'],['git'])
            self.assertEqual(simple._read(run/'request.json')['python'],request['python'])
            (run/'request-original.json').write_text('{}')
            with self.refusal('tool_binding_input_drift'):simple._prepared(run)
        with directory() as root:
            run,result=self.checked(root,bind=True);self.assertTrue(result['passed'])
            self.assertEqual((run/'review-packet/request-original.json').read_bytes(),(run/'request-original.json').read_bytes())
            (run/'review-packet/request-original.json').write_bytes(b'tampered')
            with self.refusal('review_packet_drift'):simple.report(run)

    def test_binding_provenance_and_bound_request_cannot_drift(self):
        for mode in ('provenance','tools','bound'):
            with self.subTest(mode=mode),directory() as root:
                run=self.fixture(root,bind=True)
                if mode=='provenance':
                    prepared=simple._read(run/'PREPARED.json');prepared['tool_binding']['filled']=['python']
                    (run/'PREPARED.json').write_bytes(simple._encoded(prepared))
                    with self.refusal('tool_binding_drift'):simple._prepared(run)
                elif mode=='tools':
                    prepared=simple._read(run/'PREPARED.json');prepared['tool_binding']['tools']['python']['sha256']='0'*64
                    (run/'PREPARED.json').write_bytes(simple._encoded(prepared))
                    with self.refusal('tool_binding_drift'):simple._prepared(run)
                else:
                    bound=simple._read(run/'request.json');bound['criteria']=['changed']
                    (run/'request.json').write_bytes(simple._encoded(bound))
                    with self.refusal('request_drift'):simple._prepared(run)
        with directory() as root:
            self.fixture(root)
            with self.refusal('tool_binding_flag'):
                simple.prepare(root/'request.json',root/'baseline',root/'bad-flag',bind_tools=1)
            self.assertFalse((root/'bad-flag').exists())

    def test_selected_tool_byte_change_refuses_revalidation(self):
        with directory() as root:
            self.fixture(root);tool=root/'ordinary-file';tool.write_bytes(b'one')
            value=self.request(root);value['git']={'path':str(tool)}
            source=root/'selected.json';simple._put(source,value);run=root/'selected-run'
            simple.prepare(source,root/'baseline',run,bind_tools=True)
            tool.write_bytes(b'two')
            with self.refusal('tool_identity'):simple._prepared(run)

    def test_public_prepare_opt_in_outputs_summary_and_default_keeps_old_layout(self):
        with directory() as root:
            run=self.fixture(root)
            self.assertNotIn('tool_binding',simple._read(run/'PREPARED.json'))
            self.assertFalse((run/'request-original.json').exists())
            value=self.request(root);value['python'].pop('sha256')
            path=root/'opt-in.json';simple._put(path,value)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code=cli.main(['prepare','--bind-tools','--request',str(path),'--baseline-dir',str(root/'baseline'),'--run-dir',str(root/'public')])
            self.assertEqual(code,0);result=json.loads(out.getvalue())
            self.assertEqual(result['state'],'prepared');self.assertEqual(result['summary']['outcome'],'prepared')
            self.assertIn('independent review',result['summary']['unmet_unknown'])
            self.assertEqual(simple._read(root/'public/PREPARED.json')['tool_binding']['filled'],['python'])

    def test_cli_summaries_do_not_infer_review_and_keep_error_exit(self):
        from unittest.mock import patch
        for command,value,outcome in [
            ('prepare',{'state':'prepared'},'prepared'),
            ('check',{'passed':True},'check_passed'),
            ('check',{'passed':False,'known_ended':False},'unmet'),
            ('report',{'state':'review_import_incomplete'},'review_import_incomplete'),
            ('report',{'state':'unmet','missing':['independent_review']},'unmet'),
            ('report',{'state':'unmet','verdicts':{'security':'FAIL'}},'unmet'),
            ('report',{'state':'PASS'},'PASS')]:
            with self.subTest(command=command,value=value):
                summary=cli._summary(command,value)
                self.assertEqual(summary['outcome'],outcome)
                self.assertEqual(set(summary),{'outcome','confirmed','unmet_unknown','next_safe_action','evidence'})
                if value.get('passed') is True:self.assertIn('human criteria and independent review',summary['unmet_unknown'])
                if value.get('known_ended') is False:self.assertIn('Stop',summary['next_safe_action'])
        with patch.object(simple,'prepare',side_effect=Blocked('tool_identity')),contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertEqual(cli.main(['prepare','--bind-tools','--request','x','--baseline-dir','y','--run-dir','z']),2)
            self.assertEqual(json.loads(out.getvalue())['reason'],'tool_identity')
        with patch.object(simple, 'check', side_effect=Blocked('collection_incomplete_or_unknown')), contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertEqual(cli.main(['check', '--run-dir', 'held']), 2)
            result = json.loads(out.getvalue())
            self.assertEqual(result['state'], 'held_unknown')
            self.assertIn('preserve', result['action'])


class RealCollectionLockTests(unittest.TestCase):
    def test_relative_dot_report_observes_collection_hold_and_real_lock(self):
        with directory() as root:
            run = root / 'run'; run.mkdir()
            simple._put(root / 'SIMPLE_ACTIVE.json', {'kind': 'synthetic-incomplete'})
            with simple._collection_lock(run):
                with self.assertRaises(Blocked):
                    with simple._collection_lock(run): pass
            previous = Path.cwd()
            try:
                os.chdir(run)
                result = simple.report('.')
            finally:
                os.chdir(previous)
            self.assertEqual(result['state'], 'held_unknown')
            self.assertEqual(result['reason'], 'collection_incomplete_or_unknown')
            self.assertTrue((root / 'SIMPLE_ACTIVE.json').is_file())

    def test_killed_checker_leaves_collection_held(self):
        with directory() as root:
            run = root / 'run'; run.mkdir()
            script = root / 'crash_fixture.py'
            script.write_text("""\
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys, time
from unittest.mock import patch
from aios.factory import simple
run = Path(sys.argv[1]); ready = Path(sys.argv[2])
prepared = {'run_id': 'synthetic', 'request_sha256': 'a' * 64}
request = {'owner_hard_stop_utc': (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat().replace('+00:00', 'Z')}
original = simple._put
def stop_after_marker(path, value):
    original(path, value)
    if Path(path).name == 'SIMPLE_ACTIVE.json':
        ready.write_text('ready')
        time.sleep(30)
with patch.object(simple, '_prepared', return_value=(run, prepared, request)), patch.object(simple, '_put', side_effect=stop_after_marker):
    simple._check_locked(run)
""", encoding='utf-8')
            env = {'PATH': os.defpath, 'PYTHONPATH': str(Path(simple.__file__).resolve().parents[2]),
                   'PYTHONDONTWRITEBYTECODE': '1'}
            if os.name == 'nt' and os.environ.get('SystemRoot'):
                env['SystemRoot'] = os.environ['SystemRoot']
            ready = root / 'ready'
            child = subprocess.Popen([sys.executable, '-B', str(script), str(run), str(ready)], env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.is_file())
                child.kill(); child.wait(timeout=5)
                self.assertIsNotNone(child.returncode)
                self.assertTrue((root / 'SIMPLE_ACTIVE.json').exists())
                with self.assertRaises(Blocked) as error:
                    simple._collection_clear(run)
                self.assertEqual(error.exception.code, 'collection_incomplete_or_unknown')
            finally:
                if child.poll() is None:
                    child.kill(); child.wait(timeout=5)
