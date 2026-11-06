"""macOS source-protecting launcher for the pinned Basic Memory 1.0 index.

This launcher runs only against an explicitly configured index and refuses an
unknown upstream indexer. The child's sandbox blocks network and writes to the
Memory source tree. It does not turn other processes into read-only clients.
"""
from __future__ import annotations

import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import stat
import sys

_INDEXER_SHA256 = '94ee965df924013f486b8ac1006cc032a06ced4803b97715b155bdc429c8b620'


def _memory_root() -> Path:
    config = Path(os.environ.get('BASIC_MEMORY_CONFIG_DIR', ''))
    if not config.is_absolute() or config.is_symlink():
        raise RuntimeError('invalid index configuration directory')
    marker = config / '.aios-basic-memory-index.json'
    node = marker.lstat()
    if not stat.S_ISREG(node.st_mode) or node.st_size > 8192:
        raise RuntimeError('invalid index marker')
    record = json.loads(marker.read_text(encoding='utf-8'))
    root = Path(record['memory_root'])
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise RuntimeError('invalid Memory root')
    return root.resolve(strict=True)


def _check_upstream() -> None:
    if metadata.version('basic-memory') != '1.0.0':
        raise RuntimeError('unsupported Basic Memory version')
    # Inspect distribution metadata without importing Basic Memory before the
    # network- and source-write-denying sandbox is active.
    path = Path(metadata.distribution('basic-memory').locate_file(
        'basic_memory/indexing/batch_indexer.py'))
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != _INDEXER_SHA256:
        raise RuntimeError('unsupported Basic Memory indexer build')


def _run_indexer() -> None:
    from basic_memory.indexing.batch_indexer import BatchIndexer, _PreparedMarkdownFile

    async def normalize(self, prepared, reserved_permalinks, *, existing_permalink=None):
        permalink = await self._resolve_batch_permalink(
            prepared, reserved_permalinks, existing_permalink=existing_permalink)
        prepared.markdown.frontmatter.metadata['permalink'] = permalink
        return _PreparedMarkdownFile(file=prepared.file, content=prepared.content,
            final_checksum=prepared.final_checksum, markdown=prepared.markdown,
            frontmatter_state=prepared.frontmatter_state, sync_checksum=prepared.sync_checksum)

    async def reconcile(self, prepared, entity):
        if entity.permalink is not None:
            prepared.markdown.frontmatter.metadata['permalink'] = entity.permalink
        return prepared

    BatchIndexer._normalize_markdown_file = normalize
    BatchIndexer._reconcile_persisted_permalink = reconcile
    from basic_memory.cli.main import app
    sys.exit(app())


def main() -> None:
    if sys.platform != 'darwin':
        raise RuntimeError('Basic Memory 1.0 launcher currently supports macOS only')
    root = _memory_root()
    _check_upstream()
    profile = '(version 1) (allow default) (deny network*) (deny file-write* (subpath ' + json.dumps(str(root)) + '))'
    env = dict(os.environ)
    env.update({'PYTHONDONTWRITEBYTECODE': '1',
                'HF_HUB_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1'})
    os.execve('/usr/bin/sandbox-exec', ['/usr/bin/sandbox-exec', '-p', profile,
              sys.executable, '-c',
              'from aios.basic_memory_100_launcher import _run_indexer; _run_indexer()',
              *sys.argv[1:]], env)


if __name__ == '__main__':
    main()
