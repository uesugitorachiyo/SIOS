"""An identity response must reflect server-supplied authority, not tool arguments."""

import unittest
import http.client
import json
import threading

from aios.server import Device, make_server
from aios.service import dispatch
from aios.store import ValidationError


class IdentityTests(unittest.TestCase):
    def test_local_mode_is_labeled_local(self):
        result = dispatch(object(), "identity", {}, actor="local",
                          scopes={"read", "memory:write"})
        self.assertEqual(result, {"identity": "local",
                                  "scopes": ["memory:write", "read"]})

    def test_returns_only_authenticated_identity_and_scopes(self):
        result = dispatch(object(), "identity", {}, actor="nucx-1",
                          scopes={"missions:write", "read"})
        self.assertEqual(result, {"identity": "nucx-1",
                                  "scopes": ["missions:write", "read"]})

    def test_requires_read_scope(self):
        with self.assertRaises(PermissionError):
            dispatch(object(), "identity", {}, actor="nucx-claude-1",
                     scopes={"threads:post"})

    def test_rejects_caller_supplied_identity_or_scope(self):
        for params in ({"actor": "nucx-1"}, {"scopes": ["missions:write"]},
                       {"owner_authorized": True}):
            with self.subTest(params=params), self.assertRaises(ValidationError):
                dispatch(object(), "identity", params, actor="nucx-claude-1",
                         scopes={"read"})

    def test_http_identity_comes_from_authenticated_device(self):
        instance = "00000000-0000-4000-8000-000000000001"
        devices = [
            Device("nucx-1", "a" * 32, frozenset({"read", "missions:write"})),
            Device("nucx-claude-1", "b" * 32, frozenset({"read"})),
            Device("no-read", "c" * 32, frozenset({"threads:post"})),
        ]
        server = make_server(object(), instance, devices, bind="127.0.0.1", port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def call(token, params=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port,
                                                    timeout=5)
            try:
                body = json.dumps({"instance_id": instance, "operation": "identity",
                                   "params": {} if params is None else params}).encode()
                connection.request("POST", "/v1/rpc", body=body,
                                   headers={"Authorization": "Bearer " + token,
                                            "Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            self.assertEqual(call("a" * 32)[1]["result"],
                             {"identity": "nucx-1", "scopes": ["missions:write", "read"]})
            self.assertEqual(call("b" * 32)[1]["result"],
                             {"identity": "nucx-claude-1", "scopes": ["read"]})
            self.assertEqual(call("c" * 32)[0], 403)
            self.assertEqual(call("d" * 32)[0], 401)
            self.assertEqual(call("b" * 32, {"actor": "nucx-1"})[0], 400)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
