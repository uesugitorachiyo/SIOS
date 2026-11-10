import hashlib
import unittest

from aios.search_resolver import resolve_search_verified


class GeneralSearchTests(unittest.TestCase):
    def test_ordinary_note_full_read_and_revision(self):
        path = 'Memory/notes/架空/example note.md'
        text = '架空の保存内容'
        revision = hashlib.sha256(text.encode()).hexdigest()
        calls = []
        def read(p):
            calls.append(p)
            return {'path': p, 'revision': revision, 'text': text}
        result = resolve_search_verified('fiction', lambda _: {'results': [{'read_path': path, 'title': 'example'}]}, read)
        self.assertEqual(calls, [path])
        self.assertEqual(result['results'][0]['status'], 'verified_indexed_source')
        self.assertEqual(result['results'][0]['revision'], revision)

    def test_invalid_path_and_bad_digest_never_verified(self):
        hits = [{'read_path': 'Memory/notes/../private.md'}, {'read_path': 'Memory/notes/ordinary.md'}]
        calls = []
        def read(p):
            calls.append(p)
            return {'path': p, 'revision': '0' * 64, 'text': 'fiction'}
        result = resolve_search_verified('fiction', lambda _: {'results': hits}, read)
        self.assertEqual(calls, ['Memory/notes/ordinary.md'])
        self.assertEqual(result['results'], [])
        self.assertEqual({w['type'] for w in result['warnings']}, {'invalid_hit_path', 'indexed_source_invalid'})

    def test_budget_and_duplicates(self):
        hit = {'read_path': 'Memory/notes/a.md'}
        text = 'fixture'
        calls = []
        def read(p):
            calls.append(p)
            return {'path': p, 'revision': hashlib.sha256(text.encode()).hexdigest(), 'text': text}
        result = resolve_search_verified('x', lambda _: {'results': [hit, hit]}, read, max_reads=1)
        self.assertEqual(calls, [hit['read_path']])
        self.assertEqual(len(result['results']), 1)
        calls.clear()
        result = resolve_search_verified('x', lambda _: {'results': [hit]}, read, max_reads=0)
        self.assertEqual(calls, [])
        self.assertEqual(result['results'], [])


if __name__ == '__main__':
    unittest.main()
