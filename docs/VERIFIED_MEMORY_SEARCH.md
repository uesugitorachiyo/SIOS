# Verified Memory search

The optional MCP client exposes `memory_search_verified(query, max_reads=6)`.
It searches the configured Memory index, reads selected full sources and
verifies their revisions before following references found in those sources.
Read attempts are bounded, including failures. The result reports incomplete
coverage and any unresolved reads; an index hit or a reference is not itself
proof that a claim is current or relevant.

The tool uses existing read permissions. Underlying reads and searches can
trigger the server's normal pending-operation recovery and index synchronization.
It does not repair missing search results or guarantee that every correction
or natural-language query will be found.

The MCP client also exposes three bounded evidence helpers:

- `memory_capture_body_readiness` checks that one chosen body marker returns
  the expected selective note and that the current source contains the marker
  at the supplied revision. The marker is sent to the search service; do not
  supply secrets or material excluded from search.
- `read_successor_evidence` follows explicit `supersedes` fields in a bounded
  set of search hits after verifying sources. It cannot establish that all
  successors were indexed or that a decision remains current.
- `memory_literal_verified` performs an explicit source scan and checks full
  source revisions and matches. The scan can lock the store and recover pending
  operations, and `max_reads` limits follow-up reads, not the scan itself. It
  refuses to start if the preceding health check reports pending operations;
  this check is not an atomic fence against new operations. Use it only for a
  distinctive literal when index results are insufficient and no operation has
  an unresolved outcome. Source text can be returned to the authorized caller.

These helpers use the existing read scope; they do not add an access grant.
Results are observations rather than guarantees of complete coverage, and
underlying reads or searches may synchronize or recover state.

In remote client mode, capture-readiness observations use a separate client
with a six-second transport timeout. If that client cannot be created or the
observation cannot finish, readiness remains unconfirmed; capture uses the
normal client and is not retried by the observation. The outer observation
also has a deadline and may retain its worker slot until an in-flight request
actually returns.

For `memory_index_refresh`, pass the note's canonical filename stem as
`identifier` (for example `example-note`), without `Memory/notes/` or `.md`.
The client rejects invalid stems before making a request and does not echo the
submitted identifier in the error. A timeout after a valid request remains an
unknown outcome and does not roll back a committed note.

A successful `memory_index_refresh` reports a bounded observation for one
marker and source revision. The result includes `atomic_snapshot: false`,
`index_coverage: unknown`, and its observation scope. Index rows or source
files can change between or immediately after the individual checks; a positive
result does not fence other writers or establish continuing search visibility.
Consumers using strict response schemas must allow these additional fields.
