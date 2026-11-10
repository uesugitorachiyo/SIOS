import hashlib
import unittest
from aios.literal_search import verify_literal

class LiteralSearchTests(unittest.TestCase):
    def test_verified_and_unknown_coverage(self):
        text = '架空の記録 Straße'
        path = 'Memory/notes/架空.md'
        rev = hashlib.sha256(text.encode()).hexdigest()
        result = verify_literal('STRASSE', lambda _: [{'path': path, 'revision': rev}],
                                lambda p: {'path': p, 'revision': rev, 'text': text})
        self.assertEqual(result['verified'][0]['text'], text)
        self.assertTrue(result['unknown_coverage'])

    def test_rejects_forged_revision_and_unsafe_path(self):
        path, bad = 'Memory/notes/test.md', '0' * 64
        result = verify_literal('test', lambda _: [{'path': path, 'revision': bad},
            {'path': 'Memory/notes/../secret.md', 'revision': bad}],
            lambda p: {'path': p, 'revision': bad, 'text': 'test'})
        self.assertFalse(result['verified'])
        self.assertEqual(len(result['malformed']), 2)

    def test_stale_changed_path_missing_literal_and_read_failure(self):
        text, path = 'fiction literal', 'Memory/notes/test.md'
        rev = hashlib.sha256(text.encode()).hexdigest()
        source = {'path': path, 'revision': rev, 'text': text}
        run = lambda hits, reader: verify_literal('literal', lambda _: hits, reader)
        self.assertEqual(len(run([{'path': path, 'revision': '0'*64}], lambda _: source)['stale']), 1)
        self.assertEqual(len(run([{'path': path, 'revision': rev}], lambda _: {**source, 'path':'other'})['malformed']), 1)
        self.assertEqual(len(run([{'path': path, 'revision': rev}], lambda _: (_ for _ in ()).throw(OSError('secret')))['unreadable']), 1)
        self.assertEqual(len(verify_literal('absent', lambda _: [{'path': path, 'revision': rev}], lambda _: source)['stale']), 1)

    def test_bounds_and_malformed_hit(self):
        path, text = 'Memory/notes/test.md', 'fiction'
        rev = hashlib.sha256(text.encode()).hexdigest()
        result = verify_literal(text, lambda _: [None, {'path': path, 'revision': rev}], lambda p: {}, max_hits=1)
        self.assertEqual(len(result['malformed']), 1)
        self.assertTrue(result['results_limited'])
        for limit in (0, 21, True):
            with self.assertRaises(ValueError):
                verify_literal(text, lambda _: [], lambda _: {}, max_hits=limit)

if __name__ == '__main__':
    unittest.main()
