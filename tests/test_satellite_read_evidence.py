"""Fictional source checks; no central service or credentials are used."""
import asyncio
import hashlib
import json
import threading
import unittest

from aios.client_ready_proof import CaptureReadiness
from aios.successor_guard import inspect

STEM = 'selective-' + 'a' * 64
PATH = 'Memory/notes/' + STEM + '.md'
TEXT = 'Synthetic committed source.\n'
REV = hashlib.sha256(TEXT.encode()).hexdigest()


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.fail_search = False

        def call(operation, params):
            self.calls.append(operation)
            if operation == 'health':
                return {'ok': True, 'database': 'ok'}
            if operation == 'memory_index_status':
                return {'total_files': 1, 'observed_files': [{'path': 'notes/' + STEM + '.md', 'checksum': REV}]}
            if operation == 'memory_index_search':
                rows = [] if self.fail_search else [{'read_path': PATH, 'title': STEM, 'type': 'entity',
                                                     'file_path': 'notes/' + STEM + '.md'}]
                return {'results': rows, 'total': len(rows), 'total_is_exact': True,
                        'current_page': 1, 'page_size': 10, 'has_more': False}
            if operation == 'read':
                return {'path': PATH, 'revision': REV, 'text': TEXT}
            raise AssertionError('unexpected operation: ' + operation)
        self.proof = CaptureReadiness(call)

    def test_exact_source_search_and_checksums_are_required(self):
        result = self.proof.observe(PATH, REV)
        self.assertTrue(result['ready'])
        self.assertEqual(self.calls, ['health', 'memory_index_status', 'memory_index_search', 'read', 'memory_index_status'])
        self.assertEqual(result['index_coverage'], 'unknown')
        self.fail_search = True
        self.assertFalse(self.proof.observe(PATH, REV)['ready'])

    def test_capture_receipt_never_claims_unverified_search(self):
        receipt = {'index_refresh': {'ready': True}, 'path': PATH, 'revision': REV}
        reviewed = self.proof.review(receipt)
        self.assertFalse(reviewed['index_refresh']['ready'])
        self.assertTrue(reviewed['index_refresh']['server_reported_ready'])
        self.assertTrue(receipt['index_refresh']['ready'])
        self.assertEqual(self.calls, [])

    def test_invalid_target_never_calls_server(self):
        self.assertFalse(self.proof.observe('../other.md', REV)['ready'])
        self.assertEqual(self.calls, [])

    def test_timed_out_probe_does_not_start_an_overlapping_probe(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def blocked(operation, params):
            calls.append(operation)
            entered.set()
            release.wait(2)
            return {'ok': True, 'database': 'ok'}

        proof = CaptureReadiness(blocked, timeout=0.05)
        try:
            self.assertFalse(proof.observe(PATH, REV)['ready'])
            self.assertTrue(entered.is_set())
            self.assertFalse(proof.observe(PATH, REV)['ready'])
            self.assertEqual(calls, ['health'])
        finally:
            release.set()


def source(path, supersedes):
    text = '<!-- aios-selective-memory-v1 -->\n```json\n' + json.dumps({'supersedes': supersedes}) + '\n```\n'
    return {'path': path, 'text': text, 'revision': hashlib.sha256(text.encode()).hexdigest()}


class SuccessorTests(unittest.TestCase):
    def test_declared_successor_is_verified_but_coverage_remains_unknown(self):
        next_path = 'Memory/notes/selective-' + 'b' * 64 + '.md'
        docs = {PATH: source(PATH, None), next_path: source(next_path, STEM)}

        async def read(path):
            return docs[path]

        async def search(query):
            self.assertEqual(query, STEM)
            return {'results': [{'read_path': next_path}], 'total': 1,
                    'total_is_exact': True, 'current_page': 1, 'page_size': 10, 'has_more': False}

        result = asyncio.run(inspect(PATH, read=read, search=search))
        self.assertEqual(result['successors'], [{'path': next_path, 'revision': docs[next_path]['revision']}])
        self.assertEqual(result['current_state'], 'unknown')
        self.assertEqual(result['index_coverage'], 'unknown')

    def test_bad_hash_is_not_returned_as_successor(self):
        next_path = 'Memory/notes/selective-' + 'b' * 64 + '.md'
        docs = {PATH: source(PATH, None), next_path: source(next_path, STEM)}
        docs[next_path]['revision'] = '0' * 64

        async def read(path): return docs[path]
        async def search(query):
            return {'results': [{'read_path': next_path}], 'total': 1,
                    'total_is_exact': True, 'current_page': 1, 'page_size': 10, 'has_more': False}

        result = asyncio.run(inspect(PATH, read=read, search=search))
        self.assertEqual(result['successors'], [])
        self.assertEqual(result['reason'], 'incomplete_search')


if __name__ == '__main__':
    unittest.main()
