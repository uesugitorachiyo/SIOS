"""Authenticated private HTTP adapter. Unencrypted traffic is loopback-only."""
from __future__ import annotations

from dataclasses import dataclass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
from typing import Any

from .service import dispatch
from .agent_threads import ExpiredError, NotEnabledError
from .basic_memory import BasicMemoryError
from .store import Store, ConflictError, ValidationError, RecoveryError

MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
ALL_SCOPES = {"read", "memory:write", "missions:write", "brain:propose"}
SUPPORTED_SCOPES = ALL_SCOPES | {"brain:approve", "completion:control", "completion:evidence", "completion:report", "completion:approve", "missions:complete",
                                 "release:control", "release:evidence", "release:report", "release:approve", "release:check",
                                 "threads:create", "threads:post", "threads:read", "threads:ack", "threads:manage",
                                 "memory:capture", "memory:index_refresh"}


@dataclass(frozen=True)
class Device:
    identity: str
    token: str
    scopes: frozenset[str]


def device_from_env(identity: str, token_env: str, scopes: set[str]) -> Device:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", identity):
        raise ValueError("Invalid device identity")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        raise ValueError("Invalid token environment variable")
    token = os.environ.get(token_env, "")
    if len(token) < 32 or len(token) > 4096 or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("Device token must be 32–4096 printable ASCII characters without whitespace")
    if not scopes or not scopes <= SUPPORTED_SCOPES:
        raise ValueError("Invalid device scopes")
    return Device(identity, token, frozenset(scopes))


def load_devices(path: Path) -> list[Device]:
    """Read references only; actual tokens are resolved from the server environment."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("Device configuration requires a nonempty list")
    devices = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"identity", "token_env", "scopes"}:
            raise ValueError("Device entries require identity, token_env and scopes")
        if not isinstance(entry["scopes"], list):
            raise ValueError("Device scopes must be a list")
        devices.append(device_from_env(entry["identity"], entry["token_env"], set(entry["scopes"])))
    if len({d.identity for d in devices}) != len(devices) or len({d.token for d in devices}) != len(devices):
        raise ValueError("Device identities and tokens must be distinct")
    return devices


def make_server(store: Store, instance_id: str, devices: list[Device], *,
                bind: str = "127.0.0.1", port: int = 0,
                cert: Path | None = None, key: Path | None = None,
                dispatcher=dispatch) -> ThreadingHTTPServer:
    address = ipaddress.ip_address(bind)
    if not (address.is_loopback or address.is_private) or address.is_unspecified:
        raise ValueError("Bind to an explicit loopback or private address")
    if not devices or len({d.token for d in devices}) != len(devices):
        raise ValueError("Distinct authenticated devices are required")
    if bool(cert) != bool(key) or (not address.is_loopback and not (cert and key)):
        raise ValueError("Private network binding requires a TLS certificate and key")
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Invalid port")

    class Handler(BaseHTTPRequestHandler):
        server_version = "SIOS"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            # No request body, address, credential or private path logs.
            pass

        def respond(self, code: int, body: dict[str, Any]):
            content = json.dumps(body, ensure_ascii=False).encode("utf-8")
            if len(content) > MAX_RESPONSE:
                code = 413
                content = b'{"error":"response_too_large"}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            if self.path != "/v1/rpc":
                self.respond(404, {"error": "not_found"})
                return
            authorization = self.headers.get("Authorization", "")
            token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
            device = None
            for candidate in devices:
                if hmac.compare_digest(candidate.token.encode("ascii"), token.encode("utf-8")):
                    device = candidate
            if device is None:
                self.respond(401, {"error": "unauthorized"})
                return
            try:
                if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                    raise ValidationError("JSON with Content-Length is required")
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise ValidationError("One valid Content-Length is required")
                length = int(lengths[0])
                if length <= 0 or length > MAX_BODY:
                    self.respond(413, {"error": "payload_too_large"})
                    return
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValidationError("Incomplete request")
                def unique(pairs):
                    value = {}
                    for name, item in pairs:
                        if name in value:
                            raise ValidationError('Duplicate JSON field')
                        value[name] = item
                    return value
                envelope = json.loads(raw, object_pairs_hook=unique)
                if not isinstance(envelope, dict) or set(envelope) != {"instance_id", "operation", "params"}:
                    raise ValidationError("Invalid request envelope")
                if envelope["instance_id"] != instance_id:
                    self.respond(409, {"error": "wrong_instance"})
                    return
                result = dispatcher(store, envelope["operation"], envelope["params"],
                                  actor=device.identity, scopes=set(device.scopes))
                self.respond(200, {"result": result, "instance_id": instance_id})
            except PermissionError:
                self.respond(403, {"error": "forbidden"})
            except NotEnabledError:
                self.respond(403, {"error": "not_enabled"})
            except ExpiredError:
                self.respond(409, {"error": "expired"})
            except ConflictError:
                self.respond(409, {"error": "conflict", "message": "Reread the record and reconcile; preserve the original request ID for retries."})
            except (ValidationError, json.JSONDecodeError, UnicodeError, TypeError):
                self.respond(400, {"error": "invalid_request"})
            except FileNotFoundError:
                self.respond(404, {"error": "not_found"})
            except RecoveryError:
                self.respond(503, {"error": "recovery_required"})
            except BasicMemoryError:
                self.respond(503, {"error": "historical_index_unavailable"})
            except (OSError, ValueError):
                self.respond(503, {"error": "storage_unavailable"})
            except Exception:
                self.respond(500, {"error": "internal_error"})

        def do_GET(self):
            self.respond(405, {"error": "method_not_allowed"})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET

        def handle_error(self, request, client_address):
            # Failed handshakes/disconnects are expected. Avoid default traceback
            # output containing client addresses and local deployment paths.
            error = sys.exc_info()[1]
            if not isinstance(error, OSError):
                print("SIOS connection handler failed", file=sys.stderr)

    server = Server((bind, port), Handler)
    if cert and key:
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certfile=cert, keyfile=key)
            # Negotiate in the worker after installing its socket timeout.
            # A silent TCP peer must not stall the accept loop.
            server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        except Exception:
            server.server_close()
            raise
    return server
