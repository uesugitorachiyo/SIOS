import hashlib
import unittest

from aios.hindsight_evidence import verify_hindsight_sources


class HindsightEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.path = 'Memory/notes/example.md'
        self.text = '# Fictional original\n'
        self.rev = hashlib.sha256(self.text.encode()).hexdigest()
        self.item = {'path': self.path, 'revision': self.rev}

    def test_verified_original_and_unknown_coverage(self):
        result = verify_hindsight_sources([self.item], lambda p: {
            'path': p, 'revision': self.rev, 'text': self.text})
        self.assertEqual(result['results'][0]['text'], self.text)
        self.assertEqual(result['results'][0]['status'], 'verified')
        self.assertEqual(result['coverage'], 'unknown')

    def test_changed_source_is_not_presented_as_matching(self):
        result = verify_hindsight_sources([self.item], lambda p: {
            'path': p, 'revision': hashlib.sha256(b'new').hexdigest(), 'text': 'new'})
        self.assertEqual(result['results'][0]['status'], 'revision_changed')
        self.assertNotIn('text', result['results'][0])

    def test_invalid_batch_makes_no_reads(self):
        calls = []
        with self.assertRaises(ValueError):
            verify_hindsight_sources([self.item, {'path': 'Memory/../secret.md', 'revision': self.rev}], calls.append)
        self.assertEqual(calls, [])

    def test_corrupt_source_and_read_errors_fail_closed(self):
        for reader in [lambda p: {'path': p, 'revision': self.rev, 'text': 'tampered'},
                       lambda p: (_ for _ in ()).throw(PermissionError('secret'))]:
            result = verify_hindsight_sources([self.item], reader)
            self.assertEqual(result['results'][0], {'path': self.path, 'status': 'unconfirmed'})

    def test_limits_and_unexpected_fields(self):
        for items in [[], [self.item] * 11, [{**self.item, 'text': 'untrusted'}]]:
            with self.assertRaises(ValueError):
                verify_hindsight_sources(items, lambda p: self.fail('unexpected read'))
