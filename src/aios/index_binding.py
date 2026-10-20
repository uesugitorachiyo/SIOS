"""Private references for an isolated historical index; no backend write surface."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .basic_memory import BasicMemoryIndex, BasicMemoryError
from .config import Config
from .store import Store

BINDING = '.aios-basic-memory.json'


def add_read_paths(store: Store, search: object) -> object:
    """Add a Store.read-ready path while preserving Basic Memory's raw hit fields.

    The pinned index reports paths relative to Memory, but Store.read expects
    paths relative to the data root. Reject index paths that leave Memory.
    """
    if not isinstance(search, dict):
        return search
    payload = search.get('result', search)
    if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
        return search
    updated = []
    for hit in payload['results']:
        if not isinstance(hit, dict) or not isinstance(hit.get('file_path'), str):
            raise BasicMemoryError('invalid indexed Memory path')
        raw = hit['file_path']
        path = Path(raw)
        if path.is_absolute():
            try:
                relative = path.relative_to(store.root / 'Memory').as_posix()
            except ValueError:
                raise BasicMemoryError('indexed path outside Memory') from None
            read_path = 'Memory/' + relative
        elif raw.startswith('Memory/'):
            read_path = raw
        else:
            read_path = 'Memory/' + raw
        try:
            read_path = store._read_relative(read_path)
            if not read_path.startswith('Memory/') or not read_path.endswith('.md'):
                raise ValueError()
            store._read_path(read_path)
        except (ValueError, TypeError, OSError):
            raise BasicMemoryError('invalid indexed Memory path') from None
        if 'read_path' in hit and hit['read_path'] != read_path:
            raise BasicMemoryError('conflicting indexed Memory path')
        updated.append({**hit, 'read_path': read_path})
    amended = {**payload, 'results': updated}
    return {**search, 'result': amended} if payload is not search else amended


def load_index(root: Path) -> BasicMemoryIndex:
    path = root / BINDING
    Store._check_node(path)
    if not path.is_file():
        raise BasicMemoryError('historical_index_not_configured')
    if path.stat().st_size > 8192:
        raise BasicMemoryError('invalid_historical_index_binding')
    try:
        settings = json.loads(path.read_text(encoding='utf-8'))
        if set(settings) != {'schema_version', 'executable', 'config_dir', 'project'} or settings['schema_version'] != 1:
            raise ValueError()
        return BasicMemoryIndex(settings['executable'], Path(settings['config_dir']), root/'Memory', settings['project'])
    except (ValueError, TypeError, KeyError, OSError):
        raise BasicMemoryError('invalid_historical_index_binding') from None


def setup_index(config: Config, executable: str, index_root: Path | None = None) -> dict:
    if config.mode == 'client':
        raise BasicMemoryError('index_setup_requires_authoritative_host')
    directory = index_root or config.data_root/'Database'/'basic-memory'
    project = 'aios-' + config.instance_id
    adapter = BasicMemoryIndex(executable, directory, config.data_root/'Memory', project)
    settings = {'schema_version': 1, 'executable': executable,
                'config_dir': str(adapter.config_dir), 'project': project}
    binding = config.data_root/BINDING
    Store._check_node(binding)
    if binding.exists():
        if json.loads(binding.read_text(encoding='utf-8')) != settings:
            raise BasicMemoryError('historical_index_binding_conflict')
    result = adapter.setup()
    if not binding.exists():
        descriptor = os.open(binding, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
                json.dump(settings,stream,indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            binding.unlink(missing_ok=True)
            raise
    return result
