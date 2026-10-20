"""Client transport. Failure never initializes a local authoritative store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPSHandler, ProxyHandler, HTTPRedirectHandler

from .config import Config

MAX_RESPONSE = 16 * 1024 * 1024


class RemoteError(RuntimeError):
    def __init__(self, code: str, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Credentials and submissions must never follow redirects.
        return None


class Client:
    def __init__(self, config: Config, *, ca_file: Path | None = None, timeout: float = 15):
        if config.mode != "client" or not config.endpoint:
            raise ValueError("Client mode with an endpoint is required")
        self.config = config
        self.timeout = timeout
        context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context))

    def call(self, operation: str, params: dict):
        token = os.environ.get(self.config.token_env, "")
        if not 32 <= len(token) <= 4096 or not all(33 <= ord(c) <= 126 for c in token):
            raise RemoteError("credential_unavailable")
        payload = {"instance_id": self.config.instance_id, "operation": operation, "params": params}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(self.config.endpoint.rstrip("/") + "/v1/rpc", data=body,
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                          method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise RemoteError("response_too_large")
                result = json.loads(raw)
                if not isinstance(result, dict) or result.get("instance_id") != self.config.instance_id or "result" not in result:
                    raise RemoteError("invalid_response")
                return result["result"]
        except HTTPError as exc:
            try:
                code = json.loads(exc.read(8192)).get("error", "remote_error")
            except (ValueError, AttributeError):
                code = "remote_error"
            raise RemoteError(code, exc.code) from None
        except (URLError, OSError, TimeoutError):
            raise RemoteError("server_unavailable") from None
        except (ValueError, UnicodeError, AttributeError):
            raise RemoteError("invalid_response") from None
