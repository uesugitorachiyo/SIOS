import http.client
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from aios.server import Device, load_devices, make_server
from aios.store import SECTIONS, Store


class MissionWriteNameTests(unittest.TestCase):
    def test_device_file_validates_restrictions(self):
        base = {"identity": "test-writer", "token_env": "TEST_WRITER",
                "scopes": ["read", "missions:write"]}
        with TemporaryDirectory() as temp, patch.dict(os.environ, {"TEST_WRITER": "x" * 40}):
            config = Path(temp) / "devices.json"
            for entry, expected in [
                (base, None),
                ({**base, "mission_write_names": []}, frozenset()),
                ({**base, "mission_write_names": ["sample"]}, frozenset({"sample"})),
            ]:
                config.write_text(json.dumps([entry]), encoding="utf-8")
                self.assertEqual(load_devices(config)[0].mission_write_names, expected)
            for overrides in [
                {"mission_write_names": None},
                {"mission_write_names": ["../sample"]},
                {"mission_write_names": ["Sample"]},
                {"mission_write_names": ["sample", "sample"]},
                {"mission_write_names": [44]},
                {"mission_write_names": [], "scopes": ["read"]},
                {"mission_write_names": [], "scopes": ["missions:write", "missions:complete"]},
            ]:
                with self.subTest(overrides=overrides):
                    config.write_text(json.dumps([{**base, **overrides}]), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_devices(config)

    def test_three_authenticated_writers_and_exact_boundaries(self):
        write = frozenset({"read", "missions:write"})
        allowed = frozenset({"sample"})
        tokens = ["a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40]
        devices = [Device("codex", tokens[0], write),
                   Device("claude", tokens[1], write, allowed),
                   Device("antigravity", tokens[2], write, allowed),
                   Device("reader", tokens[3], frozenset({"read"})),
                   Device("deny-all", tokens[4], write, frozenset())]
        body = "---\nstatus: in_progress\n---\n\n# Fictional Mission\n\n" + "\n\n".join(
            f"## {section}\n\nFictional evidence." for section in SECTIONS)
        with TemporaryDirectory() as temp:
            store = Store(Path(temp).resolve())
            with make_server(store, "fixture", devices) as server:
                worker = Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    def rpc(index, operation, **params):
                        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                        try:
                            conn.request("POST", "/v1/rpc", json.dumps(
                                {"instance_id": "fixture", "operation": operation, "params": params}),
                                {"Authorization": "Bearer " + tokens[index],
                                 "Content-Type": "application/json"})
                            response = conn.getresponse()
                            return response.status, json.load(response)
                        finally:
                            conn.close()

                    for name in ("sample", "unrelated"):
                        status, _ = rpc(0, "mission_write", name=name, content=body,
                                        request_id="create-" + name, create=True)
                        self.assertEqual(status, 200)
                    status, data = rpc(0, "read", relative="Missions/unrelated/mission.md")
                    self.assertEqual(status, 200)
                    unrelated_revision = data["result"]["revision"]
                    status, data = rpc(0, "read", relative="Missions/sample/mission.md")
                    self.assertEqual(status, 200)
                    revision = data["result"]["revision"]
                    for index in range(3):
                        before = revision
                        content = body + f"\n\nClient update {index}.\n"
                        status, data = rpc(index, "mission_write", name="sample", content=content,
                                           request_id=f"update-{index}", expected_revision=before)
                        self.assertEqual(status, 200)
                        revision = data["result"]["result"]["revision"]
                        self.assertNotEqual(revision, before)
                        self.assertEqual(rpc(index, "mission_write", name="sample", content=body,
                                             request_id=f"stale-{index}", expected_revision=before)[0], 409)
                        entry = {"name": "sample", "entry": f"Entry {index}.",
                                 "request_id": f"log-{index}"}
                        status, data = rpc(index, "mission_log", **entry)
                        self.assertEqual(status, 200)
                        replay_status, replay = rpc(index, "mission_log", **entry)
                        self.assertEqual(replay_status, 200)
                        self.assertEqual(replay["result"]["result"], data["result"]["result"])
                        status, data = rpc(index, "read", relative="Missions/sample/mission.md")
                        self.assertEqual(status, 200)
                        self.assertEqual(data["result"]["text"], content)
                        self.assertEqual(data["result"]["revision"], revision)
                        status, data = rpc(index, "read", relative="Missions/sample/log.md")
                        self.assertEqual(status, 200)
                        self.assertEqual(data["result"]["text"].count(f"Entry {index}."), 1)

                    for index in (1, 2, 3, 4):
                        if index in (1, 2):
                            self.assertEqual(rpc(index, "mission_write", name="unrelated",
                                                 content=body + "\nUnauthorized update.",
                                                 expected_revision=unrelated_revision,
                                                 request_id=f"denied-update-{index}")[0], 403)
                            self.assertEqual(rpc(index, "mission_log", name="unrelated", entry="Denied.",
                                                 request_id=f"denied-existing-log-{index}")[0], 403)
                        denied = ("outside", "Sample", "../sample") if index in (1, 2) else ("sample",)
                        for name in denied:
                            with self.subTest(client=index, name=name):
                                self.assertEqual(rpc(index, "mission_write", name=name, content=body,
                                                     request_id=f"denied-{index}", create=True)[0], 403)
                                self.assertEqual(rpc(index, "mission_log", name=name, entry="Denied.",
                                                     request_id=f"denied-log-{index}")[0], 403)
                    status, data = rpc(0, "read", relative="Missions/unrelated/mission.md")
                    self.assertEqual(status, 200)
                    self.assertEqual(data["result"]["text"], body)
                    self.assertEqual(data["result"]["revision"], unrelated_revision)
                    self.assertEqual(rpc(0, "read", relative="Missions/unrelated/log.md")[0], 404)
                    self.assertEqual(rpc(0, "read", relative="Missions/outside/mission.md")[0], 404)
                    self.assertEqual(rpc(0, "read", relative="Missions/outside/log.md")[0], 404)
                finally:
                    server.shutdown()
                    worker.join()
