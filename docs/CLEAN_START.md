# Start with an empty SIOS instance

SIOS is distributed as source, generic documentation, templates and synthetic
tests. A new installation must start with its own configuration and empty
storage, without the developer's notes, Missions, usage history, credentials,
client identities or operational receipts. Sample fixtures are test inputs,
not initial user records. No existing AIOS installation is needed for this
empty-instance path.

## Local first start

These commands describe the current source interface. A fresh installation
and empty-instance smoke check passed on Python 3.12/macOS; this is not full
platform or production qualification. See [validation scope](VALIDATION.md).
Python 3.12 or newer is required. From the downloaded source directory, on
macOS or Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/sios --help
.venv/bin/sios --config "$HOME/.config/sios/instance.json" setup --mode standalone
.venv/bin/sios --config "$HOME/.config/sios/instance.json" health
```

Choose an unused configuration path outside every source checkout. Setup
creates a new instance UUID, empty Brain/Memory/Missions directories and
support directories. It does not seed personal content or import history.
Configuration creation refuses to overwrite an existing file. Default data
and cache roots are separate per-instance directories in the operating
system's user-data locations; explicit `--data-root` and `--cache-root` must
also be outside source checkouts. Retain the setup output privately so you
can locate your own data. Local health checks do not prove remote identity.

On Windows, use `.venv\Scripts\python.exe` and `.venv\Scripts\sios.exe`,
and choose a private configuration path outside the checkout. The POSIX
`$HOME` command example above is not a PowerShell command.

Standalone mode requires no server token or model-provider API key. It is a
local administration mode, not a sandbox for untrusted agents. Add your own
content deliberately; `sios --config <your-config> memory-write --help` and
`mission-write --help` describe explicit creation/update inputs. Keep stable
request IDs and read returned revisions before an update or uncertain retry.

Optional features have separate dependencies: `.[agent]` for stdio MCP,
`.[index]` for Memory indexing, `.[backup]` for encrypted backups and
`.[verification]` for completion evidence validation. Install only what you
intend to configure. An index is derived from your own Memory source; an
index failure does not undo a committed note.

The pinned Basic Memory 0.23.2 index extra currently depends on a prerelease
of FastMCP. With uv, resolution requires explicitly allowing prereleases
(`uv pip install --prerelease=allow -e '.[index]'`). The base installation
does not require this dependency or any index service. Review this optional
dependency chain separately before operational use; it is not needed just
to create an empty SIOS instance.

For remote clients, configure your own server instance, TLS trust, scoped
credentials and client identity using [deployment guidance](FIRST_SLICE_DEPLOYMENT.md).
Generic templates require your values; they do not enroll a client or contain
usable credentials. A remote service URL is not an HTTP MCP endpoint.

## Workflow availability

"Source available" below describes code in this repository. It does not
attest that this combined revision has passed installation, integration or
release qualification.

| Workflow | Current status | Required setup or boundary |
| --- | --- | --- |
| Empty instance, health, explicit reads and Memory/Mission writes | Source available | Private local setup; your own records and revisions |
| Brain proposal, review and owner application | Source available | [Domain boundaries](DOMAIN_BOUNDARIES.md); separate owner approval |
| Selective conversation Memory capture | Source available | [Reviewed selected claim](CONVERSATION_CAPTURE.md); no automatic transcript import |
| Memory index and restricted Memory MCP | Optional source available | Index installation and client scopes; [client profile](memory-client-profile.md) |
| Bounded agent discussions | Optional, disabled by default | [Explicit project ACL and principal scopes](AGENT_THREADS_L0.md); no vendor-session control |
| Protected Mission completion | Optional, disabled by default | [Target allowlist, retained evidence and owner approval](PROTECTED_COMPLETION.md) |
| Offline Mission evidence check | Limited source available | [Consistency of a supplied export](SIOS_QA_EVIDENCE.md); no current operational attestation |
| QA report requirement coverage | Offline source available | [Fixed-artifact report check](QA_REPORT_COVERAGE.md); no test execution or reviewer authentication |
| Local Memory index freshness | Read-only diagnostic available | [Explicit user-owned source/index](MEMORY_INDEX_DIAGNOSTICS.md); no repair |
| End-to-end QA orchestration | Not included | Requires its own portable workflow and qualification |
| Codex coding handoff, helper-owned collection and independent Claude Code review | Design only | [Minimal design](SOFTWARE_FACTORY_DESIGN.md); no helper implementation yet |

Protected completion and the offline checker do not constitute the complete
QA workflow. An empty SIOS installation is independent of the retired AIOS
runtime and does not import its history. Completion of an operator's private
migration does not qualify a public installation or supply QA orchestration.
Release readiness must be assessed separately. Missing QA evidence must not
be treated as a pass or bypassed by enabling completion.

## Distribution boundary

Publish source and generic instructions only. Keep configuration, identity
maps, tokens, private keys, data directories, logs, model conversations,
backups and execution/review evidence outside the repository and packages.
Use synthetic fixtures when adding workflow tests. Optional migration must
be an explicit user operation; a fresh install must not restore developer
state or discover another application's data automatically.

Before publication, check the proposed files with
`python3 scripts/check_public_repo.py`, scan the complete Git history and
the proposed working files for credentials, and inspect the package contents
and clean-start behavior. Automated scans do not establish absence of all
personal information or security defects. Publication and clean-install
qualification are separate from source reconciliation.
