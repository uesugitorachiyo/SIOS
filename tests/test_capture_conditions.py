"""Synthetic selective-capture validation at the local MCP boundary."""

import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from aios.mcp_adapter import run
from aios.selective_memory import _prepare_capture, parse_capture_json


BASE = {
    "retain": True, "kind": "delta", "request_id": "fixture-capture-conditions",
    "meaning_id": "fixture-capture-conditions", "claim": "Fabricated decision only.",
    "source_type": "observed", "source_actor": "fixture-client",
    "source_reference": "Memory/notes/fabricated.md", "project": "sios-test",
    "conditions": ["fabricated test only"], "outcome": "Synthetic validation",
    "evidence_status": "observed", "recorded_at": "2026-10-19T23:22:39+00:00",
    "retrieval_cue": "fixture-capture-conditions",
}


class CaptureConditionTests(unittest.TestCase):
    def test_core_rejects_scalar_condition_before_store(self):
        value = dict(BASE, conditions="fabricated test only")
        with self.assertRaisesRegex(ValueError, "conditions must be a short list"):
            _prepare_capture(parse_capture_json(json.dumps(value)))

    def test_core_accepts_string_list_before_store(self):
        prepared = _prepare_capture(parse_capture_json(json.dumps(BASE)))
        self.assertEqual(prepared[0], BASE["request_id"])
        self.assertEqual(prepared[1]["conditions"], BASE["conditions"])

    def test_mcp_error_is_field_specific_and_never_echoes_value(self):
        try:
            from mcp.server.mcpserver.exceptions import ToolError
        except ImportError:
            self.skipTest("optional MCP dependency is not installed")

        tools, calls = {}, []

        class FakeServer:
            def __init__(self, *args, **kwargs):
                pass

            def tool(self, **kwargs):
                def register(fn):
                    tools[fn.__name__] = fn
                    return fn
                return register

            def run(self, **kwargs):
                pass

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def call(self, operation, params):
                calls.append((operation, params))
                return {"status": "synthetic_only"}

        with patch("mcp.server.mcpserver.MCPServer", FakeServer), \
                patch("aios.mcp_adapter.Client", FakeClient):
            run(SimpleNamespace(mode="client"))

        for bad in ("PRIVATE-SENTINEL", ["okay", 7]):
            with self.subTest(bad=bad), self.assertRaises(ToolError) as captured:
                tools["memory_capture"](json.dumps(dict(BASE, conditions=bad)))
            self.assertIn("conditions must be a list of strings", str(captured.exception))
            self.assertNotIn("PRIVATE-SENTINEL", str(captured.exception))
            self.assertEqual(calls, [])

        self.assertEqual(tools["memory_capture"](json.dumps(BASE)),
                         {"status": "synthetic_only"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "memory_capture")


if __name__ == "__main__":
    unittest.main()
