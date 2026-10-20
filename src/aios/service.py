"""Bounded JSON service shared by the HTTP adapter and local CLI.

There is deliberately no raw filesystem write operation or canonical Brain
mutation. Actor identity comes from a trusted adapter, never from parameters.
"""
from __future__ import annotations

import inspect
from typing import Any

from .store import ConflictError, RecoveryError, Store, ValidationError


OPERATIONS = {
    "read": ("read", "read"),
    "search": ("search", "read"),
    "catalog": ("catalog", "read"),
    "health": ("health", "read"),
    "history": ("history", "read"),
    "memory_write": ("write_memory", "memory:write"),
    "memory_capture": ("capture", "memory:write"),
    "mission_write": ("mission_write", "missions:write"),
    "mission_log": ("mission_log", "missions:write"),
    "completion_register": ("completion_register", "completion:control"),
    "completion_revoke": ("completion_revoke", "completion:control"),
    "completion_evidence": ("completion_evidence", "completion:evidence"),
    "completion_report": ("completion_report", "completion:report"),
    "completion_approve": ("completion_approve", "completion:approve"),
    "mission_complete": ("mission_complete", "missions:complete"),
    "release_register": ("release_register", "release:control"),
    "release_evidence": ("release_evidence", "release:evidence"),
    "release_report": ("release_report", "release:report"),
    "release_approve": ("release_approve", "release:approve"),
    "release_revoke": ("release_revoke", "release:control"),
    "release_check": ("release_check", "release:check"),
    "publish": ("publish", "memory:write"),
    "memory_index_search": ("search", "read"),
    "memory_index_status": ("status", "read"),
    "memory_index_refresh": ("refresh", "memory:write"),
    "brain_propose": ("propose", "brain:propose"),
    "brain_review": ("review", "read"),
    "brain_apply": ("apply", "brain:approve"),
    "thread_create": ("create", "threads:create"),
    "thread_post": ("post", "threads:post"),
    "thread_list": ("list", "threads:read"),
    "thread_read": ("read", "threads:read"),
    "thread_fetch": ("fetch", "threads:ack"),
    "thread_ack": ("ack", "threads:ack"),
    "thread_close": ("close", "threads:manage"),
    "thread_revoke": ("revoke", "threads:manage"),
}


def dispatch(store: Store, operation: str, params: dict[str, Any], *,
             actor: str, scopes: set[str]) -> Any:
    if operation not in OPERATIONS:
        raise ValidationError("Unknown operation")
    method_name, scope = OPERATIONS[operation]
    if scope not in scopes:
        raise PermissionError("Operation is outside device scope")
    if operation == "memory_index_refresh" and "read" not in scopes:
        raise PermissionError("Index refresh with full-note return also requires read scope")
    if operation == "memory_capture" and "read" not in scopes:
        raise PermissionError("Selective capture requires read scope for deduplication")
    if operation.startswith("thread_") and "threads:read" not in scopes:
        raise PermissionError("Thread operations require thread read scope")
    if not isinstance(params, dict) or {"actor", "owner_authorized"} & set(params):
        raise ValidationError("Parameters must be an object without an actor override")
    if operation.startswith("brain_"):
        from .brain import BrainWorkflow
        method = getattr(BrainWorkflow(store), method_name)
    elif operation == "memory_capture":
        from .selective_memory import capture_with_store, parse_capture_json
        if set(params) != {"request_json"}:
            raise ValidationError("Invalid capture parameters")
        try:
            data = parse_capture_json(params["request_json"])
            return capture_with_store(data, store=store, actor=actor)
        except (ConflictError, RecoveryError):
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValidationError("Invalid capture request") from None
    elif operation == "memory_index_refresh":
        from .indexed_note import refresh
        return refresh(store, **params)
    elif operation in {"memory_index_search", "memory_index_status"}:
        from .index_binding import load_index
        method = getattr(load_index(store.root), method_name)
    elif operation.startswith("thread_"):
        from .agent_threads import AgentThreads
        method = getattr(AgentThreads(store), method_name)
    else:
        method = getattr(store, method_name)
    supplied = dict(params)
    if scope != "read" or operation.startswith("thread_"):
        supplied["actor"] = actor
    if scope == "brain:approve":
        supplied["owner_authorized"] = True
    try:
        inspect.signature(method).bind(**supplied)
    except TypeError as exc:
        raise ValidationError("Invalid operation parameters") from exc
    result = method(**supplied)
    if operation == "memory_index_search":
        from .index_binding import add_read_paths
        result = add_read_paths(store, result)
    if operation == "health":
        result = {key: value for key, value in result.items() if key != "data_root"}
    return result
