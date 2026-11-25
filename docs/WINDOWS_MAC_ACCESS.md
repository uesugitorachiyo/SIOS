# Windows access to a Mac-hosted SIOS Memory gateway

## Scope and existing implementation

Use the existing `src/aios/hindsight_relay.py` and
`templates/windows-hindsight-relay.ps1`. The relay uses Python's standard
library and exposes four Memory tools over local stdio. It forwards one
authenticated HTTPS request per tool call to an independently provisioned
Mac/POSIX gateway. Hindsight, the write journal and the single cooperating
writer stay on that host. Publishing this guide does not install or qualify a
gateway or change any running client.

The scoped SIOS context client and the Memory relay are distinct registrations:

| Role | Registration example | Verification |
| --- | --- | --- |
| Current guidance and permitted original-source reads | Existing `sios-nucx` or another enrolled scoped client | Its own health and allowed document read |
| Normal Hindsight Memory discovery, primary reads and authorized writes | `sios-memory` | Actual recall, one authorized store, full primary read and indexed retrieval |
| Retired candidate/bootstrap route, if present | Existing `sios-hindsight-candidate` | Keep disabled in that installation; it is not a fallback writer |

Do not infer that every deployment has these names, scopes or retirement state.
Confirm its owner's enrollment and backend selection. Retain retired settings,
backups and evidence privately; disabling a registration does not authorize
deleting records or stopping a service. A healthy context client does not prove
that the separate Memory gateway is reachable.

## Private setup and Codex registration

1. Obtain the HTTPS gateway origin, CA file, CA-file SHA-256 and scoped device
   credential through a trusted private enrollment channel. Check the server
   certificate's hostname/IP match. The gateway is not an HTTP MCP endpoint.
2. Prepare a private setup directory outside Git. Protect its ACL and runtime
   against other users. Put the verified CA at `ca.crt` and copy the public
   launcher there. Choose either the installed-package relay described in the
   [gateway guide](HINDSIGHT_GATEWAY_DRAFT.md) or a verified standalone copy of
   `src/aios/hindsight_relay.py`. For the standalone mode, record its exact file
   SHA-256 through the trusted release/enrollment channel. A hash beside a
   replaceable script does not authenticate that script.
3. Run the launcher with `-Enroll -SetupRoot <PRIVATE_SETUP_ROOT>` in a private
   interactive PowerShell window. Enter the credential only at its hidden
   prompt. Windows DPAPI binds enrollment to the current account/machine; it
   does not protect against a compromised process running as the same user.
   The credential is never a command-line argument or a TOML value.
4. Merge [the Codex relay example](../templates/codex-hindsight-relay.example.toml)
   into the application's supported private MCP configuration, substituting
   verified absolute paths and trust values. The standalone file mode is shown;
   omit both relay-file arguments when using a verified installed package.
   Keep each path as its own argument, including paths containing spaces or
   Unicode. The launcher uses isolated Python mode and validates the CA and
   optional script hash before decrypting the credential. Do not weaken TLS,
   hostname checks or script policy to make an untrusted installation run.
5. Preserve unrelated registrations, scoped tool allowlists and write approvals.
   If this installation has retired the candidate route, set only its existing
   `enabled` value to `false`; preserve its private definition. Back up the
   private configuration outside Git first. Do not copy the full configuration
   into this public repository.

The example gives the outer MCP call more time than the relay's 75-second HTTP
timeout. A timeout is still an uncertain write, not permission to retry it.
`memory_store_reconcile` never resends the original write. Endpoint, credential,
certificate, ACL and gateway enrollment remain deployment-specific controls.
Project tags filter searches; they are not per-project authorization boundaries.
See the [gateway's trust-domain limits](HINDSIGHT_GATEWAY_DRAFT.md).

## Verify the actual application and its saved state separately

A new `codex mcp list --json` process can check registration loading; inspect only
the needed enabled flags and tool names. Its raw output can contain private
paths, arguments or environment values, so do not publish it. This check does
not establish what an already-open chat loaded. Test that chat's registered
tools separately. Do not restart an active task solely to claim a fresh test.

For a user-authorized read/write qualification, use one clearly labelled,
non-sensitive synthetic fixture rather than a real conversation or project
decision:

1. Call the context client's health and read one allowed guidance document.
2. Recall under an allowed explicit project tag. A response, zero results and
   an exact matching hit are distinct outcomes; coverage can remain unknown.
3. Prepare one unique stable request ID and a tiny synthetic payload privately.
   Store it once through `memory_store` only with existing write scope/approval.
4. If committed, read the returned primary path with `memory_read_primary`.
   Compare the entire text and source revision with the intended content and
   receipt. A successful write receipt alone is insufficient.
5. Recall the unique fixture marker, locate the exact saved path/revision and
   read that returned source in full. A fuzzy search hit or excerpt alone does
   not prove visibility of the newly saved record.
6. Keep the request, receipt and actual measurements privately. A result may
   attest this bounded fixture/client only, not general migration completeness,
   exhaustive search coverage or another open chat's behavior. Remove a test
   record only through a separately authorized supported cleanup route.

For `basic_source_copy` hits, use the enrolled SIOS original-read route and
compare the returned revision. Use `memory_read_primary` for `hindsight_primary`
hits. A search excerpt is a pointer, not a verified source.

The ability to perform this explicit synthetic I/O test does not establish a
conversation record's provenance. Before saving a real session-derived claim,
verify its actual source using trusted startup/session evidence. An unresolved
session reference blocks that dependent capture; it does not prove the MCP
tool is missing or the storage connection failed. Never borrow another chat's
identifier or invent a source to bypass that check.

## Classify failures before acting

| Observed result | Meaning and bounded next step |
| --- | --- |
| Expected tool absent in this chat | Discovery/configuration state is unconfirmed; compare private registration and actual chat tool inventory |
| Context health succeeds, Memory call fails | Separate routes; inspect the failing Memory route without switching writers |
| `gateway_http_400` on a read/search | A request was rejected; investigate permitted arguments/project and gateway compatibility privately. It is not proof of missing records or a failed session source check |
| Authentication/scope refusal | Respect the denial; use the owner's enrollment process, not another writer |
| CA pin or certificate-name failure | Verify trusted enrollment values; do not disable validation |
| Store timeout, transport error or `unknown_no_retry` | Preserve the exact request and call `memory_store_reconcile`; never resend automatically |
| Committed primary read succeeds, exact recall hit absent | Storage/readback succeeded; search visibility remains unverified |
| Real-session source unverified | Do not capture that claim; report provenance separately from I/O availability |

This public change contains reusable setup and checks only. Real endpoints,
accounts, local paths, CA material, DPAPI files, device/instance identifiers,
session references, private test receipts and record bodies remain outside Git.
The ignore rules exclude DPAPI enrollments and personal `config.toml` backups.
The publication guard also rejects them if they were force-added to Git; run
`python scripts/check_public_repo.py` plus a full-history secret scan before
publishing. Ignore rules and scanners are safeguards, not a proof that arbitrary
new text is safe to disclose.
