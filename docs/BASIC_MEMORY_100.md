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

## Optional single-file repair

The server's retry worker can optionally repair a missing index entry for an
already committed note. This is disabled by default and is restricted to
macOS. For a trusted, dedicated 1.0 environment, set these environment
variables on the server process before starting it:

```sh
SIOS_REPAIR_100_ENABLED=1
SIOS_REPAIR_PYTHON=/absolute/path/to/venv/bin/python
SIOS_REPAIR_VENDOR=/absolute/path/to/trusted/import-directory
SIOS_REPAIR_CACHE=/absolute/path/to/writable/cache-directory
```

The Python environment must contain this SIOS package and the pinned Basic
Memory 1.0.0 build. The import directory must be a trusted existing directory;
it is added to the Python import path, so it must not be writable by untrusted
users. The worker checks the project binding, note revision and pinned upstream
indexer, and runs the repair in a macOS sandbox that denies networking, forks
and writes to the Memory source tree. A durable marker records uncertain
outcomes; an unconfirmed repair is not reported as successful. Do not change
these settings for an existing service without separately checking its runtime
and recovery procedures. This does not provide a backup or fence other processes
that already have access to the source tree.

`memory_capture_readiness` is a separate bounded observation of an already
committed selective note. It checks an exact-stem search result, index checksums
and a full source read using existing read access. It never repeats a capture;
it does not prove index completeness, natural-language recall or an atomic
snapshot. A timeout or conflicting evidence returns unconfirmed.

The macOS repair tests cover a live child retaining its lock after its
supervisor exits, a changed source or binding, a replaced lock, and an unknown
marker protocol. These isolated fictional tests exercise bounded failure
handling; they do not prove that unrelated or detached processes are fenced.
