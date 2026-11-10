# Verify Hindsight source pointers

## Optional direct recall

Install the optional `hindsight` extra in the same environment as the MCP
client. An operator may enable the separate `hindsight_recall_verified` tool
by setting `SIOS_HINDSIGHT_RECALL_ENABLED=1`, `SIOS_HINDSIGHT_URL`,
`SIOS_HINDSIGHT_BANK` and `SIOS_HINDSIGHT_TAG` on the client process before
starting it. The endpoint and bank must be independently trusted and authorized
for the queries sent there. Provision any `SIOS_HINDSIGHT_API_KEY` through a
private credential mechanism; never put it in the repository. HTTPS is
required except for loopback HTTP. The Hindsight SDK's normal certificate
validation applies; custom private-CA provisioning is not provided here.

Call `hindsight_recall_verified` with `{"query":"your question"}`. It sends
the query to that configured service and bank using `all_strict` for the
configured tag. It considers up to ten unique source pointers and reads their
SIOS originals using the existing client. It discards Hindsight snippets and
refuses hits missing the tag, source path or revision. Search and successor
coverage are unknown; no matching source does not establish absence. Hindsight
tags filter search, not access. The configured Hindsight credential's access
is a separate boundary from SIOS scopes. This is opt-in and does not perform
retention, synchronization or automatic capture. Do not send a query unless
the Hindsight service is authorized to receive it.

## Explicit source pointers

SIOS can also be used alongside an independently configured Hindsight instance.
The optional MCP tool `hindsight_sources_verify` checks explicit source
pointers from Hindsight against the current SIOS Memory originals. The tool
does not connect to Hindsight or send data to it. It performs no imports,
index writes, synchronization, retention or automatic conversation capture.

1. Use your independently configured Hindsight client to recall from an
   authorized bank. Apply the project filters appropriate to your deployment.
   Tags are search filters, not an access control mechanism.
2. Select no more than ten candidate metadata pairs. For each, supply its
   `source_path` as `path` and its complete `source_revision` SHA-256 as
   `revision`. Submit only the pointers, not result snippets or credentials:

   ```json
   {"candidates":[{"path":"Memory/notes/example.md","revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]}
   ```

3. The SIOS client reads each source using its existing permissions, verifies
   the returned path, source bytes and revision, and returns full source text
   only when it matches the advertised revision. A changed revision or failed
   read is reported separately. A matching hash verifies those bytes, not the
   claim, its relevance, current authority, or completeness of successors.

Only Markdown under `Memory/` is accepted. The tool validates the complete
batch before reading, caps it at ten pointers and bounds source size. It does
not infer a path from a document ID or search snippet. Sources without reliable
path and revision metadata cannot be verified by this tool. Search coverage,
successor coverage and current state remain unknown; follow declared correction
links through authorized source reads. Reads can trigger normal server recovery
or index synchronization. Access is governed by the existing SIOS client and
server scopes, not by Hindsight tags. Do not submit private data to an external
Hindsight service unless its destination and retention are authorized.

Run `python -B -m unittest tests.test_hindsight_evidence tests.test_mcp_adapter`
from a checkout with the `agent` extra installed. These fictional tests do not
qualify an actual Hindsight server or demonstrate synchronization.

On 2026-11-09 the source-verification, MCP and correction tests passed on
macOS (28 tests) and the corresponding verification, MCP and successor tests
passed on a Windows host (23 tests). The tests used fictional data and mocked
remote reads. They did not run Hindsight or a Windows-to-Mac live connection.

The direct-recall addition was tested on macOS and Windows with a fictional
HTTP server and the actual pinned Python SDK. On macOS, a separate isolated
Hindsight instance also returned a fictional record through the SDK and the
source was verified against a fictional read callback. This is not an
end-to-end test against an operational SIOS server, Windows-to-Mac connection,
or evidence of search completeness.
