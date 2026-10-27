"""Bounded index catch-up after authorized SIOS Memory writes."""
from __future__ import annotations

import asyncio
import re


def refresh_after_write(store, operation, result, *, scopes):
    """Observe exact indexed revisions without changing a committed receipt.

    The existing scoped backend process is closed on success or timeout. A
    caller without read and refresh authority does not gain either capability.
    """
    if not isinstance(result, dict):
        return result
    if operation == 'memory_capture':
        if result.get('status') not in {'captured', 'duplicate'}:
            return result
        metadata = result
    elif operation in {'memory_write', 'publish'} and isinstance(result.get('result'), dict):
        metadata = result['result']
    else:
        return result
    result = dict(result)
    if not ({'memory:write', 'memory:index_refresh'} & set(scopes)) or 'read' not in scopes:
        result['index_refresh'] = {'ready': False, 'reason': 'index_scope_unavailable'}
        return result
    expected = {}
    try:
        paths = [metadata.get('path')]
        if operation == 'publish':
            paths.append(metadata.get('journal'))
        for path in paths:
            if (not isinstance(path, str) or not path.startswith('Memory/') or
                    not path.endswith('.md')):
                raise ValueError('invalid committed Memory target')
            read = store.read(path)
            revision = read.get('revision')
            if not isinstance(revision, str) or re.fullmatch(r'[0-9a-f]{64}', revision) is None:
                raise ValueError('invalid Memory revision')
            if path == metadata.get('path') and metadata.get('revision') != revision:
                raise ValueError('committed Memory source changed')
            expected[path.removeprefix('Memory/')] = revision
        from .index_reconcile import reconcile
        if reconcile(store) is None:
            raise ValueError('Memory reconciliation unavailable')
        from .index_binding import load_index
        index = load_index(store.root)
        ready = asyncio.run(index.wait_for_files(expected, timeout=7)) is True
        if ready:
            ready = all(store.read('Memory/' + path).get('revision') == checksum
                        for path, checksum in expected.items())
        if ready:
            from .index_retry import complete
            for path, revision in expected.items():
                complete(store, 'Memory/' + path, revision)
        result['index_refresh'] = {'ready': ready, 'reason': None if ready else 'not_confirmed'}
    except Exception:
        result['index_refresh'] = {'ready': False, 'reason': 'refresh_unavailable'}
    if not result['index_refresh']['ready']:
        try:
            from .index_retry import enqueue_if_absent
            for path, revision in expected.items():
                enqueue_if_absent(store, 'Memory/' + path, revision)
            if not expected:
                enqueue_if_absent(store, metadata.get('path'), metadata.get('revision'))
        except Exception:
            result['index_refresh']['retry'] = 'unavailable'
    return result
