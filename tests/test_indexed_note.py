import tempfile
import unittest
import sys
import json
from pathlib import Path
from unittest.mock import patch

from aios.indexed_note import refresh
from aios.basic_memory import BasicMemoryIndex, _mcp_json, _readiness_observation, _reported_project_path, BasicMemoryError
from aios.service import dispatch
from aios.store import ConflictError, Store, ValidationError


class IndexedNoteTests(unittest.TestCase):
    def test_index_config_cannot_rewrite_source_notes(self):
        with tempfile.TemporaryDirectory(prefix="sios-index-config-") as raw:
            root = Path(raw)
            memory = root / "data/Memory"
            memory.mkdir(parents=True)
            index = BasicMemoryIndex(sys.executable, root / "index", memory, "sios-test")
            child_env = index._environment()
            for key in ("HOME", "USERPROFILE", "TMPDIR", "BASIC_MEMORY_CONFIG_DIR",
                        "FASTEMBED_CACHE_PATH", "HF_HOME"):
                self.assertTrue(Path(child_env[key]).is_relative_to(root.resolve()))
            self.assertTrue(index._prepare_configuration())
            path = root / "index/config.json"
            settings = json.loads(path.read_text())
            self.assertIs(settings["ensure_frontmatter_on_sync"], False)
            self.assertIs(settings["disable_permalinks"], True)
            self.assertIs(settings["format_on_save"], False)
            settings["ensure_frontmatter_on_sync"] = True
            path.write_text(json.dumps(settings))
            with self.assertRaises(BasicMemoryError):
                index._validate_configuration()

    def test_pinned_mcp_result_uses_snake_case_structured_content(self):
        try:
            from mcp.types import CallToolResult
        except ImportError:
            self.skipTest("optional MCP dependency is not installed")
        payload = {"results": []}
        result = CallToolResult(content=[], structured_content=payload)
        self.assertFalse(result.is_error)
        self.assertEqual(_mcp_json(result), payload)

    def test_basic_memory_project_shorthand_uses_isolated_home(self):
        local_home = Path(tempfile.gettempdir()).resolve() / "isolated-index-parent"
        self.assertEqual(_reported_project_path("~/data/Memory", local_home),
                         local_home / "data/Memory")
        with self.assertRaises(BasicMemoryError):
            _reported_project_path("data/Memory", local_home)

    def test_basic_memory_023_search_contract(self):
        response = {"results": [{"title": "Sample", "type": "entity",
            "score": 0.9, "file_path": "notes/sample.md", "matched_chunk": "green lantern"}],
            "current_page": 1, "page_size": 10, "total": 1,
            "total_is_exact": True, "has_more": False}
        self.assertTrue(_readiness_observation(response))
        with self.assertRaises(BasicMemoryError):
            _readiness_observation({key: value for key, value in response.items()
                                    if key != "total_is_exact"})

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="sios-synthetic-")
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name).resolve() / "data")
        self.first = dispatch(self.store, "memory_write", {"identifier": "sample",
            "content": "# Sample\nblue lantern", "request_id": "create-1", "create": True},
            actor="satellite", scopes={"read", "memory:write"})

    def readiness(self, query, timeout=30):
        async def ready(query, timeout=30, target_path=None):
            return {"ready": True, "search": {"results": [{"title": "Sample", "type": "entity",
                "score": 0.9, "file_path": "notes/sample.md",
                "matched_chunk": query + " lantern"}]}}
        class Index:
            wait_ready = staticmethod(ready)
        return Index()

    def test_create_refresh_read_update_and_idempotent_retry(self):
        with patch("aios.indexed_note.load_index", return_value=self.readiness(None)):
            first = dispatch(self.store, "memory_index_refresh", {
                "identifier": "sample", "expected_revision": self.first["result"]["revision"],
                "query": "lantern"}, actor="satellite", scopes={"read", "memory:write"})
            self.assertTrue(first["ready"])
            self.assertEqual(first["note"]["text"], "# Sample\nblue lantern")
            self.assertEqual(first["index_hit"]["path"], "Memory/notes/sample.md")
            update_args = {"identifier": "sample", "content": "# Sample\ngreen lantern",
                "request_id": "update-2", "expected_revision": self.first["result"]["revision"]}
            updated = dispatch(self.store, "memory_write", update_args,
                actor="satellite", scopes={"read", "memory:write"})
            self.assertEqual(updated, dispatch(self.store, "memory_write", update_args,
                actor="satellite", scopes={"read", "memory:write"}))
            with self.assertRaises(ConflictError):
                refresh(self.store, "sample", self.first["result"]["revision"], "blue")
            current = refresh(self.store, "sample", updated["result"]["revision"], "green")
            self.assertEqual(current["note"]["text"], "# Sample\ngreen lantern")

    def test_permission_and_wrong_target(self):
        args = {"identifier": "sample", "expected_revision": self.first["result"]["revision"],
                "query": "lantern"}
        with self.assertRaises(PermissionError):
            dispatch(self.store, "memory_index_refresh", args, actor="write-only",
                     scopes={"memory:write"})
        class Index:
            async def wait_ready(self, query, timeout=30, target_path=None):
                return {"ready": True, "search": {"results": [{"title": "Wrong", "type": "entity",
                    "score": 0.9, "file_path": "notes/other.md"}]}}
        with patch("aios.indexed_note.load_index", return_value=Index()):
            result = refresh(self.store, "sample", self.first["result"]["revision"], "lantern")
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "target_marker_not_found")
        with self.assertRaises(ValidationError):
            refresh(self.store, "../sample", self.first["result"]["revision"], "lantern")

    def test_stale_hit_for_same_path_does_not_prove_update(self):
        updated = self.store.write_memory("sample", "# Sample\ngreen lantern", "update-3",
            expected_revision=self.first["result"]["revision"])
        class StaleIndex:
            async def wait_ready(self, query, timeout=30, target_path=None):
                return {"ready": True, "search": {"results": [{"title": "Sample",
                    "type": "entity", "score": 0.9, "file_path": "notes/sample.md",
                    "matched_chunk": "blue lantern"}]}}
        with patch("aios.indexed_note.load_index", return_value=StaleIndex()):
            result = refresh(self.store, "sample", updated["result"]["revision"], "green")
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "target_marker_not_found")


if __name__ == "__main__":
    unittest.main()
