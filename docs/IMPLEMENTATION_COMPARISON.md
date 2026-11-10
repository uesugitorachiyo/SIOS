# Bounded implementation comparison

This is a comparison of the public package against one Basic Memory 1.0
readiness and shutdown candidate snapshot. It is not an inventory of every
private deployment or a claim of complete feature parity. No private data,
configuration, credentials, or deployment paths are part of this document.

The comparison covered 32 Python modules in the candidate: 20 were
byte-identical to same-named public modules, and 12 differed. In particular,
`index_retry.py`, `index_repair_observe.py`, `index_repair_resume.py`,
`indexed_note.py`, and `graceful_drain.py` matched. Identical source does not
prove that the deployed configuration or runtime is the same.

| Area | Result for this comparison |
| --- | --- |
| Basic Memory adapter and binding | The public package retains the older adapter and has a separately selected 1.0 adapter and source-protecting launcher. The candidate stores its 1.0 implementation under the older module name. Do not overwrite the public adapter or binding with that file. |
| Repair child | The public repair child installs the pinned source-preserving indexer before invoking 1.0. The compared candidate uses a separately prepared runtime. Their startup code is intentionally different. |
| Capture readiness | The public server distinguishes index projection from verified search visibility; the compared candidate's server can report readiness based on the projection. Preserve the public distinction. |
| Thread and Memory scopes | The public package includes optional thread expiry and narrower capture and index-refresh scopes. The compared candidate lacks those capabilities; wholesale replacement would remove them. |
| Guidance updates | The candidate includes a deployment-specific guidance-write route. This comparison does not establish a need to add that write authority to the public package. |
| Client tools | The public MCP adapter includes body-marker, literal-source, and successor evidence helpers. It also has independent bounded readiness transport and validates refresh identifiers. Other client differences, including startup behavior, were not established as generally suitable for the public package. |

The public scope and thread tests pass with synthetic data on macOS. This is a
bounded review, not a full regression suite, cross-platform qualification, or
security audit. Continue reviewing other candidate snapshots when there is a
specific feature or evidence that they contain a needed change.
