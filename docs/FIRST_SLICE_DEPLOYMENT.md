# Isolated deployment guidance

Keep server configuration, data, cache, logs, certificate keys and credentials
outside the source checkout. Give each instance a distinct ID and storage
root. Use separate per-device tokens with only the scopes needed for that
client. Do not reuse another application's state or credentials.

A server listening beyond loopback requires an explicit private address and
TLS certificate. Clients must verify the certificate chain, server address
and expected instance ID. Do not bind a new listener to `0.0.0.0` or bypass
TLS validation. The HTTPS `/v1/rpc` endpoint is application RPC; MCP clients
use the local stdio adapter.

The generic Windows launcher in `templates/windows-satellite-mcp.ps1` expects
an independently verified instance ID, HTTPS endpoint and public CA hash. Its
enrollment stores the client token with current-user DPAPI and passes it to a
child process environment. Supply local values at launch; do not commit them.
If the installed Windows `sios.exe` points to a nonphysical interpreter path,
`templates/make-windows-physical-entrypoint.py` can create an entrypoint for
the actual virtual environment.

For an acceptance test, use fabricated notes. Create one with a fresh request
ID, wait for index catch-up, search, and read the full source. Update it using
its expected revision and a new request ID, then repeat search and read with
a unique marker. Check rejection of a bad token, wrong instance, stale
revision and reused request ID with changed content. A failed index refresh
does not imply the prior write failed; read the committed note before retrying.

Agent threads are disabled until the server is started with an owner-private
ACL and devices receive explicit thread scopes. Keep real discussions out of
synthetic acceptance fixtures. Direct access from another agent application
requires supported tools in that application; a server URL alone does not
establish integration.
