# SIOS Core

SIOS Core is an experimental, self-hosted Python service for Brain, Memory,
Mission and bounded agent-thread records. It keeps authoritative Markdown
under a configured data root and uses SQLite for operation receipts. The
`aios` Python module name remains for compatibility; the package and command
are `sios-core` and `sios`.

This repository contains source code, synthetic tests and generic deployment
templates. It contains no instance configuration, credentials, certificates,
personal notes, Mission content or runtime data. Keep those outside the Git
checkout. The application is a prototype and has not been qualified for a
production deployment.

## Install and use

Python 3.12 or newer is required. Create a virtual environment, install the
project with the optional features you need, then run `sios --help`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[agent,index,backup,verification]'
.venv/bin/sios --help
```

Create a private configuration with `sios setup --help`. Point its data and
cache roots outside this repository. The server accepts private-network
connections only with TLS and scoped credentials. The local MCP adapter can
also run in a client environment with an authorized configuration. A remote
JSON RPC endpoint is not an HTTP MCP endpoint.

Memory writes use stable request IDs and revision checks. Basic Memory indexes
only the configured Memory source; the Markdown note remains authoritative.
An index timeout does not undo a completed write. Re-read the note and its
receipt before retrying. Selective `memory_capture` accepts an already reviewed
claim; it does not extract a conversation or inspect prose for secrets.
Agent threads require an explicit server-side ACL and device scopes and are
disabled by default.

See [domain boundaries](docs/DOMAIN_BOUNDARIES.md),
[conversation capture](docs/CONVERSATION_CAPTURE.md),
[agent threads](docs/AGENT_THREADS_L0.md), and
[deployment guidance](docs/FIRST_SLICE_DEPLOYMENT.md).

## Publishing boundary

Only source, generic documentation, synthetic tests and templates belong in
this repository. Runtime state, backups, logs, local MCP configuration,
developer guidance containing local paths, exports and evidence bundles stay
outside it. Run `python3 scripts/check_public_repo.py` and a secret scanner
against the full Git history before publishing. This check is a guardrail,
not a guarantee that arbitrary new prose is safe to disclose.
