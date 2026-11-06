"""Bounded, conservative client-side evidence for selective capture readiness.

The server's committed result remains authoritative. These extra calls never
retry a write. They observe one exact-stem hit, two index checksums and a full
source read; they do not fence concurrent writers or prove semantic coverage.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any, Callable

_STEM = re.compile(r"selective-[0-9a-f]{64}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class CaptureReadiness:
    """One bounded observation per client process; overlapping proofs fail closed.

    A daemon thread may continue a read-only RPC after the caller's deadline;
    it retains the single slot until it returns. Process exit can end that
    thread, but does not prove the remote operation ended. Existing transport
    response limits still apply; this class does not cap bytes in transit.
    """

    def __init__(self, call: Callable[[str, dict[str, Any]], Any], *, timeout: float = 10.0,
                 max_status_rows: int = 10000, max_status_bytes: int = 2 * 1024 * 1024):
        if not callable(call) or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 15:
            raise ValueError("invalid readiness configuration")
        if type(max_status_rows) is not int or not 1 <= max_status_rows <= 10000:
            raise ValueError("invalid status row bound")
        if type(max_status_bytes) is not int or not 1024 <= max_status_bytes <= 2 * 1024 * 1024:
            raise ValueError("invalid status size bound")
        self._call = call
        self._timeout = float(timeout)
        self._max_status_rows = max_status_rows
        self._max_status_bytes = max_status_bytes
        self._slot = threading.Lock()

    @staticmethod
    def _target(path: Any, revision: Any) -> tuple[str, str, str] | None:
        if not isinstance(path, str) or not isinstance(revision, str) or not _SHA.fullmatch(revision):
            return None
        if not path.startswith("Memory/notes/") or not path.endswith(".md"):
            return None
        stem = path[len("Memory/notes/"):-3]
        if not _STEM.fullmatch(stem) or path != "Memory/notes/" + stem + ".md":
            return None
        return path, stem, revision

    def _status_matches(self, status: Any, stem: str, revision: str) -> bool:
        if not isinstance(status, dict) or type(status.get("total_files")) is not int:
            return False
        files = status.get("observed_files")
        if not isinstance(files, list) or len(files) > self._max_status_rows or status["total_files"] != len(files):
            return False
        try:
            if len(json.dumps(status, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > self._max_status_bytes:
                return False
        except (TypeError, ValueError, UnicodeError):
            return False
        target = "notes/" + stem + ".md"
        matches = [row for row in files if isinstance(row, dict) and row.get("path") == target]
        return len(matches) == 1 and matches[0].get("checksum") == revision

    @staticmethod
    def _search_matches(search: Any, path: str, stem: str) -> bool:
        if not isinstance(search, dict) or not isinstance(search.get("results"), list):
            return False
        rows = search["results"]
        if len(rows) > 100:
            return False
        total = search.get('total')
        page = search.get('current_page')
        size = search.get('page_size')
        has_more = search.get('has_more')
        if type(total) is not int or total < len(rows):
            return False
        if search.get('total_is_exact') is not True:
            return False
        if type(has_more) is not bool or has_more != (total > len(rows)):
            return False
        if type(page) is not int or page != 1:
            return False
        if type(size) is not int or not 1 <= size <= 100 or len(rows) > size:
            return False
        if has_more and len(rows) != size:
            return False
        exact = [row for row in rows if isinstance(row, dict) and row.get("read_path") == path]
        return (len(exact) == 1 and exact[0].get("title") == stem and exact[0].get("type") == "entity"
                and exact[0].get("file_path") == "notes/" + stem + ".md")

    def _observe(self, path: str, stem: str, revision: str, deadline: float,
                 stage_start: Callable[[str], None], stage_end: Callable[[], None]) -> bool:
        def request(stage: str, operation: str, params: dict[str, Any]) -> Any:
            if time.monotonic() >= deadline:
                raise TimeoutError
            stage_start(stage)
            try:
                value = self._call(operation, params)
            finally:
                stage_end()
            if time.monotonic() >= deadline:
                raise TimeoutError
            return value

        health = request("health", "health", {})
        if not isinstance(health, dict) or health.get("ok") is not True or health.get("database") != "ok":
            return False
        first = request("status_before", "memory_index_status", {})
        if not self._status_matches(first, stem, revision):
            return False
        if not self._search_matches(request("search", "memory_index_search", {"query": stem}), path, stem):
            return False
        source = request("read", "read", {"relative": path})
        if not isinstance(source, dict) or source.get("path") != path or source.get("revision") != revision:
            return False
        text = source.get("text")
        if not isinstance(text, str) or hashlib.sha256(text.encode("utf-8")).hexdigest() != revision:
            return False
        return self._status_matches(request("status_after", "memory_index_status", {}), stem, revision)

    def review(self, result: Any) -> Any:
        """Return the committed receipt immediately; do not start any RPC."""
        if not isinstance(result, dict):
            return result
        reported = result.get("index_refresh")
        if not isinstance(reported, dict):
            return result
        server_ready = reported.get("ready")
        if server_ready is False:
            return result

        # Preserve the server's checksum-only claim while accurately withholding
        # search visibility until a separate observation is requested.
        copy = dict(result)
        refresh = dict(reported)
        copy["index_refresh"] = refresh
        if server_ready is True:
            refresh.update(ready=False, reason="search_visibility_unverified", server_reported_ready=True)
        else:
            # Do not expose an arbitrary malformed value, which could include
            # sensitive text, or let a truthy non-boolean pass as ready.
            refresh.update(ready=False, reason="server_readiness_malformed", server_ready_classification="malformed")
        return copy

    def observe(self, path: Any, revision: Any) -> dict[str, Any]:
        """Observe one already committed source; never retry or issue a write."""
        response = {'ready': False, 'reason': 'not_confirmed',
                    'scope': 'exact_stem_checksum_and_full_source_observation',
                    'index_coverage': 'unknown', 'atomic_snapshot': False}
        start = time.monotonic()
        target = self._target(path, revision)
        if target is None:
            response['diagnostic'] = {'stage': 'input', 'outcome': 'invalid_request', 'elapsed_ms': 0, 'stages': []}
            return response
        if not self._slot.acquire(blocking=False):
            response['diagnostic'] = {'stage': 'input', 'outcome': 'inflight', 'elapsed_ms': 0, 'stages': []}
            return response
        deadline = start + self._timeout
        done = threading.Event()
        lock = threading.Lock()
        state = {'stage': 'startup', 'began': None, 'stages': [], 'outcome': 'pending'}

        def elapsed_ms(when: float) -> int:
            return max(0, min(int((when - start) * 1000), int(self._timeout * 1000)))

        def stage_start(stage: str) -> None:
            with lock:
                state['stage'] = stage
                state['began'] = time.monotonic()

        def stage_end() -> None:
            with lock:
                now = time.monotonic()
                if state['began'] is not None:
                    state['stages'].append({'stage': state['stage'],
                                            'elapsed_ms': max(0, min(int((now - state['began']) * 1000), int(self._timeout * 1000)))})
                    state['began'] = None

        def run() -> None:
            outcome = 'rpc_error'
            try:
                confirmed = self._observe(*target, deadline, stage_start, stage_end) is True
                outcome = 'confirmed' if confirmed else 'evidence_unconfirmed'
            except TimeoutError:
                outcome = 'rpc_or_deadline_timeout'
            except Exception:
                outcome = 'rpc_error'
            finally:
                with lock:
                    state['outcome'] = outcome
                self._slot.release()
                done.set()

        try:
            threading.Thread(target=run, name="sios-client-readiness-proof", daemon=True).start()
        except Exception:
            self._slot.release()
            response['diagnostic'] = {'stage': 'startup', 'outcome': 'start_error', 'elapsed_ms': elapsed_ms(time.monotonic()), 'stages': []}
            return response
        finished = done.wait(max(0.0, deadline - time.monotonic()))
        now = time.monotonic()
        with lock:
            stages = [dict(row) for row in state['stages']]
            if state['began'] is not None:
                stages.append({'stage': state['stage'], 'elapsed_ms': max(0, min(int((now - state['began']) * 1000), int(self._timeout * 1000)))})
            outcome = state['outcome'] if finished and now < deadline else 'deadline_exceeded'
            response['diagnostic'] = {'stage': state['stage'], 'outcome': outcome,
                                      'elapsed_ms': elapsed_ms(now), 'stages': stages}
        if outcome == 'confirmed':
            response.update(ready=True, reason=None)
        return response
