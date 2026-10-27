"""Capture stays committed and scoped when automatic index refresh fails."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from aios.service import dispatch
from aios.basic_memory import BasicMemoryIndex, BasicMemoryError
from aios.store import Store


class CaptureIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sios-capture-index-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name).resolve() / 'data')
        self.request = {
            'retain': True, 'kind': 'delta', 'request_id': 'synthetic-auto-index',
            'meaning_id': 'synthetic-auto-index-marker', 'claim': 'Synthetic record.',
            'source_type': 'observed', 'source_actor': 'fixture-client',
            'source_reference': 'https://example.com/synthetic', 'project': 'sios-test',
            'conditions': ['isolated fixture'], 'outcome': 'test',
            'evidence_status': 'observed', 'recorded_at': '2026-10-27T00:00:00+00:00',
            'retrieval_cue': 'synthetic-auto-index-marker',
        }

    def capture(self, scopes):
        return dispatch(self.store, 'memory_capture', {'request_json': json.dumps(self.request)},
                        actor='fixture', scopes=scopes)

    def test_committed_capture_refreshes_and_retry_is_safe(self):
        class Index:
            async def wait_for_files(self, expected, timeout):
                self.expected = expected
                return True
        index = Index()
        with patch('aios.index_binding.load_index', return_value=index) as refresh:
            first = self.capture({'read', 'memory:write'})
            self.assertEqual(first['status'], 'captured')
            self.assertEqual(first['index_refresh'], {'ready': True, 'reason': None})
            self.assertEqual(index.expected, {first['path'].removeprefix('Memory/'): first['revision']})
            self.assertEqual(self.store.read(first['path'])['revision'], first['revision'])
            second = self.capture({'read', 'memory:write'})
        self.assertEqual(second['status'], 'duplicate')
        self.assertEqual(second['revision'], first['revision'])
        self.assertEqual(refresh.call_count, 2)

    def test_failed_refresh_does_not_undo_or_expose_exception(self):
        with patch('aios.index_binding.load_index', side_effect=RuntimeError('private detail')):
            result = self.capture({'read', 'memory:write'})
        self.assertEqual(result['status'], 'captured')
        self.assertEqual(result['index_refresh'], {'ready': False, 'reason': 'refresh_unavailable'})
        self.assertEqual(self.store.read(result['path'])['revision'], result['revision'])
        self.assertNotIn('private detail', str(result))

    def test_source_change_during_indexing_is_not_reported_ready(self):
        store = self.store
        class Index:
            async def wait_for_files(self, expected, timeout):
                relative, revision = next(iter(expected.items()))
                identifier = Path(relative).stem
                store.write_memory(identifier, '# Changed later', 'later-write',
                                   expected_revision=revision, actor='fixture')
                return True
        with patch('aios.index_binding.load_index', return_value=Index()):
            result = self.capture({'read', 'memory:write'})
        self.assertEqual(result['status'], 'captured')
        self.assertEqual(result['index_refresh'], {'ready': False, 'reason': 'not_confirmed'})

    def test_capture_only_scope_does_not_gain_index_permission(self):
        with patch('aios.index_binding.load_index') as refresh:
            result = self.capture({'read', 'memory:capture'})
        self.assertEqual(result['status'], 'captured')
        self.assertEqual(result['index_refresh']['reason'], 'index_scope_unavailable')
        refresh.assert_not_called()

    def test_nonretained_request_does_not_touch_index(self):
        with patch('aios.index_binding.load_index') as refresh:
            result = dispatch(self.store, 'memory_capture', {'request_json': '{"retain":false}'},
                              actor='fixture', scopes={'read', 'memory:write'})
        self.assertEqual(result['status'], 'skipped')
        refresh.assert_not_called()

    def test_raw_write_and_publication_refresh_exact_committed_targets(self):
        class Index:
            calls = []
            async def wait_for_files(self, expected, timeout):
                self.calls.append(dict(expected))
                return True
        index = Index()
        scopes = {'read', 'memory:write'}
        with patch('aios.index_binding.load_index', return_value=index):
            note = dispatch(self.store, 'memory_write', {'identifier': 'sample',
                'content': '# Sample\nTest content', 'request_id': 'note-1', 'create': True},
                actor='fixture', scopes=scopes)
            report = dispatch(self.store, 'publish', {'routine': 'test', 'run_id': 'sample-run',
                'report': '# Synthetic report', 'summary': 'Synthetic', 'status': 'completed',
                'request_id': 'report-1'}, actor='fixture', scopes=scopes)
        self.assertTrue(note['index_refresh']['ready'])
        self.assertEqual(index.calls[0], {'notes/sample.md': note['result']['revision']})
        self.assertTrue(report['index_refresh']['ready'])
        expected_paths = {report['result']['report'].removeprefix('Memory/'),
                          report['result']['journal'].removeprefix('Memory/')}
        self.assertEqual(set(index.calls[1]), expected_paths)

    def test_index_confirmation_uses_exact_project_path_and_checksum(self):
        root = Path(self.temp.name).resolve()
        index_dir = root / 'index'
        index_dir.mkdir()
        (self.store.root / 'Memory').mkdir()
        index = BasicMemoryIndex(sys.executable, index_dir, self.store.root / 'Memory', 'sios-test')
        with sqlite3.connect(index_dir / 'memory.db') as db:
            db.execute('CREATE TABLE project(id INTEGER, name TEXT, path TEXT)')
            db.execute('CREATE TABLE entity(project_id INTEGER, file_path TEXT, checksum TEXT)')
            db.execute('INSERT INTO project VALUES(?, ?, ?)', (1, 'sios-test', str(index.memory_root)))
            db.execute('INSERT INTO entity VALUES(?, ?, ?)', (1, 'notes/sample.md', 'a' * 64))
        self.assertTrue(index._indexed_checksums_match({'notes/sample.md': 'a' * 64}))
        self.assertFalse(index._indexed_checksums_match({'notes/sample.md': 'b' * 64}))
        self.assertFalse(index._indexed_checksums_match({'notes/other.md': 'a' * 64}))
        with sqlite3.connect(index_dir / 'memory.db') as db:
            db.execute('UPDATE project SET name=?', ('other-project',))
        with self.assertRaises(BasicMemoryError):
            index._indexed_checksums_match({'notes/sample.md': 'a' * 64})


if __name__ == '__main__':
    unittest.main()
