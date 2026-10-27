# Evidence Search

Evidence Search is a read-only local browser interface for a configured SIOS
client. It searches Brain and Missions through the existing `search` operation
and combines the Memory index with direct Memory source search, then reads the original Markdown and
checks its SHA-256 revision before showing it. It does not generate answers,
modify a SIOS record, install a service, or send content to an AI provider.
Existing SIOS read operations may recover a previously journaled write.

## Install

Use Python 3.12 or newer. Install the package and its MCP client extra in an
isolated environment from the checkout containing this feature.

macOS:

```sh
python3.12 -m venv --copies .venv
.venv/bin/python -m pip install '.[agent]'
.venv/bin/sios-evidence-search --registered sios-l0
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install '.[agent]'
.\.venv\Scripts\sios-evidence-search.exe --registered sios-nucx
```

The named MCP client must already be registered in the user's Codex
`config.toml` and have SIOS read access. The program launches that existing
client with its registered command, arguments, working directory and
configured environment; it does not ask for or print credentials. The Python
environment hosting Evidence Search needs the MCP extra. For a different
existing SIOS client, use `--mcp-command` and repeat `--mcp-arg` for its
arguments, or use `--config` and `--ca-file` with the client's existing token
environment. Do not put credentials into command-line arguments.

Open the private session link printed in the terminal on the same computer.
The random fragment is a temporary access capability: do not share it. The
server listens only on `127.0.0.1`, does not log queries or source bodies,
and stops when the terminal process ends. It restricts browser-origin requests
and requires the session capability; this is not a boundary against a hostile
process running as the same operating-system user.

## Reading the results

Search is literal keyword matching. For a longer mixed-language question, a
small number of Latin project-name variants may also be searched and are shown
in the result status. It is not semantic search or an AI answer: use concise
keywords and inspect the full source before drawing conclusions.

The source path, source revision, declared type and status, and any recorded
verification or review dates come from the full document. Unknown fields stay
unknown. A matching revision proves only byte consistency with the read
response, not that the statement is true or current. A past review or stale
date is flagged using the local computer's date.

A Memory index may lag, so direct exact-text source search can find matching
records that have not yet been indexed. The index can also return matches that
the exact-text source search does not find. If either search is unavailable,
the screen reports the reduced coverage. The index may return only its first
matches; the screen reports that limitation even when source search succeeds.
Direct source search depends on the configured client and can fail when a source
is too large or cannot be read. A result limit, unavailable store, source-read error,
or unverified search revision is disclosed rather than treated as proof of
absence. Mission search includes Markdown histories up to 16 million bytes per
file and 100 million bytes per search. Exceeding either limit reports an
incomplete search. The ordinary full-read limit remains two million bytes. For a larger
Mission log, the viewer can reconstruct an existing versioned snapshot only
when its parts, combined content and current source revision all match. A
missing or stale snapshot is reported as an unavailable source. If full
Mission search fails, the tool may search a bounded selection
of registered Mission bodies instead; it labels that incomplete fallback and
does not claim to search every history or attachment. Search candidates are
limited to 5,000 per store and 30 are processed per page. Use **Load more
results** to continue. Each page has a ten-million-byte source content budget;
if the budget or time limit is reached, the screen reports it and offers a
continuation when progress is possible. If the candidate list changes between
pages, run the search again. Pages are not an atomic snapshot of the stores.

Search results from versioned log snapshot files show whether the index refers
to the current or an older log, or whether that comparison is unknown. Individual
parts are checked against the index. This does not verify the entire snapshot;
an integrity mismatch is shown if an individual part disagrees with its index.

The content budget is applied after a client response is received. A single
large MCP response can therefore temporarily use more memory. Search aims to
stop after 45 seconds and reports an incomplete result, but a direct-client
call already in progress can exceed that deadline. MCP cancellation does not
prove that the remote server has stopped processing the request.

Only explicit HTTPS and recognized Codex thread links found in the original
text are offered as links. Opening a link is a separate user action. Private
records, configuration, credentials, caches and test evidence remain outside
the public source checkout.
