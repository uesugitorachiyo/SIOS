# Windows satellite access from MCP applications

This guide describes the existing Windows stdio adapter for Claude Code,
Antigravity and Codex. A satellite application launches a local MCP process;
the process connects to the central SIOS application RPC over authenticated
HTTPS. Do not configure the HTTPS RPC URL as an HTTP MCP server.

The bounded `sios-memory` profile offers selected Memory operations and
restricted context reads. It is not a full administrative client, does not
automatically save conversations, and is not a separate server-side privilege
boundary. Use a dedicated per-device credential and grant only the scopes
needed on the server. Read-only clients cannot capture. The server determines
authorization, even if an MCP tool appears in discovery.

## Prepare a private installation

1. Obtain the endpoint, expected instance ID, and SHA-256 of the CA **file**
   from a trusted enrollment record. A changed address needs an HTTPS
   certificate valid for that address; updating an IP alone is insufficient.
   Never disable certificate or hostname verification.
2. Install `sios-core[agent]` in a versioned Windows virtual environment and
   verify that its physical `Scripts/sios-memory.exe` is the bounded profile.
   Use the Windows entrypoint generator when needed. Preserve an existing
   working runtime until the replacement has been checked.
3. Keep the client `config.json`, CA certificate and DPAPI-protected token in
   a private directory outside the checkout. The configuration's endpoint and
   instance must equal the independently verified values. Use the private
   enrollment procedure and `windows-memory-client.ps1 -Enroll`; a token is
   scoped to the Windows account that enrolled it. The child process receives
   the token in its environment; same-user isolation is not guaranteed.
4. Copy `templates/windows-memory-client.ps1` to the private setup directory.
   Supply `-SetupRoot`, `-SiosExe`, `-ExpectedInstanceId`,
   `-ExpectedEndpoint`, and `-ExpectedCaSha256` as separate arguments. The
   launcher validates the target and CA before decrypting the token or starting
   the child. Do not put credentials, CA files, real endpoints or local paths
   in the repository.

## Register the local process

For Claude Code or Antigravity, use the application's supported MCP settings
and adapt `templates/memory-mcp-config.example.json` privately. For Codex,
adapt `templates/codex-memory-mcp.example.toml` in the supported Codex MCP
configuration. These are examples, not installed registrations: confirm the
application version's actual configuration scope and preserve existing MCP
servers. Do not paste a token into application settings or enable blanket
approval of writes. Start a fresh session to load changed settings; avoid
interrupting an active task.

## Verify and operate

Use a dedicated test client and an authorized synthetic record. Confirm health,
tool discovery, an allowed search and full source read. If capture is in scope,
submit one selected fixture with a stable request ID, then search and read its
exact committed revision. Confirm both a denied scope and mismatched endpoint,
instance and CA are rejected. Search snippets and process health do not prove
capture, search visibility, or application-native use. Independently test each
application and device; a successful launcher test is not a Claude Code,
Antigravity or Codex session test.

An uncertain write must be reconciled against its original request and receipt
before any retry. Keep selected Hindsight synchronization separately controlled;
do not resend an unknown selected-sync request or infer delivery from an empty
search. The basic Memory profile does not automatically configure Hindsight.

## Compatibility gaps

The profile offers client-side `memory_capture_readiness` and
`read_successor_evidence`, but neither proves complete index coverage or
current state. The optional selected-sync facade below still requires an
independently provisioned and qualified forced-command server gateway. The
optional pinned Windows launcher below
is a source candidate, not a qualified replacement for an existing launcher.
Do not replace a working installation with
this profile until the required tool surface, scopes, startup behavior and
rollback have been checked in a new native application session. Publishing
these templates does not install or upgrade the satellite clients.

## Optional pinned Windows launcher candidate

`templates/windows-pinned-mcp.py` starts the full or bounded MCP module using a
separately provisioned runtime and a private profile. Run it with that runtime's
Python using `-I -S -B`, `--profile` and an independently recorded
`--profile-sha256`. Profile JSON has exactly these keys:

- `root`: absolute private root; all other paths below are slash-separated paths
  relative to that root: `runtime`, `config_file`, `ca_file`, `secret_file`,
  `package_root`, `manifest_file`.
- `runtime_sha256`, `ca_sha256`, `manifest_sha256`: lowercase SHA-256 hashes
  of the exact files. `manifest_file` is a JSON object mapping each relative
  `aios/...` package file, case-insensitively, to its lowercase SHA-256. The
  launcher rejects missing or extra package files, bytecode and reparse points.
- `config`: exactly `mode`, `instance_id`, `endpoint`, `token_env`; these must
  match the private configuration. Mode must be `client`, the endpoint HTTPS,
  and the token variable must match `SIOS_[A-Z0-9_]*TOKEN`.
- `module`: `aios.memory_mcp` for the bounded profile or `aios` for the full
  profile. The latter exposes a broader surface and still uses server scopes.
- `distributions`: a map of locally expected package versions, including
  `mcp` and `pydantic`, checked against runtime metadata before token decryption.

Keep the profile, DPAPI token, CA and manifest outside the repository. The
profile hash must come from an independent trusted deployment record; a hash
beside a mutable profile is not authentication. The launcher pins the Python
executable bytes and package tree, but version metadata is not a dependency
file hash or publisher signature. Its runtime, standard library, dependencies,
private directory, credential enrollment and same-user processes remain trusted.
Paths and hashes are checked before launch, not protected against subsequent
same-user replacement. Native application startup and rollback need separate
qualification before replacing a current installation.

## Optional selected-Hindsight client facade

`sios-selected-sync` is a Windows stdio MCP client for an existing, dedicated
forced-command SSH gateway. It requires `--ssh`, `--key`, `--known-hosts`,
`--host`, and `--hostkey-alias` arguments; paths must be absolute regular files
without symlink or reparse-point ancestors. `--port` defaults to 22. Keep all
actual endpoint, identity, key and host-key configuration outside the checkout.
It disables SSH config files, agent forwarding and connection forwarding, uses
only the named identity, verifies the host key, and sends one bounded request
on stdin. The server must force the intended command for that key.

The facade exposes inspection, explicitly reviewed submission and reconciliation
as separate tools. It reports `automatic_retry: false` and makes no automatic
retry after timeout, transport failure or unknown response. The client cannot
prevent a user or another client from issuing a second submission: the remote
gateway must authenticate and authorize the principal, independently review the
source, bind the exact revision and destination, preserve a durable attempt
journal, and reject a repeated or unresolved submission. A read-only inspection
does not demonstrate successful Hindsight delivery. The gateway's own
reconciliation may update its journal and must never resend an unknown request.

This repository does not ship the owner-specific gateway, bank mappings, legacy
exceptions, credentials or existing attempt journal. Do not point this facade
at an arbitrary SSH shell or treat it as a complete gateway installation. A
rebuild or journal replacement must preserve and independently reconcile the
old history before any selected write. Native checks with fictional data do not
qualify a real bank, server-side access policy or cross-writer fence.
