# Bounded satellite MCP profile

The `sios-memory` source candidate exposes exactly nine tools: health, memory_read,
memory_capture, memory_index_search, memory_index_status, memory_index_refresh,
read, read_revision and brain_review. The last three are read-only; they add
bounded Guidance, Brain and Mission context to the existing Memory operations.
It reuses the existing authenticated SIOS Client, strict selective-memory parser
and MCPServer runtime. It creates no credentials and installs no automatic capture.
The profile narrows the client tool surface; it does not narrow the underlying
credential's server privileges or isolate secrets from same-user programs.
Brain proposal/application, raw Memory write and Mission mutation/completion are not exposed.

The public server source now has a separate, uninstalled permission path for
selective capture and index verification: `read` + `memory:capture` +
`memory:index_refresh`. These scopes authorize only `memory_capture` and
`memory_index_refresh`; they do not authorize raw `memory_write` or `publish`.
Existing `memory:write` clients retain their current behavior. No device has
been enrolled with the new scopes, and the current NUCX launcher still uses its
existing broad credential. The gateway still advertises selective capture and index refresh;
tool discovery alone does not prove a call is permitted. Changing a device,
credential, client config or installed server requires separate owner approval.

## Packaging and deployment

The module uses the current SIOS client/config/parser contracts and the optional
MCP runtime (`pip install 'sios-core[agent]'`). The installed packaging entry point
is `sios-memory = "aios.memory_mcp:main"`. Use the physical Windows console
entrypoint generator against that distribution's interpreter; do not copy another
machine's executable. The template launcher retains DPAPI, CA byte-hash validation,
TLS verification and target checks. Configure its placeholders privately before
use. No live config, token, CA pin, machine path or instance identity is bundled.
Its explicit native Security and Utility module imports prevent inherited PS7
module paths from hiding WinPS ConvertTo-SecureString/Get-FileHash cmdlets.
Enrollment/revocation branches are source examples, never setup approval.
An older sideloaded Windows entrypoint may instead import a standalone
`sios_memory_mcp` module. Verify the actual installed entrypoint and module before
planning an upgrade; copying only `aios.memory_mcp` into that runtime does not
change what the existing executable imports. Keep the old runtime available for
rollback until a newly built, versioned candidate is independently qualified.

Merge only the new sios-memory entry into the app's supported config scope,
preserving other servers and normal per-call approvals. Back up settings first.
Do not restart an active app task. Verify discovery and read an existing approved
test note in a fresh isolated native client; standalone MCP checks alone do not
prove app-native operation. Do not add blanket tool approval rules.

## Operation contract

- Read: `memory_read(relative="Memory/notes/example.md")`. Include `Memory/`;
  use `relative`, not `path`. The search hit's `read_path` is the exact source.
- Shared read: `read(relative=...)` accepts Memory Markdown, exact registered
  Guidance, bounded canonical Brain Markdown, approved exact noncanonical Brain
  items, and Mission body/log/evidence Markdown. Morning Brief/Radar skills,
  unrelated Policies, Runs and non-Markdown evidence are excluded. Noncanonical
  Brain inbox/candidate items require an exact owner-named allowlist; its default
  is empty. The native entry point accepts repeatable
  `--owner-named-noncanonical EXACT_BRAIN_PATH` arguments for individually
  owner-named `Brain/inbox/**/*.md` or `Brain/working/candidates/**/*.md` paths.
  It rejects canonical paths, traversal, hidden paths and duplicates at startup.
  Add an item to a private launcher only through the separately approved config
  procedure; no item is enabled by this source change. Responses retain authority
  status, path and revision.
- Large Mission log: `read_revision(relative=...)` returns its SHA-256 and size
  without the body. Match that SHA to a versioned Mission evidence snapshot index
  before reading the Markdown parts; stop if the source revision changes.
- Existing Brain proposals: `brain_review(proposal_id=...)` reads the diff and
  stale-base status only. It cannot create, approve or promote a proposal.
- General search accepts ordinary text, including spaces and hyphens, subject
  to the existing backend query contract. Search snippets are only locators.
- Refresh: identifier is the note filename stem, no directory or `.md`;
  expected_revision is the committed SHA-256. Its query is a verification marker
  matching `[a-z0-9][a-z0-9_-]{3,99}`: 4-100 lowercase ASCII characters, with
  hyphens/underscores allowed and spaces disallowed. The marker must already
  occur in the note. These marker rules are not general-search restrictions.
- Never edit a committed note or fixed retry payload just to add a marker.
  If no suitable existing marker is available, report index verification pending
  and use authorized search/full-read evidence without claiming refresh success.
- Keep writes minimal, English, explicitly authorized and source-attributed.
  Search/read for duplicates first. Preserve IDs/payloads after uncertainty.
  Capture only selected reusable deltas; never ingest unrelated chats/transcripts.
- Retain the app/project rule that Morning Brief and afternoon Radar execution,
  scheduling and publication belong to Codex. This profile exposes no publisher;
  guidance is not a new server-side security role.

## Focused checks and rollback

The integrated-QA Mission currently permits source-only work and zero runtime
attempts. Its original exact AIOS-Central Python caller has MCP 1.30.0 and
cannot import this candidate's `mcp.server.mcpserver` dependency from MCP 2.3.0.
Do not substitute another interpreter under that existing allowance. Focused
gateway tests may run under the current SIOS `.venv` only after the owner
explicitly amends the same Mission for a bounded SIOS gateway test allowance;
that amendment must retain the original caller and all consumed historical
lineages. `tests/test_memory_profile.py` uses a
fake transport for tool/path/response boundaries. The separate
`tests/test_memory_gateway_oversized_log.py` uses an isolated temporary Store
and production dispatcher to check oversized-log revision and snapshot handling.
Neither test proves fresh native-client use or grants a deployment. Restore the
saved module/config if a deployment fails; do not undo data or remove other servers.
This public source integration does not deploy or configure any native client.
No push is authorized.
