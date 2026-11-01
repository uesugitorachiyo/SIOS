"""Adapter unit tests; native Windows behavior requires separate host tests."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class AdapterTests(unittest.TestCase):
    def load_adapter(self):
        import aios.factory.platforms as package
        class Native:
            def __init__(self, run, **bounds):
                self.run, self.bounds, self.records, self.unknown = run, bounds, [], False
            def call(self, argv, **kwargs):
                self.last = (argv, kwargs)
                return {'known_ended': True, 'exit_code': 0}
        fake = types.ModuleType('aios.factory.platforms._windows_native')
        fake.Supervisor, fake.RunLock = Native, object
        path = Path(package.__file__).parent / '_simple_windows.py'
        spec = importlib.util.spec_from_file_location('aios.factory.platforms._adapter_test', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'aios.factory.platforms._windows_native': fake}):
            spec.loader.exec_module(module)
        return module

    def test_original_bounds_role_and_safe_environment_are_forwarded(self):
        module = self.load_adapter()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            (root / 'System32').mkdir()
            (root / 'CHECK_STARTED.json').write_bytes(b'bound')
            with patch.dict(os.environ, {'SystemRoot': str(root)}):
                obj = module.Supervisor(root, anchor=10.0, productive=20.0, hard=30.0,
                                        owner_utc='2030-01-01T00:00:00Z')
            self.assertEqual(obj.native.bounds['productive_deadline_ns'], 20_000_000_000)
            self.assertEqual(obj.native.bounds['parent_hard_deadline_ns'], 30_000_000_000)
            self.assertEqual(obj.native.bounds['cleanup_allowance_ns'], 10_000_000_000)
            record = obj.call(['tool'], cwd=root, env={'PATH': 'restricted', 'TMPDIR': 'ignored'},
                              stage='simple-check', success=False)
            self.assertTrue(record['known_ended'])
            call = obj.native.last[1]
            self.assertEqual(call['role'], 'simple-check')
            self.assertFalse(call['success'])
            self.assertEqual(call['env']['SystemRoot'], str(root))
            self.assertEqual(call['env']['PATH'], str(root / 'System32'))
            self.assertEqual(call['env']['TEMP'], str(root / 'tmp'))
            self.assertNotIn('TMPDIR', call['env'])

    def test_unknown_termination_is_not_converted_to_success(self):
        module = self.load_adapter()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            (root / 'System32').mkdir()
            (root / 'CHECK_STARTED.json').write_bytes(b'bound')
            with patch.dict(os.environ, {'SystemRoot': str(root)}):
                obj = module.Supervisor(root, anchor=10., productive=20., hard=30.,
                                        owner_utc='2030-01-01T00:00:00Z')
            obj.native.unknown = True
            self.assertTrue(obj.unknown)
