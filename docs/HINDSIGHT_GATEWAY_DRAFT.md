# Experimental Hindsight gateway and Windows relay

This source is an experimental candidate and has not been qualified as a
production replacement. It contains no existing installation, state or secrets.
It connects a local Hindsight service to an explicitly configured HTTPS gateway,
and offers a Windows stdio MCP relay. The service must be installed and operated
by its owner; nothing starts automatically. The gateway and its file-locking
journal require a POSIX host; the Windows relay is the supported satellite role.

The gateway is **one trust domain**: each device credential with `read` can read
all committed primary documents reachable by path in the bank. Project tags
filter recall; they are not access controls. Do not use this gateway for
mutually untrusted users or projects. Server scopes distinguish `read` and
`memory:write`, but the operator must independently restrict Hindsight, the
machine, network, private files and service credentials. Same-user processes,
other Hindsight clients and noncooperating writers are not fenced by the journal.
The gateway's connection and authentication deadlines bound individual idle
connections, not a sustained denial-of-service attack. Restrict network reach
and apply appropriate firewall and rate controls outside the gateway.

The Hindsight endpoint is restricted to loopback, with an explicit CA for
HTTPS. Configure the `sios_primary_chunks` retention strategy on an isolated
bank before trying a write. The gateway needs a private existing journal root
(mode 0700), a pre-created private target lock file (mode 0600), and a single
shared lock used by every cooperating writer. The target bank, journal and
lock are a single deployment unit: losing or replacing the journal makes
write identity and unknown outcomes impossible to reconcile safely. Back up and
independently test restore before production use. Never send a second write
when the result is uncertain; call `memory_store_reconcile` with the same
request ID. Missing target content does not prove a write failed.
Committed readback verifies exact source text and metadata only; it does not
verify stored tags, entities, extraction strategy or search visibility. This
candidate accepts a `supersedes` link only to a committed primary document in
the same project whose text and journal metadata still match. It does not
establish complete correction history.

A private 0600 gateway configuration supplies `host`, `port`, `cert_file`,
`key_file` and `devices`. The address must be a single private IPv4 or
loopback address; TLS 1.2 or later is required. Per-device entries contain
only a token SHA-256, scope list and server-chosen actor. Provision tokens
securely with `aios.hindsight_gateway.init_device` and transfer each token over
a trusted channel; never place it in Git, command-line arguments or chat.
The operator must obtain the endpoint and CA-file SHA-256 from an independent
trusted enrollment record, and verify that the certificate is valid for that
endpoint. Revocation requires removing the relevant device from configuration
and restarting the gateway; it does not delete copies already obtained.

For an isolated local fixture, the initial JSON has this shape; replace the
certificate paths with absolute paths in an owner-only directory, and select a
free port. Use a certificate whose subject alternative name matches the host.

```json
{"host":"127.0.0.1","port":19443,"cert_file":"/private/example/server.crt",
 "key_file":"/private/example/server.key","devices":{}}
```

Create the configuration and private directory with mode 0600 and 0700,
respectively; the key must also be 0600. From the verified package environment,
call `init_device(config_path, name, scopes, token_out)` with a new output path
in a 0700 directory. The function writes the generated credential to that file,
not the console. A failure after token creation can leave an orphan token;
reconcile the file and config under the operator's control before retrying.
Device updates through this API serialize on its config lock. Do not edit the
configuration concurrently through other programs. The gateway reads a
snapshot at startup: an enrollment or revocation becomes effective only after
an operator-controlled restart and verification.

The foreground entry point `python -m aios.hindsight_gateway_main --help`
shows the explicit required paths, bank and allowed project tags. Supply the
Hindsight token through `SIOS_HINDSIGHT_TOKEN` in a private process environment.
The service account and same-user processes can access that environment. The
operator is responsible for secure startup, log handling and host permissions.
Do not expose the backend to the public internet or bypass certificate checks.

On Windows, install the package in a verified Python environment, keep the CA
file and token enrollment outside the checkout in a private directory, then
adapt `templates/windows-hindsight-relay.ps1` with an independently verified
endpoint and CA-file hash. Enrollment uses a hidden prompt and Windows user
DPAPI. The relay sends the token to the configured TLS endpoint, rejects
redirects, and performs no automatic retry. Protect the setup directory and
runtime from modification; DPAPI does not protect against a compromised process
running as the same user. Start a new MCP application session after configuring
it and test with a synthetic record, a denied-scope credential, a bad CA, and
an uncertain write followed by reconciliation. Never test these conditions by
modifying an existing production journal or replaying an unknown request.

The current tests use fake data and a local generated certificate. They do not
qualify a real Hindsight installation, its retention strategy, independent
backup/restore, cross-writer fencing, Windows installation or fresh desktop
application session. An authenticated health response proves neither complete
search coverage nor backend readiness. Review and qualify each deployment.
The TLS integration tests require an `openssl` executable on the test PATH;
they are skipped when it is absent. Install the package in the chosen Python
environment before using the PowerShell launcher: its isolated `-I` mode does
not import code merely because a checkout is the current directory.
