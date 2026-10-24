"""Isolated server-backed contract for an oversized Mission log snapshot."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from aios.client import RemoteError
from aios.memory_mcp import MemoryGateway
from aios.service import dispatch
from aios.store import Store, ValidationError
from mcp.server.mcpserver.exceptions import ToolError


class ReadScopeClient:
    """Use the production dispatcher and Store without transport or credentials."""

    def __init__(self, store):
        self.store = store

    def call(self, operation, params):
        try:
            return dispatch(self.store, operation, params, actor='isolated-reader',
                            scopes={'read'})
        except ValidationError:
            # This is the HTTP adapter's documented mapping for validation errors.
            raise RemoteError('invalid_request') from None


class OversizedMissionLogTests(unittest.TestCase):
    def test_revision_bound_snapshot_read(self):
        with tempfile.TemporaryDirectory(prefix='sios-memory-oversized-log-') as root:
            store = Store(Path(root) / 'data')
            gateway = MemoryGateway(ReadScopeClient(store))
            folder = store.root / 'Missions' / 'isolated-gateway'
            evidence = folder / 'evidence' / 'log-read-snapshot-example'
            evidence.mkdir(parents=True)

            log = folder / 'log.md'
            source = b'# Log\n' + b'x' * 2_000_000
            log.write_bytes(source)
            source_sha = hashlib.sha256(source).hexdigest()
            prefix = 'Missions/isolated-gateway/evidence/log-read-snapshot-example/'
            index_path = prefix + 'index.md'
            parts = [source[:900_000], source[900_000:1_800_000], source[1_800_000:]]
            rows = []
            for number, body in enumerate(parts, 1):
                name = f'part-{number:03}.md'
                (evidence / name).write_bytes(body)
                rows.append(f'| [{name}]({name}) | {len(body)} | `{hashlib.sha256(body).hexdigest()}` |')
            (evidence / 'index.md').write_text(
                '# Snapshot\n'
                f'Source path: `Missions/isolated-gateway/log.md`\n'
                f'Source SHA-256: `{source_sha}`\n'
                f'Source bytes: {len(source)}\n'
                'Parts: 3. Concatenate their UTF-8 bytes in order.\n\n'
                '| Part | Bytes | SHA-256 |\n|---|---:|---|\n' + '\n'.join(rows) + '\n',
                encoding='utf-8')

            with self.assertRaisesRegex(ToolError, 'invalid_request'):
                gateway.invoke('read', {'relative': 'Missions/isolated-gateway/log.md'})
            head = gateway.invoke('read_revision', {'relative': 'Missions/isolated-gateway/log.md'})
            self.assertEqual(head['bytes'], len(source))
            self.assertEqual(head['revision'], source_sha)
            self.assertIs(head['content_returned'], False)
            self.assertNotIn('text', head)

            index = gateway.invoke('read', {'relative': index_path})
            self.assertIn(f'Source SHA-256: `{head["revision"]}`', index['text'])
            recovered = b''.join(gateway.invoke('read', {'relative': prefix + f'part-{number:03}.md'})
                                 ['text'].encode('utf-8') for number in range(1, 4))
            self.assertEqual(hashlib.sha256(recovered).hexdigest(), head['revision'])
            self.assertEqual(recovered, source)

            log.write_bytes(source + b'y')
            changed = gateway.invoke('read_revision', {'relative': 'Missions/isolated-gateway/log.md'})
            self.assertNotEqual(changed['revision'], head['revision'])
            self.assertNotIn(f'Source SHA-256: `{changed["revision"]}`', index['text'])


if __name__ == '__main__':
    unittest.main()
