# Basic Memory 1.0 on macOS

Basic Memory 0.23.2 remains the default for existing installations. To use
1.0.0, create a separate Python environment with `sios-core[index100,agent]`
and select the source-protecting launcher explicitly:

```sh
sios --config /absolute/path/to/config.json index-setup \
  --basic-memory-version 1.0.0 \
  --executable /absolute/path/to/venv/bin/sios-basic-memory-100 \
  --index-root /absolute/path/to/new-index
```

Use a new index and retain the previous binding, data and index before a
migration. An existing different binding is rejected; this command does not
replace or migrate it. The 1.0 launcher currently supports macOS only. It
checks the Basic Memory version and indexer source hash, retains derived
permalinks in the index, denies the child process writes to the selected Memory
tree, and denies network access. Other processes retain their existing access.
It refuses an unrecognized upstream build rather than applying a patch blindly.

Natural-language queries attempt hybrid search. If that is unavailable, the
adapter attempts text search and reports `sios_search_mode: text_fallback` and
`sios_search_notice: hybrid_unavailable`. A text fallback is not equivalent to
multilingual semantic search. Results remain candidates requiring source
verification; index coverage and correction completeness remain unknown.

Install the 0.23.2 and 1.0.0 extras in separate environments. The latter has
not been qualified on Windows, Linux, or with every external OKF consumer.
