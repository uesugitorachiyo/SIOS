"""Isolated, read-only Basic Memory index access for AIOS Memory notes.

The only setup mutation is Basic Memory's own configuration/database state in
``config_dir``. AIOS memory documents are supplied as a source directory.
"""

from __future__ import annotations

import json
import math
import os
import asyncio
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import stat
import subprocess

_VERSION = "Basic Memory version: 0.23.2"
_MARKER = ".aios-basic-memory-index.json"
_PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}\Z")
_TIMEOUT = 30


class BasicMemoryError(RuntimeError):
    """A sanitized dependency or isolation failure."""


def _normal(path: Path) -> Path:
    path = path.expanduser().absolute()
    return path.parent.resolve(strict=False) / path.name


def _within(path: Path, root: Path) -> bool:
    path = path.resolve(strict=False)
    root = root.resolve(strict=False)
    return path == root or root in path.parents


def _reported_project_path(raw: str, local_home: Path) -> Path:
    """Resolve Basic Memory's CLI shorthand under its isolated child HOME."""
    if raw.startswith(("~/", "~\\")):
        path = local_home / raw[2:]
    else:
        path = Path(raw)
    if not path.is_absolute():
        raise BasicMemoryError("Basic Memory project path is not absolute")
    return path.resolve()


def _check_node(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise BasicMemoryError("linked index or memory path is forbidden")


def _check_tree(root: Path) -> None:
    _check_node(root)
    if root.exists():
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                _check_node(Path(directory) / name)


def _check_checkout(path: Path) -> None:
    for ancestor in (path.resolve(strict=False), *path.resolve(strict=False).parents):
        if (ancestor / ".git").exists() or (ancestor / ".git").is_symlink() or (ancestor / "src" / "aios").exists():
            raise BasicMemoryError("index and memory paths must be outside source checkouts")


class BasicMemoryIndex:
    def __init__(self, executable: str, config_dir: Path, memory_root: Path, project: str):
        if not isinstance(executable, str) or not executable or not Path(executable).is_absolute():
            raise BasicMemoryError("Basic Memory executable must be an absolute path")
        if not isinstance(project, str) or not _PROJECT.fullmatch(project):
            raise BasicMemoryError("invalid Basic Memory project name")
        self.executable = executable
        self.config_dir = _normal(Path(config_dir))
        self.memory_root = _normal(Path(memory_root))
        self.project = project
        if _within(self.config_dir, self.memory_root) or _within(self.memory_root, self.config_dir):
            raise BasicMemoryError("index config and Memory source must be separate")
        for path in (self.config_dir, self.memory_root):
            _check_checkout(path)
            _check_tree(path)
        if not self.memory_root.is_dir():
            raise BasicMemoryError("Memory source directory is missing")

    def _environment(self) -> dict[str, str]:
        # Allow only process-launch essentials. Provider keys and inherited
        # Basic Memory/cloud settings never reach the child.
        allowed = ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL")
        environment = {key: os.environ[key] for key in allowed if key in os.environ}
        local_home = str(self.config_dir.parent)
        environment.update({
            "HOME": local_home,
            "USERPROFILE": local_home,
            "TMP": local_home,
            "TEMP": local_home,
            "TMPDIR": local_home,
            "XDG_CONFIG_HOME": local_home,
            "XDG_CACHE_HOME": local_home,
            "XDG_DATA_HOME": local_home,
            "BASIC_MEMORY_CONFIG_DIR": str(self.config_dir),
            "BASIC_MEMORY_HOME": str(self.config_dir / "unused-default-home"),
            "FASTEMBED_CACHE_PATH": str(self.config_dir / "fastembed_cache"),
            "HF_HOME": str(self.config_dir / "huggingface_cache"),
            "BASIC_MEMORY_CLOUD_MODE": "false",
            "BASIC_MEMORY_FORCE_CLOUD": "false",
            "BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED": "false",
            "BASIC_MEMORY_LOGFIRE_ENABLED": "false",
            "BASIC_MEMORY_LOGFIRE_SEND_TO_LOGFIRE": "false",
            "BASIC_MEMORY_AUTO_UPDATE": "false",
            "BASIC_MEMORY_CLOUD_PROMO_OPT_OUT": "true",
        })
        return environment

    def _run(self, *args: str, timeout: int = _TIMEOUT) -> str:
        self._validate_configuration()
        try:
            result = subprocess.run(
                [self.executable, *args], capture_output=True, text=True,
                env=self._environment(), cwd=self.config_dir.parent,
                timeout=timeout, check=False, shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BasicMemoryError("Basic Memory invocation unavailable") from None
        if result.returncode:
            raise BasicMemoryError("Basic Memory command failed")
        return result.stdout.strip()

    def _json(self, *args: str) -> object:
        try:
            return json.loads(self._run(*args))
        except (TypeError, json.JSONDecodeError):
            raise BasicMemoryError("Basic Memory returned invalid JSON") from None

    def _marker(self) -> dict[str, object]:
        return {"schema_version": 1, "project": self.project, "memory_root": str(self.memory_root.resolve())}

    def _ensure_index_root(self) -> None:
        _check_checkout(self.config_dir)
        _check_tree(self.config_dir)
        marker = self.config_dir / _MARKER
        if self.config_dir.exists():
            if not marker.is_file():
                raise BasicMemoryError("existing Basic Memory index has no AIOS marker")
            try:
                saved = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise BasicMemoryError("invalid Basic Memory index marker") from None
            if saved != self._marker():
                raise BasicMemoryError("Basic Memory index belongs to another source or project")
            return
        self.config_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.config_dir.mkdir()
        except FileExistsError:
            self._ensure_index_root()
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(marker, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(self._marker(), stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def setup(self) -> dict[str, object]:
        """Register only the selected local Memory project in an isolated DB."""
        self._ensure_index_root()
        fresh = self._prepare_configuration()
        if self._run("--version") != _VERSION:
            raise BasicMemoryError("unsupported Basic Memory version")
        projects = self._json("project", "list", "--local", "--json")
        # The CLI currently returns a list of project objects; tolerate an
        # enveloped list while refusing unexpected output.
        if isinstance(projects, dict):
            projects = projects.get("projects")
        if not isinstance(projects, list):
            raise BasicMemoryError("invalid Basic Memory project list")
        if any(not isinstance(item, dict) or item.get("name") != self.project for item in projects):
            raise BasicMemoryError("isolated index contains another project")
        if fresh and projects:
            raise BasicMemoryError("fresh isolated index database is not empty")
        if not projects:
            # A preseeded config already owns this name.  The CLI's add command
            # rejects that name, whereas set-local attaches its missing DB row.
            # Never use its path-update capability on an existing index.
            if not fresh:
                raise BasicMemoryError("isolated index registration is incomplete")
            self._ensure_index_root()
            _check_tree(self.memory_root)
            self._validate_configuration()
            self._run("project", "set-local", self.project,
                      "--local-path", str(self.memory_root))
            self._validate_configuration()
            projects = self._json("project", "list", "--local", "--json")
            if isinstance(projects, dict):
                projects = projects.get("projects")
        if not isinstance(projects, list) or len(projects) != 1 or not isinstance(projects[0], dict):
            raise BasicMemoryError("invalid Basic Memory project registration")
        registered = projects[0]
        if registered.get("name") != self.project or not isinstance(registered.get("local_path"), str) or not registered["local_path"]:
            raise BasicMemoryError("invalid Basic Memory project registration")
        if _reported_project_path(registered["local_path"], self.config_dir.parent) != self.memory_root.resolve():
            raise BasicMemoryError("Basic Memory project path differs from Memory source")
        status = self.status()
        self._validate_configuration(durable=True)
        return {"ok": True, "project": self.project, "version": "0.23.2",
                "index_status": status}

    def _prepare_configuration(self) -> bool:
        """Seed only a marker-only index; existing config never gains freshness."""
        self._ensure_index_root()
        config = self.config_dir / "config.json"
        _check_node(config)
        fresh = not config.exists()
        if fresh:
            if {p.name for p in self.config_dir.iterdir()} != {_MARKER}:
                raise BasicMemoryError("isolated index configuration is incomplete")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                root = self.config_dir.stat()
                descriptor = os.open(config, flags, 0o600)
                try:
                    held = os.fstat(descriptor)
                    with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
                        json.dump({"projects": {self.project: {
                            "path": str(self.memory_root), "mode": "local"}},
                            "default_project": self.project, "database_backend": "sqlite",
                            "ensure_frontmatter_on_sync": False,
                            "disable_permalinks": True,
                            "format_on_save": False}, stream)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                finally:
                    os.close(descriptor)
                named = config.lstat()
                current_root = self.config_dir.stat()
                if ((held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)
                        or (root.st_dev, root.st_ino) != (current_root.st_dev, current_root.st_ino)
                        or {p.name for p in self.config_dir.iterdir()} != {_MARKER, "config.json"}):
                    raise ValueError()
                self._sync_config_directory()
            except OSError:
                raise BasicMemoryError("isolated index configuration unavailable") from None
            except ValueError:
                raise BasicMemoryError("isolated index changed during initialization") from None
        self._validate_configuration()
        return fresh

    def _sync_config_directory(self) -> None:
        # File fsync applies on Windows too; its directory durability requires
        # native qualification rather than pretending POSIX directory handles work.
        if os.name == "nt":
            return
        descriptor = os.open(self.config_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _validate_configuration(self, *, durable: bool = False) -> None:
        """Validate a bounded held ordinary file, without seeding or repair."""
        try:
            self._ensure_index_root()
            config = self.config_dir / "config.json"
            _check_node(config)
            named = config.lstat()
            if not stat.S_ISREG(named.st_mode) or named.st_size > 1048576:
                raise ValueError()
            descriptor = os.open(config, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0))
            try:
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    held = os.fstat(stream.fileno())
                    def identity(info):
                        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    if not stat.S_ISREG(held.st_mode) or identity(named) != identity(held):
                        raise ValueError()
                    raw = stream.read(1048577)
                    if (len(raw) > 1048576 or identity(held) != identity(os.fstat(stream.fileno()))
                            or identity(held) != identity(config.lstat())):
                        raise ValueError()
                    if durable:
                        os.fsync(stream.fileno())
            finally:
                os.close(descriptor)
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            def nonfinite(value):
                raise ValueError()
            settings = json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)
            if not isinstance(settings, dict) or settings.get("default_project") != self.project:
                raise ValueError()
            if (settings.get("ensure_frontmatter_on_sync") is not False
                    or settings.get("disable_permalinks") is not True
                    or settings.get("format_on_save") is not False):
                raise ValueError()
            if (settings.get("database_backend", "sqlite") != "sqlite"
                    or settings.get("database_url") is not None or settings.get("project_root") is not None):
                raise ValueError()
            projects = settings.get("projects")
            if not isinstance(projects, dict) or set(projects) != {self.project}:
                raise ValueError()
            entry = projects[self.project]
            if not isinstance(entry, dict) or entry.get("mode") != "local":
                raise ValueError()
            if (not isinstance(entry.get("path"), str) or not entry["path"]
                    or not Path(entry["path"]).is_absolute()):
                raise ValueError()
            if Path(entry["path"]).resolve() != self.memory_root.resolve():
                raise ValueError()
            if (entry.get("workspace_id") is not None or entry.get("local_sync_path") is not None
                    or entry.get("cloud_sync_path") is not None
                    or entry.get("bisync_initialized", False) is not False
                    or entry.get("last_sync") is not None):
                raise ValueError()
            if durable:
                self._sync_config_directory()
        except (OSError, ValueError, TypeError):
            raise BasicMemoryError("isolated index configuration differs from selected local project") from None

    def search(self, query: str) -> object:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise BasicMemoryError("invalid search query")
        self._ensure_index_root()
        return self._json("tool", "search-notes", "--project", self.project,
                          "--local", "--json", "--", query)

    def status(self) -> object:
        self._ensure_index_root()
        return self._json("status", "--project", self.project, "--local", "--json")

    async def wait_ready(self, query: str, timeout: float = 30,
                         target_path: Path | None = None) -> dict[str, object]:
        """Briefly observe scoped MCP search for a target revision marker.

        The MCP process is closed on success or timeout; this never exposes its
        tool list to callers and never becomes a persistent backend service.
        """
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise BasicMemoryError("invalid search query")
        if not 0 < timeout <= 30:
            raise BasicMemoryError("readiness timeout must be within 30 seconds")
        self._ensure_index_root()
        self._validate_configuration()
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            raise BasicMemoryError("MCP SDK unavailable for scoped index refresh") from None

        parameters = StdioServerParameters(
            command=self.executable,
            args=["mcp", "--transport", "stdio", "--project", self.project],
            env=self._environment(), cwd=self.config_dir.parent,
        )
        last_search: object = None
        last_reason = "index_pending"
        try:
            async with asyncio.timeout(timeout):
                with open(os.devnull, "w", encoding="utf-8") as errors:
                    async with stdio_client(parameters, errlog=errors) as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            while True:
                                # A new attempt has no completed observation yet.
                                last_search = None
                                last_reason = "index_pending"
                                response = await session.call_tool("search_notes", arguments={
                                    "query": query, "project": self.project,
                                    "search_type": "text", "output_format": "json",
                                })
                                if response.is_error:
                                    raise BasicMemoryError("scoped Basic Memory search failed")
                                search = _mcp_json(response)
                                found = _readiness_observation(search)
                                if found and target_path is not None:
                                    payload = search.get("result", search)
                                    expected = target_path.resolve()
                                    found = False
                                    for hit in payload["results"]:
                                        raw = Path(hit["file_path"])
                                        if raw.is_absolute():
                                            candidate = raw.resolve()
                                        elif raw.parts and raw.parts[0] == "Memory":
                                            candidate = (self.memory_root.parent / raw).resolve()
                                        else:
                                            candidate = (self.memory_root / raw).resolve()
                                        snippet = "\n".join(value for value in
                                            (hit.get("matched_chunk"), hit.get("content"))
                                            if isinstance(value, str))
                                        if candidate == expected and query.casefold() in snippet.casefold():
                                            found = True
                                            break
                                last_search = search
                                if found:
                                    return {"ready": True, "search": last_search}
                                last_reason = "no_match"
                                await asyncio.sleep(0.5)
        except TimeoutError:
            return {"ready": False, "reason": last_reason, "search": last_search}
        except BasicMemoryError:
            raise
        except Exception:
            raise BasicMemoryError("scoped Basic Memory refresh unavailable") from None

    def _indexed_checksums_match(self, expected: dict[str, str]) -> bool:
        """Read the pinned local index without opening a write connection."""
        database = self.config_dir / 'memory.db'
        _check_node(database)
        if not database.is_file():
            return False
        try:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0.25)) as db:
                db.execute('PRAGMA query_only=ON')
                projects = db.execute('SELECT id, name, path FROM project').fetchall()
                if (len(projects) != 1 or projects[0][1] != self.project or
                        Path(projects[0][2]).resolve() != self.memory_root.resolve()):
                    raise BasicMemoryError('index project does not match binding')
                project_id = projects[0][0]
                for path, checksum in expected.items():
                    rows = db.execute('SELECT checksum FROM entity WHERE project_id=? AND file_path=?',
                                      (project_id, path)).fetchall()
                    if rows != [(checksum,)]:
                        return False
            return True
        except sqlite3.OperationalError:
            return False
        except (sqlite3.Error, OSError, TypeError, ValueError):
            raise BasicMemoryError('index checksum observation unavailable') from None

    async def wait_for_files(self, expected: dict[str, str], timeout: float = 7) -> bool:
        """Briefly run the existing scoped MCP sync and check exact indexed hashes."""
        if (not isinstance(expected, dict) or not expected or len(expected) > 2 or
                not 0 < timeout <= 7):
            raise BasicMemoryError('invalid index observation request')
        for path, checksum in expected.items():
            if (not isinstance(path, str) or not path or '\\' in path or path.startswith('/') or
                    any(part in ('', '.', '..') for part in path.split('/')) or
                    not path.endswith('.md') or not isinstance(checksum, str) or
                    re.fullmatch(r'[0-9a-f]{64}', checksum) is None):
                raise BasicMemoryError('invalid index observation target')
        self._ensure_index_root()
        self._validate_configuration()
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            parameters = StdioServerParameters(
                command=self.executable,
                args=['mcp', '--transport', 'stdio', '--project', self.project],
                env=self._environment(), cwd=self.config_dir.parent,
            )
            async with asyncio.timeout(timeout):
                with open(os.devnull, 'w', encoding='utf-8') as errors:
                    async with stdio_client(parameters, errlog=errors) as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            while not self._indexed_checksums_match(expected):
                                await asyncio.sleep(0.25)
                            return True
        except TimeoutError:
            return False
        except BasicMemoryError:
            raise
        except Exception:
            raise BasicMemoryError('scoped Basic Memory refresh unavailable') from None


def _readiness_observation(search: object) -> bool:
    """Validate the pinned 0.23.2 search shape; status is not an index lag count."""
    if isinstance(search, dict) and set(search) == {"result"}:
        search = search["result"]
    search_fields = {"results", "current_page", "page_size", "total", "total_is_exact", "has_more"}
    if not isinstance(search, dict) or not search_fields <= set(search):
        raise BasicMemoryError("scoped Basic Memory search unavailable")
    if (not isinstance(search["results"], list)
            or type(search["current_page"]) is not int or search["current_page"] < 1
            or type(search["page_size"]) is not int or search["page_size"] < 1
            or type(search["total"]) is not int or search["total"] < 0
            or type(search["total_is_exact"]) is not bool
            or type(search["has_more"]) is not bool):
        raise BasicMemoryError("scoped Basic Memory search unavailable")
    for item in search["results"]:
        if (not isinstance(item, dict) or not isinstance(item.get("title"), str)
                or not isinstance(item.get("type"), str)
                or item["type"] not in {"entity", "observation", "relation"}
                or not isinstance(item.get("file_path"), str) or not item["file_path"].strip()
                or type(item.get("score")) is not float
                or not math.isfinite(item["score"])):
            raise BasicMemoryError("scoped Basic Memory search unavailable")
    return bool(search["results"])


def _mcp_json(response: object) -> object:
    structured = getattr(response, "structured_content", None)
    if isinstance(structured, (dict, list)):
        return structured
    for block in getattr(response, "content", ()):
        content = getattr(block, "text", None)
        if isinstance(content, str):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                continue
    raise BasicMemoryError("scoped Basic Memory returned invalid JSON")
