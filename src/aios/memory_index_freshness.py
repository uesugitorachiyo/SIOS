#!/usr/bin/env python3
"""Read-only source/SQLite freshness check for the bound private Memory index.

This is a local diagnostic, not an authenticated SIOS client or an index repair.
It prints counts and hashed path identifiers only; it never prints note content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys


HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _ordinary(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    wanted = stat.S_ISDIR if directory else stat.S_ISREG
    if not wanted(info.st_mode):
        raise ValueError("index or source contains a linked/nonordinary path")


def _digest_file(path: Path) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    after = path.stat()
    identity = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns)
    if identity(before) != identity(after):
        raise ValueError("source or index changed during read")
    return digest.hexdigest()


def _source_files(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        folder = Path(directory)
        for name in dirs:
            _ordinary(folder / name, directory=True)
        for name in files:
            path = folder / name
            _ordinary(path, directory=False)
            if path.suffix.lower() != ".md":
                continue
            relative = path.relative_to(root).as_posix()
            result[relative] = _digest_file(path)
    return result


def _path_hashes(paths: set[str]) -> list[str]:
    return sorted(hashlib.sha256(path.encode("utf-8")).hexdigest() for path in paths)


def check(data_root: Path) -> dict[str, object]:
    if not data_root.is_absolute():
        raise ValueError("data root must be absolute")
    _ordinary(data_root, directory=True)
    memory = data_root / "Memory"
    _ordinary(memory, directory=True)
    binding = data_root / ".aios-basic-memory.json"
    _ordinary(binding, directory=False)
    if binding.stat().st_size > 8192:
        raise ValueError("index binding is oversized")
    settings = json.loads(binding.read_text(encoding="utf-8"))
    if set(settings) != {"schema_version", "executable", "config_dir", "project"} or settings["schema_version"] != 1:
        raise ValueError("unexpected index binding")
    config_dir = Path(settings["config_dir"])
    if (not config_dir.is_absolute() or config_dir.parent != data_root.parent
            or config_dir.name != "index" or not isinstance(settings["project"], str)):
        raise ValueError("invalid bound index path or project")
    _ordinary(config_dir, directory=True)
    database = config_dir / "memory.db"
    _ordinary(database, directory=False)
    if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("active SQLite sidecar prevents immutable snapshot comparison")

    db_before = database.stat()
    index_sha = _digest_file(database)
    source = _source_files(memory)
    connection = sqlite3.connect(database.as_uri() + "?immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        projects = connection.execute("SELECT id,name,path FROM project").fetchall()
        if len(projects) != 1 or projects[0][1] != settings["project"] or Path(projects[0][2]) != memory:
            raise ValueError("index project differs from binding")
        rows = connection.execute(
            "SELECT file_path,checksum,content_type FROM entity WHERE project_id=?",
            (projects[0][0],),
        ).fetchall()
    finally:
        connection.close()
    indexed: dict[str, str] = {}
    for relative, checksum, content_type in rows:
        if not isinstance(relative, str) or not isinstance(checksum, str):
            raise ValueError("invalid indexed file path or checksum")
        parts = PurePosixPath(relative).parts
        if (content_type != "text/markdown" or not parts or relative.startswith("/")
                or relative != PurePosixPath(relative).as_posix()
                or "\\" in relative or any(part in ("", ".", "..") for part in parts)
                or not HEX64.fullmatch(checksum) or relative in indexed):
            raise ValueError("unexpected indexed entity")
        indexed[relative] = checksum
    db_after = database.stat()
    identity = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns)
    if identity(db_before) != identity(db_after) or _digest_file(database) != index_sha:
        raise ValueError("index changed during comparison")

    missing = set(source) - set(indexed)
    orphan = set(indexed) - set(source)
    changed = {path for path in source.keys() & indexed.keys() if source[path] != indexed[path]}
    fresh = not (missing or orphan or changed)
    return {
        "status": "fresh" if fresh else "lag",
        "authority_attested": False,
        "project": settings["project"],
        "index_sha256": index_sha,
        "source_files": len(source),
        "indexed_entities": len(indexed),
        "missing_count": len(missing),
        "changed_count": len(changed),
        "orphan_count": len(orphan),
        "missing_path_hashes": _path_hashes(missing),
        "changed_path_hashes": _path_hashes(changed),
        "orphan_path_hashes": _path_hashes(orphan),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_root", type=Path, help="absolute private SIOS data root")
    args = parser.parse_args()
    try:
        result = check(args.data_root)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "authority_attested": False, "reason": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "fresh" else 2


if __name__ == "__main__":
    sys.exit(main())
