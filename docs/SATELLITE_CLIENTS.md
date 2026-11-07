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

This source profile does not yet provide the separate client-side
`memory_capture_readiness` and `read_successor_evidence` helpers present in some
private satellite installations. Confirm an actual indexed hit and full source
read separately, and treat successor coverage as unknown. The source also does
not include a deployment-specific integrity-pinned direct launcher or the
private selected-Hindsight gateway. Do not replace a working installation with
this profile until the required tool surface, scopes, startup behavior and
rollback have been checked in a new native application session. Publishing
these templates does not install or upgrade the satellite clients.
