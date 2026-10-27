"""Observe direct Memory changes without rewriting the source tree."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aios.index_reconcile import DELETED, reconcile, source_digest, source_snapshot
from aios.index_retry import pending_count, retry_one
from aios.pathutils import native_path
from aios.store import Store


class DirectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sios-direct-memory-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name).resolve() / 'data')
        self.memory = self.store.root / 'Memory'
        self.memory.mkdir()

    def due(self):
        with self.store._connection() as db:
            db.execute('UPDATE index_retry_jobs SET next_at=0')

    def test_direct_create_modify_and_delete_queue_exact_changes(self):
        path = self.memory / 'notes' / 'sample file.md'
        path.parent.mkdir()
        path.write_text('# Initial\nfirst marker', encoding='utf-8')
        relative = 'Memory/notes/sample file.md'
        old = hashlib.sha256(path.read_bytes()).hexdigest()
        with patch('aios.index_reconcile.index_snapshot', return_value={}):
            self.assertEqual(reconcile(self.store), 1)
        class Index:
            async def wait_for_files(self, expected, timeout):
                self.expected = expected
                return True
        index = Index()
        with patch('aios.index_reconcile.reconcile', return_value=0), patch('aios.index_binding.load_index', return_value=index):
            self.assertTrue(retry_one(self.store))
        self.assertEqual(index.expected, {'notes/sample file.md': old})
        self.assertEqual(pending_count(self.store), 0)
        path.write_text('# Changed\nsecond marker', encoding='utf-8')
        new = hashlib.sha256(path.read_bytes()).hexdigest()
        with patch('aios.index_reconcile.index_snapshot', return_value={relative: old}):
            self.assertEqual(reconcile(self.store), 1)
        with patch('aios.index_reconcile.reconcile', return_value=0), patch('aios.index_binding.load_index', return_value=index):
            retry_one(self.store)
        self.assertEqual(index.expected, {'notes/sample file.md': new})
        path.unlink()
        with patch('aios.index_reconcile.index_snapshot', return_value={relative: new}):
            self.assertEqual(reconcile(self.store), 1)
        with patch('aios.index_reconcile.reconcile', return_value=0), patch('aios.index_binding.load_index', return_value=index):
            retry_one(self.store)
        self.assertEqual(index.expected, {'notes/sample file.md': None})
        self.assertEqual(pending_count(self.store), 0)

    def test_scans_unicode_spaces_and_nested_paths_without_normalizing_names(self):
        folder = self.memory / ('nested ' + '\u65e5\u672c') / ('long-' + 'a' * 80)
        folder.mkdir(parents=True)
        path = folder / ('note ' + '\u8a18\u9332' + '.md')
        path.write_text('exact bytes', encoding='utf-8')
        relative = path.relative_to(self.store.root).as_posix()
        self.assertEqual(source_snapshot(self.store), {relative: hashlib.sha256(b'exact bytes').hexdigest()})
        self.assertEqual(source_digest(self.store, relative), hashlib.sha256(b'exact bytes').hexdigest())

    def test_native_long_nested_path_over_260_characters(self):
        folder = self.memory
        parts = ['segment-' + str(i) + '-' + 'x' * 60 for i in range(4)]
        for part in parts:
            folder = folder / part
        path = folder / ('note ' + '\u65e5\u672c' + '.md')
        self.assertGreater(len(str(path)), 260)
        try:
            native_path(folder).mkdir(parents=True)
            native_path(path).write_text('long path content', encoding='utf-8')
            relative = 'Memory/' + '/'.join(parts) + '/' + path.name
            self.assertEqual(source_snapshot(self.store)[relative],
                             hashlib.sha256(b'long path content').hexdigest())
            self.assertEqual(source_digest(self.store, relative),
                             hashlib.sha256(b'long path content').hexdigest())
        finally:
            if native_path(path).exists():
                native_path(path).unlink()
            for _ in parts:
                if native_path(folder).exists():
                    native_path(folder).rmdir()
                folder = folder.parent

    def test_unreadable_index_does_not_enqueue_partial_or_deleted_data(self):
        (self.memory / 'example.md').write_text('example', encoding='utf-8')
        with patch('aios.index_reconcile.index_snapshot', side_effect=RuntimeError('private error')):
            self.assertIsNone(reconcile(self.store))
        self.assertEqual(pending_count(self.store), 0)
        health = self.store.health()
        self.assertEqual(health['index_reconciliation']['status'], 'unavailable')
        self.assertNotIn('private error', str(health))

    def test_many_missing_sources_are_not_automatically_removed_from_index(self):
        indexed = {f'Memory/notes/{i}.md': 'a' * 64 for i in range(11)}
        with patch('aios.index_reconcile.index_snapshot', return_value=indexed):
            self.assertIsNone(reconcile(self.store))
        self.assertEqual(pending_count(self.store), 0)

    def test_mass_disappearance_blocks_existing_retry_from_starting_backend(self):
        with self.store._connection() as db:
            db.execute('INSERT INTO index_retry_jobs(path,revision,attempts,next_at) VALUES(?,?,0,0)',
                       ('Memory/notes/pending.md', 'a' * 64))
        indexed = {f'Memory/notes/{i}.md': 'a' * 64 for i in range(11)}
        with patch('aios.index_reconcile.index_snapshot', return_value=indexed), \
                patch('aios.index_binding.load_index') as load:
            self.assertTrue(retry_one(self.store))
        load.assert_not_called()
        self.assertEqual(pending_count(self.store), 1)
        self.assertEqual(self.store.health()['index_reconciliation']['status'], 'unavailable')

    def test_symlink_or_reparse_entry_stops_scan(self):
        target = Path(self.temp.name) / 'outside.txt'
        target.write_text('outside', encoding='utf-8')
        link = self.memory / 'link.md'
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation unavailable on this host')
        with patch('aios.index_reconcile.index_snapshot', return_value={}):
            self.assertIsNone(reconcile(self.store))
        self.assertEqual(pending_count(self.store), 0)
        self.assertEqual(target.read_text(encoding='utf-8'), 'outside')

    def test_scan_does_not_overwrite_a_more_recent_queued_revision(self):
        path = self.memory / 'sample.md'
        path.write_text('newer', encoding='utf-8')
        with self.store._connection() as db:
            db.execute('INSERT INTO index_retry_jobs(path,revision,attempts,next_at) VALUES(?,?,0,0)',
                       ('Memory/sample.md', 'f' * 64))
        with patch('aios.index_reconcile.index_snapshot', return_value={}):
            reconcile(self.store)
        with self.store._connection() as db:
            revision, = db.execute('SELECT revision FROM index_retry_jobs').fetchone()
        self.assertEqual(revision, 'f' * 64)


if __name__ == '__main__':
    unittest.main()
