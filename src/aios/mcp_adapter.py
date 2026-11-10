"""Optional MCP stdio surface for the bounded AIOS service operations."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from .client import Client, RemoteError
from .basic_memory import BasicMemoryError
from .config import Config, initialize
from .server import ALL_SCOPES
from .service import dispatch
from .store import ConflictError, NotFoundError, Store, ValidationError, RecoveryError
from .agent_threads import ExpiredError, NotEnabledError
from .client_ready_proof import CaptureReadiness
from .successor_mcp_bridge import SuccessorBridge


def run(cfg: Config, ca_file: Path | None = None) -> None:
    """Serve explicit tools on stdio. The MCP dependency is optional until run."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    try:
        if cfg.mode == "client":
            remote = Client(cfg, ca_file=ca_file)
            store = None
        else:
            initialize(cfg)
            store = Store(cfg.data_root, cfg.timezone)
            remote = None
    except Exception:
        raise RuntimeError("mcp_initialization_failed") from None

    mcp = MCPServer("SIOS", instructions="Bounded personal knowledge tools. Writes require a stable request_id.")
    open_world = False  # Tools access one configured private instance.
    read_hint = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=open_world)
    write_hint = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                 idempotentHint=True, openWorldHint=open_world)
    receipt_hint = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                   idempotentHint=True, openWorldHint=open_world)

    def call(operation: str, params: dict[str, Any]) -> Any:
        try:
            if remote is not None:
                return remote.call(operation, params)
            assert store is not None
            return dispatch(store, operation, params, actor="local",
                            scopes=ALL_SCOPES)
        except RemoteError as exc:
            code = exc.code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", exc.code) else "remote_error"
            raise RuntimeError(code) from None
        except NotFoundError:
            raise RuntimeError("not_found") from None
        except NotEnabledError:
            raise RuntimeError("not_enabled") from None
        except ExpiredError:
            raise RuntimeError("expired") from None
        except ConflictError:
            raise RuntimeError("conflict") from None
        except RecoveryError:
            raise RuntimeError("recovery_required") from None
        except BasicMemoryError:
            raise RuntimeError("historical_index_unavailable") from None
        except (ValidationError, ValueError, TypeError):
            raise RuntimeError("invalid_request") from None
        except PermissionError:
            raise RuntimeError("forbidden") from None
        except OSError:
            raise RuntimeError("storage_unavailable") from None
        except Exception:
            raise RuntimeError("internal_error") from None

    # Observation calls have their own bounded transport. A slow proof must
    # not change the normal client's timeout or prevent a committed capture.
    if remote is not None:
        try:
            proof_call = Client(cfg, ca_file=ca_file, timeout=6).call
        except Exception:
            def proof_call(operation: str, params: dict[str, Any]) -> Any:
                raise RuntimeError("proof_client_unavailable") from None
    else:
        proof_call = call
    ready_proof = CaptureReadiness(proof_call, timeout=15)
    from .bounded_body_proof import BodyProof
    body_proof = BodyProof(proof_call, timeout=15)
    successor_bridge = SuccessorBridge(call)

    @mcp.tool(description="Read one allowed Markdown document or run metadata by relative path.", annotations=read_hint)
    def read(relative: str) -> Any:
        return call("read", {"relative": relative})

    @mcp.tool(annotations=read_hint)
    def read_revision(relative: str) -> Any:
        """Read a Mission Markdown SHA-256 and byte count without its body."""
        return call("read_revision", {"relative": relative})

    @mcp.tool(description="Search one knowledge store: brain, memory, or missions.", annotations=read_hint)
    def search(store: str, query: str) -> Any:
        return call("search", {"store": store, "query": query})

    @mcp.tool(description="List bounded Mission bodies or noncanonical Brain review paths with existing read scope.", annotations=read_hint)
    def catalog(store: str) -> Any:
        return call("catalog", {"store": store})

    @mcp.tool(description="Get completed runs for a routine.", annotations=read_hint)
    def history(routine: str) -> Any:
        return call("history", {"routine": routine})

    @mcp.tool(description="Check the configured instance health.", annotations=read_hint)
    def health() -> Any:
        return call("health", {})

    @mcp.tool(description="Report this connection's authenticated principal and granted scopes; local mode reports local. No token or credential material is returned.", annotations=read_hint)
    def identity() -> Any:
        return call("identity", {})

    @mcp.tool(description="Search the isolated historical Basic Memory index, if configured; indexing may lag committed files.", annotations=read_hint)
    def memory_index_search(query: str) -> Any:
        return call("memory_index_search", {"query": query})

    @mcp.tool(description="Search indexed Memory and verify full sources and referenced notes. Index coverage and current state remain unknown; reads can trigger server recovery or index synchronization.", annotations=read_hint)
    def memory_search_verified(query: str, max_reads: int = 6) -> Any:
        from .search_resolver import resolve_search_verified
        from mcp.server.mcpserver.exceptions import ToolError
        status = call("health", {})
        if not isinstance(status, dict) or status.get("ok") is not True:
            raise RuntimeError("startup_health_failed")
        def read_verified(path: str) -> Any:
            try:
                return call("read", {"relative": path})
            except RuntimeError as exc:
                raise ToolError(str(exc)) from None
        return resolve_search_verified(
            query,
            lambda value: call("memory_index_search", {"query": value}),
            read_verified,
            max_reads=max_reads,
        )

    @mcp.tool(description="Show freshness of the configured historical index, if configured.", annotations=read_hint)
    def memory_index_status() -> Any:
        return call("memory_index_status", {})

    @mcp.tool(description="Observe exact-stem search visibility and source checksum of an already committed selective note. Reads may invoke recovery or synchronization. This does not prove complete index coverage or an atomic snapshot.", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=open_world))
    async def memory_capture_readiness(path: str, revision: str) -> Any:
        return await asyncio.to_thread(ready_proof.observe, path, revision)

    @mcp.tool(description="Check one body marker query against an exact committed selective note and its full source. Does not prove completeness; marker is sent to search, so do not supply secrets. Underlying calls may synchronize or recover state.", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=open_world))
    async def memory_capture_body_readiness(path: str, revision: str, marker: str) -> Any:
        return await asyncio.to_thread(body_proof.check, path, revision, marker)

    @mcp.tool(description="Inspect declared successor evidence using existing read scope. Current state and index coverage remain unknown.", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=open_world))
    async def read_successor_evidence(predecessor_path: str) -> Any:
        return await successor_bridge.inspect(predecessor_path)

    @mcp.tool(description="Explicit literal Memory source scan and bounded full-source verification. Scanning can lock the store and recover pending operations; do not use for unresolved operations. Complete coverage is unknown.", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=open_world))
    def memory_literal_verified(query: str, max_reads: int = 6) -> Any:
        from .literal_search import verify_literal
        from mcp.server.mcpserver.exceptions import ToolError
        status = call("health", {})
        if not isinstance(status, dict) or status.get("ok") is not True or status.get("pending_operations") != 0:
            raise ToolError("startup_or_pending_unconfirmed")
        return verify_literal(query, lambda q: call("search", {"store": "memory", "query": q}),
                              lambda path: call("read", {"relative": path}), max_hits=max_reads)

    @mcp.tool(description="Catch up the isolated index for a committed Memory note and verify its exact revision and full source. Identifier must be the canonical note filename stem, without a path or .md extension. A timeout never rolls back the write.", annotations=write_hint)
    async def memory_index_refresh(identifier: str, expected_revision: str,
                                   query: str, timeout: int = 30) -> Any:
        from .identifiers import portable_component
        from mcp.server.mcpserver.exceptions import ToolError
        try:
            valid = portable_component(identifier) == identifier
        except (TypeError, ValueError):
            valid = False
        if not valid:
            # Never include the submitted string in the error or send an RPC.
            raise ToolError("invalid_identifier: use the canonical note filename stem without a path or .md extension") from None
        return await asyncio.to_thread(call, "memory_index_refresh", {"identifier": identifier,
            "expected_revision": expected_revision, "query": query, "timeout": timeout})

    @mcp.tool(description="Prepare an exact Brain proposal with provenance and base revision; does not change canonical knowledge.", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def brain_propose(target: str, content: str, request_id: str,
                      sources: list[dict[str, str]], basis: str = "unknown",
                      expected_revision: str | None = None) -> Any:
        return call("brain_propose", {"target": target, "content": content, "request_id": request_id,
                                       "sources": sources, "basis": basis, "expected_revision": expected_revision})

    @mcp.tool(description="Review an immutable Brain proposal's exact diff, provenance and stale-base status. Owner approval uses a separate administrative path.", annotations=read_hint)
    def brain_review(proposal_id: str) -> Any:
        return call("brain_review", {"proposal_id": proposal_id})

    if remote is not None:
        @mcp.tool(description=(
            "Capture one selected reusable Memory delta through the configured central controlled writer, "
            "if that deployment permits memory_capture. Requires existing read and memory:write scopes; "
            "never writes Brain. request_json is one strict JSON object, at most 65536 UTF-8 bytes. "
            "Do not transmit excluded content; a payload-free skip is {\"retain\":false} or {\"kind\":\"transient\"}. "
            "For capture use retain=true, kind=delta and required fields: request_id, meaning_id, claim, "
            "source_type, source_actor, source_reference, project, conditions (list of strings), outcome, "
            "evidence_status, recorded_at, retrieval_cue. Optional: person, task, occurred_at, verified_at, "
            "supersedes, mission_reference, evidence_instance, equivalence_of. source_type/evidence_status: "
            "direct_user/stated; observed/observed or verified; external/stated or unverified; "
            "assistant_inference/unverified. Use timezone-aware ISO dates; verified_at only for verified. "
            "References are allowed central-relative files, HTTPS or codex://threads/<canonical UUID>. "
            "Keep request_id and recorded_at unchanged after uncertain delivery; reread committed path/revision. "
            "meaning_id/evidence_instance/project/person/task use lowercase letters, digits, hyphen or underscore. "
            "An independently new event needs distinct evidence_instance; judged derivative wording needs "
            "equivalence_of=<existing selective note ID>; corrections preserve history through supersedes. "
            "source_actor is attributed evidence, not authenticated submitting identity. No root/store/actor, "
            "path override, expected_revision or owner_authorized field is accepted. "
            "Automatic selection and semantic equivalence remain the conversational agent's responsibility."
        ), annotations=write_hint)
        def memory_capture(request_json: str) -> Any:
            from mcp.server.mcpserver.exceptions import ToolError
            from .selective_memory import parse_capture_json

            try:
                data = parse_capture_json(request_json)
            except (ValueError, UnicodeError):
                # The MCP error is static: never echo submitted note content.
                raise ToolError("invalid_request: malformed capture JSON") from None
            if data.get("retain") is True and data.get("kind") == "delta":
                conditions = data.get("conditions")
                if (not isinstance(conditions, list) or
                        any(not isinstance(item, str) for item in conditions)):
                    raise ToolError("invalid_request: conditions must be a list of strings") from None
                if len(conditions) > 12 or any(len(item) > 300 or not item.strip() or "\x00" in item for item in conditions):
                    raise ToolError("invalid_conditions: use at most 12 nonempty entries of at most 300 characters; no request was sent") from None
                if len({item.strip() for item in conditions}) != len(conditions):
                    raise ToolError("invalid_conditions: entries must be distinct; no request was sent") from None
            return ready_proof.review(call("memory_capture", {"request_json": request_json}))

        @mcp.tool(description="Create one bounded project discussion. Requires an enabled server-side thread ACL and threads:create plus threads:read scopes. Participant IDs must be authenticated configured principals with the same project grant.", annotations=write_hint)
        def thread_create(project: str, purpose: str, participants: list[str], request_id: str,
                          max_messages: int = 12, expires_at: str | None = None) -> Any:
            return call("thread_create", {"project": project, "purpose": purpose,
                                          "participants": participants, "request_id": request_id,
                                          "max_messages": max_messages, "expires_at": expires_at})

        @mcp.tool(description="Append one untrusted message to an authorized open discussion. Does not wake or control a desktop conversation. Retry the exact request ID and payload after uncertain delivery.", annotations=write_hint)
        def thread_post(thread_id: str, body: str, kind: str, request_id: str,
                        parent_message_id: str | None = None,
                        target_participant_ids: list[str] | None = None,
                        expected_revision: int | None = None) -> Any:
            return call("thread_post", {"thread_id": thread_id, "body": body, "kind": kind,
                                        "request_id": request_id, "parent_message_id": parent_message_id,
                                        "target_participant_ids": target_participant_ids,
                                        "expected_revision": expected_revision})

        @mcp.tool(description="List only discussions visible to the authenticated principal in one granted project.", annotations=read_hint)
        def thread_list(project: str, limit: int = 20, after_thread_id: str | None = None) -> Any:
            return call("thread_list", {"project": project, "limit": limit,
                                        "after_thread_id": after_thread_id})

        @mcp.tool(description="Read a bounded page of untrusted discussion messages visible to the authenticated principal. Reading alone does not record a fetched receipt, acknowledge or act on messages.", annotations=read_hint)
        def thread_read(thread_id: str, after_seq: int = 0, limit: int = 20) -> Any:
            return call("thread_read", {"thread_id": thread_id, "after_seq": after_seq,
                                        "limit": limit})

        @mcp.tool(description="After a successful thread_read, explicitly record the highest received message sequence for this principal. Requires threads:ack and threads:read. This is not acknowledgement, action or reply proof.", annotations=receipt_hint)
        def thread_fetch(thread_id: str, through_seq: int, request_id: str) -> Any:
            return call("thread_fetch", {"thread_id": thread_id, "through_seq": through_seq,
                                         "request_id": request_id})

        @mcp.tool(description="Record only the authenticated principal's acknowledgement cursor. This is not proof of action or reply.", annotations=write_hint)
        def thread_ack(thread_id: str, through_seq: int, request_id: str) -> Any:
            return call("thread_ack", {"thread_id": thread_id, "through_seq": through_seq,
                                       "request_id": request_id})

        @mcp.tool(description="Owner-only pause, resume or close of a discussion with exact revision check. This does not control any vendor desktop session.", annotations=write_hint)
        def thread_close(thread_id: str, action: str, expected_revision: int,
                         request_id: str) -> Any:
            return call("thread_close", {"thread_id": thread_id, "action": action,
                                         "expected_revision": expected_revision,
                                         "request_id": request_id})

        @mcp.tool(description="Owner-only removal of one participant from a discussion. Immediately denies that principal's future list, read, post and acknowledgement for this thread; does not revoke its other device capabilities.", annotations=write_hint)
        def thread_revoke(thread_id: str, participant: str, expected_revision: int,
                          request_id: str) -> Any:
            return call("thread_revoke", {"thread_id": thread_id,
                                          "participant": participant,
                                          "expected_revision": expected_revision,
                                          "request_id": request_id})

    @mcp.tool(description="Create or replace a memory note with a stable request ID.", annotations=write_hint)
    def memory_write(identifier: str, content: str, request_id: str,
                     expected_revision: str | None = None, create: bool = False) -> Any:
        return call("memory_write", {"identifier": identifier, "content": content,
                                     "request_id": request_id, "expected_revision": expected_revision,
                                     "create": create})

    @mcp.tool(description="Create or replace a structured mission with a stable request ID.", annotations=write_hint)
    def mission_write(name: str, content: str, request_id: str,
                      expected_revision: str | None = None, create: bool = False) -> Any:
        return call("mission_write", {"name": name, "content": content,
                                      "request_id": request_id, "expected_revision": expected_revision,
                                      "create": create})

    @mcp.tool(description="Request central completion using retained verification; no worker approval or registry fields.", annotations=write_hint)
    def mission_complete(name: str, content: str, request_id: str,
                         verification_id: str, expected_revision: str) -> Any:
        return call('mission_complete', {'name':name, 'content':content, 'request_id':request_id,
            'verification_id':verification_id, 'expected_revision':expected_revision})

    @mcp.tool(description="Append a mission log entry with a stable request ID.", annotations=write_hint)
    def mission_log(name: str, entry: str, request_id: str, day: str | None = None) -> Any:
        return call("mission_log", {"name": name, "entry": entry,
                                    "request_id": request_id, "day": day})

    @mcp.tool(description="Publish a run report and journal entry with a stable request ID.", annotations=write_hint)
    def publish(routine: str, run_id: str, report: str, summary: str, status: str,
                request_id: str, items: list[str] | None = None,
                started_at: str | None = None) -> Any:
        return call("publish", {"routine": routine, "run_id": run_id,
                                "report": report, "summary": summary, "status": status,
                                "request_id": request_id, "items": items, "started_at": started_at})

    mcp.run(transport="stdio")
