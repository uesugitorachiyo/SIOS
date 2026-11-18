import os
import tempfile
import unittest
from pathlib import Path
if os.name == 'posix':
    from aios.hindsight_journal import PrimaryJournal
    from aios.hindsight_writer import write_primary, reconcile


class Target:
    def __init__(self):
        self.docs = {}
        self.sends = 0
        self.lose_reply = False
    def get(self, ident):
        return self.docs.get(ident)
    def _request(self, path, body=None):
        if path == '/config':
            return {'config': {'retain_strategies': {'sios_primary_chunks': {'retain_extraction_mode': 'chunks'}}}}
        self.sends += 1
        item = body['items'][0]
        self.docs[item['document_id']] = {'text': item['content'], 'metadata': item['metadata']}
        if self.lose_reply:
            raise TimeoutError('simulated lost response')
        return {}


@unittest.skipUnless(os.name == 'posix', 'POSIX server journal')
class WriterTests(unittest.TestCase):
    def test_journal_commit_failure_after_send_remains_unknown(self):
        class Journal:
            def get(self, request_id): return None
            def insert_unknown(self, row): pass
            def mark_committed(self, request_id): raise OSError('fixture')
        target = Target()
        result = write_primary(target, Journal(), request_id='fixture-commit', content='fictional',
                               actor='fixture', supersedes=None, project_tag='project:test', entities=None)
        self.assertEqual(result['status'], 'unknown_no_retry')
        self.assertEqual(target.sends, 1)

    def test_lost_reply_is_reconciled_without_a_second_send(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            os.chmod(root, 0o700)
            lock = root / 'target.lock'
            lock.touch(mode=0o600)
            target = Target()
            target.lose_reply = True
            with PrimaryJournal(root, target_lock=lock) as journal:
                result = write_primary(target, journal, request_id='fixture-123', content='fictional',
                                       actor='fixture', supersedes=None, project_tag='project:test', entities=None)
                self.assertEqual(result['status'], 'unknown_no_retry')
                self.assertEqual(target.sends, 1)
                self.assertEqual(reconcile(target, journal, request_id='fixture-123', actor='fixture')['status'],
                                 'reconciled_committed')
                self.assertEqual(target.sends, 1)
                with self.assertRaises(PermissionError):
                    reconcile(target, journal, request_id='fixture-123', actor='different')
                with self.assertRaises(ValueError):
                    write_primary(target, journal, request_id='fixture-123', content='different',
                                  actor='fixture', supersedes=None, project_tag='project:test', entities=None)
                self.assertEqual(target.sends, 1)

    def test_failed_strategy_preflight_never_sends(self):
        class MissingStrategy(Target):
            def _request(self, path, body=None):
                if path == '/config':
                    return {'config': {}}
                return super()._request(path, body)
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            os.chmod(root, 0o700)
            lock = root / 'target.lock'
            lock.touch(mode=0o600)
            target = MissingStrategy()
            with PrimaryJournal(root, target_lock=lock) as journal:
                result = write_primary(target, journal, request_id='fixture-456', content='fictional',
                                       actor='fixture', supersedes=None, project_tag='project:test', entities=None)
                self.assertEqual(result['reason'], 'retain_strategy_unconfigured')
                self.assertIsNone(journal.get('fixture-456'))
                self.assertEqual(target.sends, 0)
