"""Retry survives failures, restarts and concurrent Memory changes."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from aios.index_retry import RetryWorker, pending_count, retry_one
from aios.service import dispatch
from aios.store import Store


class IndexRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sios-index-retry-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'data'
        self.store = Store(self.root)
        self.scopes = {'read', 'memory:write'}

    def write(self, content='first', request='write-1', **extra):
        return dispatch(self.store, 'memory_write', {'identifier': 'sample',
            'content': content, 'request_id': request, **extra}, actor='fixture', scopes=self.scopes)

    def due(self):
        with self.store._connection() as db:
            db.execute('UPDATE index_retry_jobs SET next_at=0')

    def test_failed_write_refresh_is_queued_and_worker_recovers_after_restart(self):
        with patch('aios.index_binding.load_index', side_effect=RuntimeError('unavailable')):
            written = self.write(create=True)
        self.assertFalse(written['index_refresh']['ready'])
        self.assertEqual(pending_count(self.store), 1)
        self.assertEqual(self.store.read('Memory/notes/sample.md')['text'], 'first')
        reopened = Store(self.root)
        self.assertEqual(pending_count(reopened), 1)
        self.due()
        class Index:
            async def wait_for_files(self, expected, timeout):
                return expected == {'notes/sample.md': written['result']['revision']}
        with patch('aios.index_binding.load_index', return_value=Index()):
            worker = RetryWorker(reopened).start()
            try:
                deadline = time.monotonic() + 8
                while pending_count(reopened) and time.monotonic() < deadline:
                    time.sleep(0.1)
            finally:
                worker.stop()
        self.assertEqual(pending_count(reopened), 0)

    def test_retry_failure_persists_backoff_and_later_succeeds(self):
        with patch('aios.index_binding.load_index', side_effect=RuntimeError('offline')):
            self.write(create=True)
        self.due()
        with patch('aios.index_binding.load_index', side_effect=RuntimeError('offline')):
            self.assertTrue(retry_one(self.store))
        with self.store._connection() as db:
            attempts, next_at, reason = db.execute('SELECT attempts,next_at,last_error FROM index_retry_jobs').fetchone()
        self.assertEqual(attempts, 1)
        self.assertGreater(next_at, time.time())
        self.assertEqual(reason, 'not_confirmed')
        self.assertFalse(retry_one(self.store))
        self.due()
        class Index:
            async def wait_for_files(self, expected, timeout):
                return True
        with patch('aios.index_binding.load_index', return_value=Index()):
            self.assertTrue(retry_one(self.store))
        self.assertEqual(pending_count(self.store), 0)

    def test_idempotent_write_retry_queues_refresh_if_backend_is_down(self):
        class Index:
            async def wait_for_files(self, expected, timeout):
                return True
        with patch('aios.index_binding.load_index', return_value=Index()):
            first = self.write(create=True)
        self.assertTrue(first['index_refresh']['ready'])
        self.assertEqual(pending_count(self.store), 0)
        with patch('aios.index_binding.load_index', side_effect=RuntimeError('offline')):
            repeat = self.write(create=True)
        self.assertFalse(repeat['index_refresh']['ready'])
        self.assertEqual(pending_count(self.store), 1)

    def test_new_revision_cannot_be_acknowledged_by_old_retry(self):
        with patch('aios.index_binding.load_index', side_effect=RuntimeError('offline')):
            first = self.write(create=True)
            second = self.write('second', 'write-2', expected_revision=first['result']['revision'])
        self.due()
        class Index:
            async def wait_for_files(self, expected, timeout):
                self.expected = expected
                return True
        idx = Index()
        with patch('aios.index_binding.load_index', return_value=idx):
            retry_one(self.store)
        self.assertEqual(idx.expected, {'notes/sample.md': second['result']['revision']})
        self.assertEqual(pending_count(self.store), 0)

    def test_concurrent_rewrite_is_not_removed_by_inflight_old_retry(self):
        from aios.index_retry import begin, end
        with patch('aios.index_binding.load_index', side_effect=RuntimeError('offline')):
            first = self.write(create=True)
        self.due()
        store = self.store
        class Index:
            async def wait_for_files(self, expected, timeout):
                token = begin({'read', 'memory:write'})
                try:
                    store.write_memory('sample', 'newer', 'write-concurrent',
                                       expected_revision=first['result']['revision'])
                finally:
                    end(token)
                return True
        with patch('aios.index_binding.load_index', return_value=Index()):
            retry_one(self.store)
        with self.store._connection() as db:
            revision, = db.execute('SELECT revision FROM index_retry_jobs').fetchone()
        self.assertEqual(revision, self.store.read('Memory/notes/sample.md')['revision'])
        self.assertEqual(pending_count(self.store), 1)

    def test_pending_intent_survives_crash_before_source_replace(self):
        with patch.object(self.store, '_atomic_replace', side_effect=SystemExit('synthetic crash')):
            with self.assertRaises(SystemExit):
                self.write(create=True)
        reopened = Store(self.root)
        self.assertEqual(pending_count(reopened), 1)
        self.assertEqual(reopened.read('Memory/notes/sample.md')['text'], 'first')

    def test_nonprivileged_capture_does_not_enqueue_index_work(self):
        from aios.index_retry import begin, end
        token = begin({'read', 'memory:capture'})
        try:
            self.store.write_memory('sample', 'first', 'write-capture', create=True)
        finally:
            end(token)
        self.assertEqual(pending_count(self.store), 0)


if __name__ == '__main__':
    unittest.main()
