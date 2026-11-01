import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aios.evidence_search import search_evidence
from aios.store import MAX_BYTES, Store, ValidationError


class MissionHistoryTests(unittest.TestCase):
    def test_search_reaches_large_history_without_changing_read_limit(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as root:
            folder = Path(root) / 'Missions/example'
            folder.mkdir(parents=True)
            content = b'x' * MAX_BYTES + b'\nunique-history-marker\n'
            (folder / 'log.md').write_bytes(content)
            (folder / 'mission.md').write_text('another document', encoding='utf-8')
            store = Store(Path(root))
            hits = store.search('missions', 'unique-history-marker')
            self.assertEqual(hits[0]['path'], 'Missions/example/log.md')
            self.assertEqual(hits[0]['revision'], hashlib.sha256(content).hexdigest())
            with self.assertRaises(ValidationError):
                store.read('Missions/example/log.md')
            with patch('aios.store.MAX_MISSION_SEARCH_BYTES', 100):
                with self.assertRaises(ValidationError):
                    store.search('missions', 'unique-history-marker')
            with patch('aios.store.MAX_MISSION_SEARCH_TOTAL_BYTES', 100):
                with self.assertRaises(ValidationError):
                    store.search('missions', 'unique-history-marker')

    def test_verified_snapshot_reconstructs_original_and_detects_drift(self):
        path = 'Missions/example/log.md'
        chunks = ['first part\n', 'last part with target\n']
        content = ''.join(chunks)
        revision = hashlib.sha256(content.encode()).hexdigest()
        root = 'Missions/example/evidence/log-read-snapshot-' + revision[:12]
        rows = []
        documents = {}
        for number, part in enumerate(chunks, 1):
            name = f'part-{number:03d}.md'
            digest = hashlib.sha256(part.encode()).hexdigest()
            rows.append(f'| [{name}]({name}) | {len(part.encode())} | `{digest}` |')
            documents[root + '/' + name] = part
        index = f'Source path: `{path}`\nSource SHA-256: `{revision}`\n' + '\n'.join(rows) + '\n'
        documents[root + '/index.md'] = index
        changed = False
        def call(op, params):
            if op == 'memory_index_search':
                return {'results': [], 'has_more': False}
            if op == 'search':
                return [{'path': path, 'revision': revision}] if params['store'] == 'missions' else []
            if op == 'read_revision':
                return {'path': path, 'revision': 'f' * 64 if changed else revision, 'bytes': len(content.encode())}
            if op == 'read':
                target = params['relative']
                if target == path:
                    raise ValueError('normal read size limit')
                text = documents[target]
                return {'path': target, 'text': text, 'revision': hashlib.sha256(text.encode()).hexdigest()}
            raise AssertionError(op)
        result = search_evidence(call, 'target')
        self.assertEqual(result['results'][0]['text'], content)
        self.assertEqual(result['results'][0]['revision'], revision)
        self.assertEqual(result['results'][0]['read_method'], 'verified_snapshot')
        documents[root + '/part-002.md'] = 'tampered'
        self.assertEqual(search_evidence(call, 'target')['results'], [])
        documents[root + '/part-002.md'] = chunks[1]
        changed = True
        self.assertEqual(search_evidence(call, 'target')['results'], [])

        part_path = root + '/part-002.md'
        def snapshot_call(op, params):
            if op == 'search' and params['store'] == 'missions':
                return [{'path': part_path, 'revision': hashlib.sha256(chunks[1].encode()).hexdigest()}]
            return call(op, params)
        self.assertEqual(search_evidence(snapshot_call, 'target')['results'][0]['snapshot_status'], 'index references older log; full snapshot unchecked')
        changed = False
        self.assertEqual(search_evidence(snapshot_call, 'target')['results'][0]['snapshot_status'], 'index references current log; full snapshot unchecked')
        documents[part_path] = 'tampered part'
        self.assertEqual(search_evidence(snapshot_call, 'target')['results'][0]['snapshot_status'], 'integrity mismatch')


if __name__ == '__main__':
    unittest.main()
