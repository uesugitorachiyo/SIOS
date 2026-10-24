# Local Memory index freshness diagnostic

This optional read-only utility compares an explicitly supplied SIOS data
root's Markdown files with its bound Basic Memory SQLite index. It is not
an authenticated remote client, an index repair or a requirement for initial
setup. It contains no preset instance, path, credentials or note content.

```sh
python -m aios.memory_index_freshness /absolute/path/to/your/data
```

It expects the existing `.aios-basic-memory.json` binding and a sibling
`index/memory.db`, with one matching project and no active SQLite sidecars.
These are compatibility filenames, not a dependency on an old AIOS runtime.
It refuses other layouts or an index changing during comparison; do not
delete sidecars or modify a live database to force acceptance. A new empty
installation without an index does not need this diagnostic.

The output reports `fresh`, `lag` or `error`, counts, an index digest and
hashed identifiers for missing/changed/orphan paths. It does not output note
contents. Exit codes are 0 for fresh, 2 for lag and 1 for an error.
`authority_attested=false` always remains explicit: a consistent local index
does not establish remote identity or central data authority. Keep outputs
private, since project names and counts are still instance metadata.
