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
