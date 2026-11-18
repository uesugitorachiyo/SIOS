"""Bounded loopback-only Hindsight document transport for SIOS integration.

This client does not authorize access to SIOS or start a server.  Hindsight
is a derived index: callers must authenticate and re-read the source before
using a hit, and must not infer current state from search ranking.
"""
from __future__ import annotations

import ipaddress
import json
import re
import ssl
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_MAX_RESPONSE = 8 * 1024 * 1024
_MAX_DOCUMENT = 2_000_000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalHindsight:
    """Explicit caller-managed local service; no background synchronization."""

    def __init__(self, endpoint: str, bank: str, *, timeout: float = 30, ca_file=None):
        u = urlsplit(endpoint)
        try:
            address = ipaddress.ip_address(u.hostname or "")
            port = u.port
        except ValueError as exc:
            raise ValueError("invalid endpoint") from exc
        if (u.scheme not in ("http", "https") or not address.is_loopback or port is None or
                u.username or u.password or u.path not in ("", "/") or u.query or u.fragment):
            raise ValueError("explicit loopback endpoint required")
        if not isinstance(bank, str) or not _ID.fullmatch(bank):
            raise ValueError("invalid bank")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise ValueError("invalid timeout")
        host = f"[{address}]" if address.version == 6 else str(address)
        if (u.scheme == "https") != (ca_file is not None):
            raise ValueError("HTTPS requires an explicit CA; HTTP must not supply one")
        self._base = f"{u.scheme}://{host}:{port}/v1/default/banks/{quote(bank, safe='')}"
        self._timeout = timeout
        handlers = [ProxyHandler({}), _NoRedirect()]
        if ca_file is not None:
            handlers.append(HTTPSHandler(context=ssl.create_default_context(cafile=str(ca_file))))
        self._opener = build_opener(*handlers)

    def _headers(self):
        return {"Content-Type": "application/json"}

    def _request(self, path: str, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        req = Request(self._base + path, data=body, headers=self._headers())
        with self._opener.open(req, timeout=self._timeout) as response:
            data = response.read(_MAX_RESPONSE + 1)
        if len(data) > _MAX_RESPONSE:
            raise ValueError("response exceeds limit")
        result = json.loads(data)
        if not isinstance(result, dict) or result.get("error") is not None:
            raise ValueError("unconfirmed response")
        return result

    def get(self, document_id: str) -> dict | None:
        if not isinstance(document_id, str) or not _ID.fullmatch(document_id):
            raise ValueError("invalid document id")
        try:
            value = self._request("/documents/" + quote(document_id, safe=""))
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return {"text": value.get("original_text"), "metadata": value.get("document_metadata")}

    def upsert(self, document_id: str, text: str, metadata: dict[str, str], *, tags: list[str] | None = None) -> None:
        if not isinstance(document_id, str) or not _ID.fullmatch(document_id):
            raise ValueError("invalid document id")
        if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_DOCUMENT:
            raise ValueError("invalid source text")
        if not isinstance(metadata, dict) or set(metadata) != {"source_path", "source_revision"} or not all(isinstance(v, str) for v in metadata.values()):
            raise ValueError("invalid metadata")
        if tags is not None and (not isinstance(tags, list) or any(not isinstance(tag, str) or not tag for tag in tags)):
            raise ValueError("invalid tags")
        item = {"content": text, "document_id": document_id,
                "timestamp": "unset", "metadata": metadata}
        if tags is not None:
            item["tags"] = tags
        self._request("/memories", {"items": [item], "async": False})

    def recall(self, query: str) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query.encode("utf-8")) > 4096:
            raise ValueError("invalid query")
        value = self._request("/memories/recall", {"query": query, "budget": "mid"})
        results = value.get("results")
        if not isinstance(results, list) or len(results) > 100 or any(not isinstance(row, dict) for row in results):
            raise ValueError("invalid results")
        return results

    def search_documents(self, query: str) -> dict:
        """Search stored document text, with a bounded page and explicit limits."""
        if not isinstance(query, str) or not query.strip() or len(query.encode('utf-8')) > 4096:
            raise ValueError('invalid query')
        result = self._request('/documents?' + urlencode({'q': query, 'limit': 100, 'offset': 0}))
        items = result.get('items')
        if (not isinstance(items, list) or len(items) > 100 or
                any(not isinstance(item, dict) for item in items) or
                type(result.get('total_is_estimate')) is not bool):
            raise ValueError('invalid document search')
        return {'items': items, 'has_more': result['total_is_estimate'] or len(items) == 100}


class AuthenticatedLocalHindsight(LocalHindsight):
    """Bearer-authenticated transport; server enforcement must be verified.

    The caller supplies the secret through an established secure channel. This
    class does not provision credentials, configure the server, protect its
    database, or prove that every server endpoint enforces authentication.
    """

    def __init__(self, endpoint: str, bank: str, *, token: str, timeout: float = 30, ca_file=None):
        if (not isinstance(token, str) or not 32 <= len(token) <= 512
                or not all(33 <= ord(c) <= 126 for c in token)):
            raise ValueError("invalid authentication token")
        super().__init__(endpoint, bank, timeout=timeout, ca_file=ca_file)
        self.__token = token

    def _headers(self):
        return {"Content-Type": "application/json", "Authorization": "Bearer " + self.__token}
