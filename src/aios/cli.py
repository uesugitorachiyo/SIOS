"""Portable setup and administration; never derives state from the source tree."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
from pathlib import Path
import sys
import uuid

from .client import Client, RemoteError
from .config import Config, create_config, initialize
from .server import ALL_SCOPES, device_from_env, load_devices, make_server
from .service import dispatch
from .store import Store


def parser():
    root = argparse.ArgumentParser(prog="sios")
    root.add_argument("--config", type=Path, help="Private instance JSON outside the checkout")
    root.add_argument("--ca-file", type=Path, help="Trust a private server CA without disabling TLS validation")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("new-request-id")
    setup = commands.add_parser("setup")
    setup.add_argument("--mode", choices=("standalone", "server", "client"), required=True)
    setup.add_argument("--data-root", type=Path)
    setup.add_argument("--cache-root", type=Path)
    setup.add_argument("--timezone", default="UTC")
    setup.add_argument("--endpoint")
    setup.add_argument("--instance-id", help="Existing server UUID required for client mode")
    setup.add_argument("--credential-env", default="SIOS_AUTH_TOKEN", help="Credential reference only; never the token value")
    commands.add_parser("health")
    read = commands.add_parser("read")
    read.add_argument("relative")
    search = commands.add_parser("search")
    search.add_argument("store", choices=("brain", "memory", "missions"))
    search.add_argument("query")
    history = commands.add_parser("history")
    history.add_argument("routine")
    for command in ("memory-write", "mission-write"):
        write = commands.add_parser(command)
        write.add_argument("--name", required=True)
        write.add_argument("--file", type=Path, required=True)
        write.add_argument("--request-id", required=True)
        mode = write.add_mutually_exclusive_group(required=True)
        mode.add_argument("--create", action="store_true")
        mode.add_argument("--expected-revision")
    log = commands.add_parser("mission-log")
    log.add_argument("--name", required=True)
    log.add_argument("--file", type=Path, required=True)
    log.add_argument("--request-id", required=True)
    log.add_argument("--day")
    complete = commands.add_parser("mission-complete", help="Submit identifiers to the authenticated central completion consumer")
    for name in ("name", "request-id", "verification-id", "expected-revision"):
        complete.add_argument('--' + name, required=True)
    complete.add_argument('--file', type=Path, required=True)
    publish = commands.add_parser("publish")
    for name in ("routine", "run-id", "summary", "status", "request-id"):
        publish.add_argument("--" + name, required=True)
    publish.add_argument("--report-file", required=True, type=Path)
    publish.add_argument("--items-file", type=Path)
    publish.add_argument("--started-at")
    serve = commands.add_parser("serve")
    serve.add_argument("--bind", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--devices", type=Path, help="Private device identities/scopes/token environment references")
    serve.add_argument("--thread-acl", type=Path, help="Opt-in private principal-to-project thread ACL")
    serve.add_argument("--token-env", default="SIOS_AUTH_TOKEN", help="Single device mode; token stays in the environment")
    serve.add_argument("--cert", type=Path)
    serve.add_argument("--key", type=Path)
    serve.add_argument("--completion-validation", action="store_true", help="Isolated central validation only; disabled by default")
    commands.add_parser("mcp", help="Run optional stdio MCP adapter")
    index_setup = commands.add_parser("index-setup", help="Configure a separate local Basic Memory index")
    index_setup.add_argument("--executable", default="basic-memory")
    index_setup.add_argument("--index-root", type=Path)
    commands.add_parser("index-status")
    index_search = commands.add_parser("index-search")
    index_search.add_argument("query")
    index_wait = commands.add_parser("index-wait", help="Bounded scoped index catch-up through read-only MCP")
    index_wait.add_argument("query")
    index_wait.add_argument("--timeout", type=int, default=30)
    propose = commands.add_parser("brain-propose")
    propose.add_argument("--target", required=True)
    propose.add_argument("--file", type=Path, required=True)
    propose.add_argument("--sources-file", type=Path, required=True)
    propose.add_argument("--basis", choices=("stated", "observed", "inferred", "unknown"), default="unknown")
    propose.add_argument("--expected-revision")
    propose.add_argument("--request-id", required=True)
    review = commands.add_parser("brain-review")
    review.add_argument("--proposal", required=True)
    approve = commands.add_parser("brain-approve", help="Owner terminal review; never exposed as an agent tool")
    approve.add_argument("--proposal", required=True)
    approve.add_argument("--request-id", required=True)
    snapshot = commands.add_parser("backup", help="Encrypted local snapshot; key remains outside AIOS")
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--key-env", default="SIOS_BACKUP_KEY")
    restore = commands.add_parser("restore", help="Restore into an absent private target only")
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True)
    restore.add_argument("--key-env", default="SIOS_BACKUP_KEY")
    return root


def file_text(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > 2_000_000:
        raise ValueError("Input exceeds the 2 MB limit")
    return raw.decode("utf-8")


def operation_params(args):
    if args.command == 'mission-complete':
        return 'mission_complete', {'name':args.name, 'content':file_text(args.file),
            'request_id':args.request_id, 'verification_id':args.verification_id,
            'expected_revision':args.expected_revision}
    if args.command == "brain-propose":
        return "brain_propose", {"target": args.target, "content": file_text(args.file),
                                 "sources": json.loads(file_text(args.sources_file)), "basis": args.basis,
                                 "expected_revision": args.expected_revision, "request_id": args.request_id}
    if args.command == "brain-review":
        return "brain_review", {"proposal_id": args.proposal}
    if args.command == "index-search":
        return "memory_index_search", {"query": args.query}
    if args.command == "index-status":
        return "memory_index_status", {}
    if args.command in ("health",):
        return args.command, {}
    if args.command == "read":
        return "read", {"relative": args.relative}
    if args.command == "search":
        return "search", {"store": args.store, "query": args.query}
    if args.command == "history":
        return "history", {"routine": args.routine}
    if args.command in ("memory-write", "mission-write"):
        key = "identifier" if args.command == "memory-write" else "name"
        return args.command.replace("-", "_"), {
            key: args.name, "content": file_text(args.file), "request_id": args.request_id,
            "create": args.create, "expected_revision": args.expected_revision,
        }
    if args.command == "mission-log":
        return "mission_log", {"name": args.name, "entry": file_text(args.file),
                               "request_id": args.request_id, "day": args.day}
    if args.command == "publish":
        items = json.loads(file_text(args.items_file)) if args.items_file else None
        return "publish", {"routine": args.routine, "run_id": args.run_id,
                           "report": file_text(args.report_file), "summary": args.summary,
                           "status": args.status, "request_id": args.request_id,
                           "items": items, "started_at": args.started_at}
    raise ValueError("Unsupported command")


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command == "new-request-id":
        print(uuid.uuid4())
        return 0
    if args.config is None and args.command != "restore":
        p.error("--config is required; private state never defaults to the checkout")
    try:
        if args.command in ("backup", "restore"):
            try:
                from .backup import backup, restore
            except ImportError as exc:
                raise ValueError("Install the backup extra before encrypted backup/restore") from exc
            try:
                key = base64.b64decode(os.environ.get(args.key_env, ""), validate=True)
                if len(key) != 32:
                    raise ValueError()
            except ValueError as exc:
                raise ValueError("Backup key environment variable must contain a base64-encoded 32-byte key") from exc
            if args.command == "restore":
                result = restore(args.source, args.target, key)
            else:
                cfg = Config.load(args.config)
                if cfg.mode == "client":
                    raise ValueError("Authoritative backup must run on the server or standalone host")
                initialize(cfg)
                result = backup(Store(cfg.data_root, cfg.timezone), args.output, key)
            print(json.dumps(result, default=str, indent=2))
            return 0
        if args.command == "setup":
            cfg = create_config(args.config, args.mode, args.data_root, args.timezone, args.endpoint, args.instance_id, token_env=args.credential_env, cache_root=args.cache_root)
            result = initialize(cfg)
            result["instance_id"] = cfg.instance_id
            print(json.dumps(result, default=str, indent=2))
            return 0
        cfg = Config.load(args.config)
        if args.command == "brain-approve":
            if not sys.stdin.isatty():
                raise ValueError("Owner approval requires interactive terminal review")
            if cfg.mode == "client":
                remote = Client(cfg, ca_file=args.ca_file)
                reviewed = remote.call("brain_review", {"proposal_id": args.proposal})
            else:
                initialize(cfg)
                store = Store(cfg.data_root, cfg.timezone)
                reviewed = dispatch(store, "brain_review", {"proposal_id": args.proposal}, actor="local", scopes=ALL_SCOPES)
            if reviewed["stale"]:
                raise ValueError("Proposal is stale; reread and prepare a new proposal")
            print(reviewed["diff"])
            print(json.dumps({key: reviewed[key] for key in ("sources", "basis", "warnings", "approval_effect")}, ensure_ascii=False, indent=2))
            confirmation = "approve " + reviewed["proposal_revision"]
            if input("Type " + confirmation + " to approve: ") != confirmation:
                raise ValueError("Owner approval cancelled")
            params = {"proposal_id": args.proposal, "proposal_revision": reviewed["proposal_revision"], "request_id": args.request_id}
            if cfg.mode == "client":
                result = remote.call("brain_apply", params)
            else:
                result = dispatch(store, "brain_apply", params, actor="local", scopes=ALL_SCOPES|{"brain:approve"})
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command in ("index-setup", "index-wait"):
            from .index_binding import setup_index, load_index
            if cfg.mode == "client":
                raise ValueError("Index administration runs on the authoritative host")
            initialize(cfg)
            if args.command == "index-setup":
                executable = shutil.which(args.executable)
                if not executable:
                    raise ValueError("Basic Memory executable is unavailable")
                result = setup_index(cfg, str(Path(executable).absolute()), args.index_root)
            else:
                result = asyncio.run(load_index(cfg.data_root).wait_ready(args.query, timeout=args.timeout))
            print(json.dumps(result, default=str, ensure_ascii=False, indent=2))
            return 0
        if args.command == "serve":
            if cfg.mode != "server":
                raise ValueError("serve requires server mode")
            initialize(cfg)
            devices = load_devices(args.devices) if args.devices else [device_from_env("client", args.token_env, ALL_SCOPES)]
            principals = {device.identity: device.scopes for device in devices}
            thread_projects = None
            if args.thread_acl is not None:
                if args.devices is None:
                    raise ValueError('Thread pilot requires explicit device identities')
                from .agent_threads import load_project_acl
                thread_projects = load_project_acl(args.thread_acl, principals)
            server = make_server(Store(cfg.data_root, cfg.timezone, completion_validation=args.completion_validation,
                                       thread_projects=thread_projects,
                                       thread_principals=principals if thread_projects is not None else None), cfg.instance_id, devices,
                                 bind=args.bind, port=args.port, cert=args.cert, key=args.key)
            scheme = "https" if args.cert else "http"
            address = server.server_address
            host = f"[{address[0]}]" if ":" in address[0] else address[0]
            print(json.dumps({"endpoint": f"{scheme}://{host}:{address[1]}", "instance_id": cfg.instance_id}), flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        if args.command == "mcp":
            from .mcp_adapter import run
            run(cfg, ca_file=args.ca_file)
            return 0
        operation, params = operation_params(args)
        if cfg.mode == "client":
            result = Client(cfg, ca_file=args.ca_file).call(operation, params)
        else:
            initialize(cfg)
            result = dispatch(Store(cfg.data_root, cfg.timezone), operation, params,
                              actor="local", scopes=ALL_SCOPES)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except RemoteError as exc:
        body = {"error": exc.code, "http_status": exc.status}
        if hasattr(args, "request_id"):
            body.update(request_id=args.request_id, acknowledged=False)
        print(json.dumps(body), file=sys.stderr)
        return 2
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
