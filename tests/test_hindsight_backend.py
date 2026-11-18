import os
import tempfile
import unittest
from pathlib import Path
if os.name == 'posix':
    from aios.hindsight_backend import HindsightBackend, UncertainWrite
from tests.test_hindsight_writer_portable import Target


class FakeTarget(Target):
    def __init__(self):
        super().__init__()
        self.queries = []
    def _request(self, path, body=None):
        if path == '/memories/recall':
            self.queries.append(body)
            return {'results': []}
        return super()._request(path, body)


@unittest.skipUnless(os.name == 'posix', 'POSIX server journal')
class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        os.chmod(root, 0o700)
        lock = root / 'target.lock'
        lock.touch(mode=0o600)
        self.target = FakeTarget()
        self.backend = HindsightBackend(self.target, root, lock, ['project:test'])

    def test_store_and_read_verified_committed_text(self):
        result = self.backend.call('memory_store', 'device-a', {'request_id': 'fixture-789',
                                  'content': 'fictional record', 'project_tag': 'project:test'})
        self.assertEqual(result['status'], 'committed_exact_readback')
        self.assertEqual(result['verification_scope'], 'text_and_metadata_only')
        read = self.backend.call('memory_read_primary', 'device-a', {'path': result['source_path']})
        self.assertEqual(read['text'], 'fictional record')
        self.assertEqual(read['source_revision'], result['source_revision'])
        self.target.docs[result['document_id']]['text'] = 'changed'
        with self.assertRaises(ValueError):
            self.backend.call('memory_read_primary', 'device-a', {'path': result['source_path']})

    def test_uncertain_write_requires_reconcile_without_resending(self):
        self.target.lose_reply = True
        args = {'request_id': 'fixture-987', 'content': 'fictional record', 'project_tag': 'project:test'}
        with self.assertRaises(UncertainWrite):
            self.backend.call('memory_store', 'device-a', args)
        self.assertEqual(self.target.sends, 1)
        result = self.backend.call('memory_store_reconcile', 'device-a', {'request_id': 'fixture-987'})
        self.assertEqual(result['status'], 'reconciled_committed')
        self.assertEqual(self.target.sends, 1)

    def test_recall_uses_strict_tags_and_rejects_unconfigured_project(self):
        result = self.backend.call('memory_recall', 'device-a', {'query': 'fictional', 'project_tag': 'project:test'})
        self.assertEqual(result['coverage'], 'unknown')
        self.assertEqual(self.target.queries[0]['tags_match'], 'all_strict')
        self.assertEqual(self.target.queries[1]['tags'], ['project:test', 'origin:primary'])
        with self.assertRaises(ValueError):
            self.backend.call('memory_recall', 'device-a', {'query': 'x', 'project_tag': 'project:other'})

    def test_supersedes_requires_committed_same_project(self):
        self.backend.tags = frozenset({'project:test', 'project:other'})
        first = self.backend.call('memory_store', 'device-a', {'request_id': 'fixture-first',
                                  'content': 'first', 'project_tag': 'project:test'})
        with self.assertRaisesRegex(ValueError, 'invalid_supersedes'):
            self.backend.call('memory_store', 'device-a', {'request_id': 'fixture-other', 'content': 'other',
                              'project_tag': 'project:other', 'supersedes': first['document_id']})
        with self.assertRaisesRegex(ValueError, 'invalid_supersedes'):
            self.backend.call('memory_store', 'device-a', {'request_id': 'fixture-import', 'content': 'other',
                              'project_tag': 'project:test', 'supersedes': 'sios-' + 'a' * 64})
        second = self.backend.call('memory_store', 'device-a', {'request_id': 'fixture-second',
                                   'content': 'second', 'project_tag': 'project:test', 'supersedes': first['document_id']})
        self.assertEqual(second['status'], 'committed_exact_readback')
        with self.assertRaisesRegex(ValueError, 'invalid_supersedes'):
            self.backend.call('memory_store', 'device-a', {'request_id': 'fixture-third', 'content': 'third',
                              'project_tag': 'project:test', 'supersedes': first['document_id']})

    def test_unknown_successor_blocks_competing_branch(self):
        first = self.backend.call('memory_store', 'device-a', {'request_id': 'fixture-first',
                                  'content': 'first', 'project_tag': 'project:test'})
        self.target.lose_reply = True
        with self.assertRaises(UncertainWrite):
            self.backend.call('memory_store', 'device-a', {'request_id': 'fixture-second', 'content': 'second',
                              'project_tag': 'project:test', 'supersedes': first['document_id']})
        with self.assertRaisesRegex(ValueError, 'invalid_supersedes'):
            self.backend.call('memory_store', 'device-a', {'request_id': 'fixture-third', 'content': 'third',
                              'project_tag': 'project:test', 'supersedes': first['document_id']})
