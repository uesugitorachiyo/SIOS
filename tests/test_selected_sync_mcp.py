"""Fictional transport checks; no Hindsight submission or credential use."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from aios.selected_sync_mcp import SelectedSync, build

PATH = 'Memory/notes/selective-' + 'a' * 64 + '.md'
REV = 'b' * 64
HOST = 'device' + '@' + 'example.invalid'


class Input(io.BytesIO):
    def close(self):
        self.saved = self.getvalue()
        super().close()


class Process:
    def __init__(self, response, *, timeout=False):
        self.stdin = Input()
        self.stdout = io.BytesIO(response)
        self.returncode = 0
        self.timeout = timeout
        self.killed = False

    def wait(self, timeout):
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired('synthetic', timeout)
        return self.returncode

    def kill(self):
        self.killed = True


class SelectedSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ('ssh', 'key', 'known'):
            (self.root / name).write_text('fictional', encoding='utf-8')
        self.started = []
        self.response = b'{"state":"inspected","submitted":false}'
        self.timeout = False

        def start(cmd, **kwargs):
            proc = Process(self.response, timeout=self.timeout)
            self.started.append((cmd, proc))
            return proc
        self.sync = SelectedSync(ssh=self.root/'ssh', key=self.root/'key', known_hosts=self.root/'known',
                                 host=HOST, hostkey_alias='example.invalid', popen=start)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejects_unreviewed_submission_before_process_start(self):
        self.assertEqual(self.sync.invoke('submit', PATH, REV, False)['state'], 'invalid_request')
        self.assertEqual(self.sync.invoke('submit', '../bad', REV, True)['state'], 'invalid_request')
        self.assertEqual(self.started, [])

    def test_inspect_uses_pinned_ssh_without_remote_command(self):
        result = self.sync.invoke('inspect', PATH)
        self.assertEqual(result, {'state': 'inspected', 'automatic_retry': False, 'submitted': False})
        cmd, proc = self.started[0]
        self.assertIn('StrictHostKeyChecking=yes', cmd)
        self.assertIn('ForwardAgent=no', cmd)
        self.assertIn('GlobalKnownHostsFile=NUL', cmd)
        self.assertIn('UpdateHostKeys=no', cmd)
        self.assertEqual(cmd[-1], HOST)
        self.assertEqual(json.loads(proc.stdin.saved), {'mode': 'inspect', 'path': PATH})

    def test_unknown_and_timeout_never_retry(self):
        self.response = b'{"state":"unknown","automatic_retry":true}'
        self.assertEqual(self.sync.invoke('submit', PATH, REV, True), {'state': 'unknown', 'automatic_retry': False})
        self.assertEqual(len(self.started), 1)
        self.timeout = True
        self.assertEqual(self.sync.invoke('submit', PATH, REV, True), {'state': 'unknown', 'automatic_retry': False})
        self.assertEqual(len(self.started), 2)
        self.assertTrue(self.started[-1][1].killed)

    def test_duplicate_json_and_unexpected_response_fail_closed(self):
        self.response = b'{"state":"verified","state":"unknown"}'
        self.assertEqual(self.sync.invoke('inspect', PATH)['state'], 'unconfirmed')
        self.response = b'{"state":["verified"]}'
        self.assertEqual(self.sync.invoke('inspect', PATH)['state'], 'unconfirmed')

    def test_only_selected_tools_are_exposed(self):
        import asyncio
        tools = asyncio.run(build(self.sync).list_tools())
        self.assertEqual({t.name for t in tools}, {'selected_memory_inspect', 'selected_memory_submit',
                                                   'selected_memory_reconcile'})


if __name__ == '__main__':
    unittest.main()
