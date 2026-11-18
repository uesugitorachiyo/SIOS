import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from aios.hindsight_admin_recovery import RecoveryGrant, RecoveryRefused, digest, recover
from aios.hindsight_journal import PrimaryJournal


class Client:
    def __init__(self):
        self.document = None
        self.sends = 0

    def get(self, document_id):
        return self.document

    def _request(self, path, payload=None):
        if path == '/config':
            return {'config': {'retain_strategies': {'sios_primary_chunks': {'retain_extraction_mode': 'chunks'}}}}
        self.sends += 1
        item = payload['items'][0]
        self.document = {'text': item['content'], 'metadata': item['metadata']}
        return {}


class RecoveryTests(unittest.TestCase):
    def test_recovery_requires_proof_and_reconciles_without_second_send(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / 'target.lock'
            lock.touch(mode=0o600)
            request = 'fixture-recovery-one'
            document = 'primary-' + digest(request.encode())
            content = 'Synthetic recovery fixture.'
            metadata = {'request_id': request, 'source_kind': 'sios-hindsight-primary',
                        'source_actor': 'fixture', 'content_sha256': hashlib.sha256(content.encode()).hexdigest()}
            payload = json.dumps({'async': False, 'items': [{'content': content, 'document_id': document,
                                 'metadata': metadata, 'strategy': 'sios_primary_chunks'}]}).encode()
            client = Client()
            target = type('Target', (), {'endpoint': 'http://127.0.0.1:5000', 'journal_root': root,
                          'lock_wait': 1, 'write_timeout': 1, 'client': lambda self, timeout: client})()
            evidence = {'safe': False}
            grant = RecoveryGrant(request, 'fixture', target.endpoint, 'fixture-bank', root, payload,
                                  digest(payload), True, frozenset({'old'}), frozenset({'old'}),
                                  lambda: True, lambda: evidence['safe'])
            class Authority:
                @contextmanager
                def hold(self, target, operator, request_id):
                    yield grant
            @contextmanager
            def locked(path, wait):
                with PrimaryJournal(path, target_lock=lock) as journal:
                    yield journal
            with locked(root, 1) as journal:
                journal.insert_unknown({'request_id': request, 'document_id': document,
                                        'metadata': metadata, 'state': 'unknown'})
            with self.assertRaisesRegex(RecoveryRefused, 'prior_effects_unverified'):
                recover(target, 'fixture', request, authority=Authority(), locked=locked, bank='fixture-bank')
            self.assertEqual(client.sends, 0)
            evidence['safe'] = True
            result = recover(target, 'fixture', request, authority=Authority(), locked=locked, bank='fixture-bank')
            self.assertEqual(result['status'], 'reconciled_committed')
            again = recover(target, 'fixture', request, authority=Authority(), locked=locked, bank='fixture-bank')
            self.assertEqual(again['status'], 'already_committed')
            self.assertEqual(client.sends, 1)


if __name__ == '__main__':
    unittest.main()
