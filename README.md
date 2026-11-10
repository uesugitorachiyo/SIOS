# SIOS Core

SIOS Core is a self-hosted Python service for Brain, Memory,
Mission and bounded agent-thread records. It keeps authoritative Markdown
under a configured data root and uses SQLite for operation receipts. The
`aios` Python module name remains for compatibility; the package and command
are `sios-core` and `sios`.

The developer uses a private local SIOS instance in ongoing work. That
instance includes private configuration, data and integrations that are not
distributed here. Operational use in one environment does not establish that
every feature or another installation is production-qualified.

This repository provides source code, synthetic tests, generic documentation
and templates for starting an independent, empty instance. It does not include
instance configuration, credentials, certificates, personal notes, Mission
content or runtime data. Keep those outside the Git checkout. The public source
is under active development; its validation is bounded and does not establish
general production readiness or equivalence to the private instance.

See [validation scope](docs/VALIDATION.md) for the public-source checks and
their limits.

## Architecture at a glance

```text
User / authorized client
          |
       SIOS Core
      /    |     \
   Brain  Memory  Missions
             |
       Optional index
```

This is a conceptual view of the public source, not a network deployment
diagram. Brain holds curated knowledge, Memory holds records, and Missions
hold work state. Configured Markdown sources remain authoritative; an optional
Memory index supports discovery. See [domain boundaries](docs/DOMAIN_BOUNDARIES.md)
for details and [clean-instance setup](docs/CLEAN_START.md) for what can be
set up independently. Individual integrations and permissions require their
own configuration and validation.

## Install and use

Start with the [clean-instance guide and workflow status](docs/CLEAN_START.md).
It distinguishes available source features from optional setup and remaining
qualification. Factory development and routine use have stopped; its source
and [historical usage guide](docs/FACTORY_USAGE.md) remain for reference.

Python 3.12 or newer is required. Create a virtual environment, install the
project with the optional features you need, then run `sios --help`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/sios --help
```

Add optional features separately as described in the clean-instance guide.
Basic Memory 0.23.2 remains the default index extra. An explicitly selected
[Basic Memory 1.0 option](docs/BASIC_MEMORY_100.md) is available on macOS;
it requires a separate environment and index and is not a Windows indexer.
Windows satellite clients can connect to a separately configured SIOS server
as described below; their own compatibility must be verified independently.

Create a private configuration with `sios setup --help`. Point its data and
cache roots outside this repository. The server accepts private-network
connections only with TLS and scoped credentials. The local MCP adapter can
also run in a client environment with an authorized configuration. A remote
JSON RPC endpoint is not an HTTP MCP endpoint.

For Windows satellite use with Claude Code, Antigravity or Codex, see
[satellite MCP setup](docs/SATELLITE_CLIENTS.md).

Memory writes use stable request IDs and revision checks. Basic Memory indexes
only the configured Memory source; the Markdown note remains authoritative.
An index timeout does not undo a completed write. Re-read the note and its
receipt before retrying. Selective `memory_capture` accepts an already reviewed
claim; it does not extract a conversation or inspect prose for secrets.
Agent threads require an explicit server-side ACL and device scopes and are
disabled by default.

Brain proposals can be reviewed with a read-scoped client. An owner-operated
terminal command can review and apply a three-file bundle containing one
concept, its category index and the Brain log. It requires the separate
`brain:approve` scope, an interactive confirmation and unchanged base
revisions. Review material under `Brain/working/` and `Brain/inbox/` is
noncanonical. `catalog(store="brain-review")` lists bounded pending paths for
review; `read_revision` returns a bounded Mission Markdown hash and byte count
without returning the body.

For a local evidence-linked search screen, see [Evidence Search](docs/EVIDENCE_SEARCH.md).
For using Hindsight recall results with authorized SIOS originals, see
[Hindsight source verification](docs/HINDSIGHT_SOURCE_VERIFICATION.md).

See [domain boundaries](docs/DOMAIN_BOUNDARIES.md),
[conversation capture](docs/CONVERSATION_CAPTURE.md),
[agent threads](docs/AGENT_THREADS_L0.md), and
[deployment guidance](docs/FIRST_SLICE_DEPLOYMENT.md).

The optional [protected completion contract](docs/PROTECTED_COMPLETION.md)
describes explicit target allowlists, retained review and interactive owner
approval. It is disabled by default and is separate from ordinary Mission
writes and from qualification of any particular deployment.

[Offline QA report coverage](docs/QA_REPORT_COVERAGE.md) checks declared
requirements against fixed artifacts and supplied review evidence.
[Memory index diagnostics](docs/MEMORY_INDEX_DIAGNOSTICS.md) compare an
explicit local source and index without changing them. These utilities do
not authenticate a reviewer, repair an index or approve a release.

## Publishing boundary

Only source, generic documentation, synthetic tests and templates belong in
this repository. Runtime state, backups, logs, local MCP configuration,
developer guidance containing local paths, exports and evidence bundles stay
outside it. Run `python3 scripts/check_public_repo.py` and a secret scanner
against the full Git history before publishing. This check is a guardrail,
not a guarantee that arbitrary new prose is safe to disclose.
