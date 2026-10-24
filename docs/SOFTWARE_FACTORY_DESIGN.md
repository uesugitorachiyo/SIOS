# Minimal coding handoff: proposed design

Status: design only, public portable summary of specification v0.17. No
Software Factory helper is implemented by this document. Existing OAuth
clients remain separately installed and authenticated by the user. A new
SIOS installation contains no account login, model conversation or run history.

## Scope and endpoint

The initial design has one Python SIOS stack and one fixed check profile.
Codex implements a task through its existing OAuth CLI. A helper collects
the actual changes, checks them, and starts a fresh Claude Code conversation
for independent read-only review through its existing OAuth CLI. The result
is a candidate plus evidence and a report for the operator.

```text
task + named committed base
  -> private helper Git records baseline B
  -> fresh extraction of B; fixed baseline checks
  -> Codex edits disposable work directory
  -> helper captures actual candidate C and complete diff
  -> fresh extraction of C; fixed candidate checks
  -> fresh read-only Claude Code review bound to C
  -> candidate + report + next human action
```

The source checkout is read-only to the helper. No automatic import, source
commit, push, PR, merge, deployment or live-runtime installation is included.
Only a named committed base is admitted; unrelated uncommitted work stays
untouched and excluded. A committed base that fails checks blocks the run.

This is trusted same-user local coding. Candidate checks execute source code
as the user. Separate default HOME/temp directories are not host isolation;
strict secret separation, hostile-code execution and hidden test oracles are
outside scope. OAuth credentials stay in vendor storage, never task packets
or reports. Model account limits and permitted usage must be defined before
live qualification; OAuth does not imply unlimited or free calls.

## Task, capture and profile

- The task fixes intent, acceptance, exact base, allowed paths and expected
  test edits. It cannot override check commands or environment.
- Use a helper-owned Git directory outside all producer write roots. Disable
  inherited Git environment/config, hooks, filters, external diff, textconv,
  encoding and line-ending rewrites. Parse path lists NUL-safely.
- Force-capture ignored and untracked source too. Reject symlinks, special
  files, nested Git repositories, submodules and unsupported attributes.
  Extraction must produce the exact selected tree B. After implementation,
  capture additions, deletions and mode changes as C and a complete binary
  diff; repository ignore rules must not hide changes.
- Recursively reject `__pycache__`, `.pyc` and `.pyo` in the selected base,
  producer directory and check trees. Disabling bytecode writes does not
  prevent reading existing bytecode; do not silently discard it.
- Initial tasks are limited to ordinary `src/aios/` and `docs/` changes,
  with test edits only when declared. Authentication, permissions, backup,
  server/MCP exposure, settings, storage integrity and release code are
  excluded. Protect tests, scripts, packaging, ignore/attribute rules and
  dependency definitions; undeclared protected changes block.

The fixed profile pins a user-selected Python 3.12+ interpreter and its
dependency environment outside producer writes. It has no build step and
performs no installations. Each check uses a fresh commit extraction,
allowlisted environment, candidate-first `PYTHONPATH`, disabled user site,
disabled bytecode writes and separate HOME/temp defaults. Do not inherit
instance configuration variables or reuse live settings. No absolute
developer paths or private runtime environments are distribution defaults.

One small helper-owned launcher outside producer writes verifies `aios` and
loaded `aios.*` module origins in the same process that discovers and runs
stdlib unittest tests. Verify origins after discovery and execution. Record
discovered/executed IDs, counts, skips with reasons, errors, expected failures
and unexpected successes. Require nonzero execution and valid structured
results bound to B/C and the profile. The passing baseline fixes required
test identities and reviewed skips. Missing tests, new unexplained skips or
expected-failure designations block, even with exit code zero. Intentional
coverage changes need a newly explicit check contract.

Checks must leave their extracted source trees unchanged. This profile has
nothing to regenerate. A future profile with generated artifacts must
regenerate them from C in a fresh tree with fixed trusted commands and
inspect those results; producer leftovers cannot qualify the candidate.

## Review and supervision

Start Claude Code in a new conversation with read-only tools, no shell or
write tools, and no undeclared hooks/MCP/customizations. Give it intent,
B/C identities, the complete bounded diff, protected-path flags and baseline/
candidate origin and test records with explicitly labeled output tails.
Do not supply the producer conversation or endorsement instructions.
Verify the reviewed source still equals C afterwards.

Require a structured result containing task ID, candidate commit,
`fulfillment` and `security` verdicts (`pass`, `block` or `unknown`), and
evidenced findings. Positive handoff requires both verdicts to pass on exact
C, successful nonempty in-scope implementation, passing checks with preserved
coverage, no blocking flags and known-ended processes. It is not release
approval and does not substitute for the pending complete QA workflow.

One supervisor covers Git, checks and both model clients. Acquire the lock
before preparation; use fixed argv, process groups and file logs. On timeout,
interrupt or overflow terminate the group and verify termination. Unknown
termination, malformed output, missing result, scope drift or quota/auth
failure stops forward work. No retries, fallback, automatic repair or resume.
Publish the final local report atomically; a missing final report means an
incomplete run. Preserve failed-run evidence privately for manual diagnosis.

Proposed limits: one foreground run, 75 wall minutes, 100 MiB source,
2 MiB each for diff and review packet, 20 MiB per process stream and two
model calls (implementation and review). Reject excess rather than omit
evidence. Private evidence permissions restrict other users, not same-user
processes. A killed client does not prove provider work or billing stopped.

## Construction and qualification plan

| Package | Minimum deliverable | Estimated human hours |
| --- | --- | ---: |
| P1 | Fixed task/profile, controlled Git capture and exact baseline | 2–2.5 |
| P2 | Two OAuth CLI adapters, lock and common supervisor | 2–3 |
| P3 | Fresh checks, same-process launcher, exact-candidate review and report | 1.5–2 |
| P4 | Qualification fixtures, bounded live cases and operator instructions | 1.25–1.75 |

Subtotal 6.75–9.25 hours; with 25% contingency, rounded up, about **9–12
hours**. This is unmeasured, assumes functioning clients/dependencies and a
passing committed base, and excludes baseline repair and source integration.
Reassess at P2 completion or five cumulative hours, whichever comes first.
Do not remove actual implementation dispatch to make the estimate fit.

Before use, local cases must cover silent omissions, planted bytecode,
stale installed imports, zero/missing/skipped tests, protected edits, wrong
review bindings, interruption, overflow and unknown termination. Qualify
Codex write containment with an inside-work success and harmless outside-work
denial under the actual policy/layout. Failure to establish it blocks use.

Live qualification comprises the actual implementation/check/review chain,
reviewer tool-boundary evidence (or a separate denied-write case), and a bad
candidate blocked by review: at most four model calls. Set an explicit account
allowance and incremental cash bound before these calls. Neither earlier
read-only CLI success nor private historical QA receipts qualifies this chain.

Other clients/stacks, automatic fixes, background execution, release/CI,
runtime installation and broader QA orchestration remain later work. A
public implementation must be independently qualified with synthetic inputs
and fresh user-owned settings; no private operational history is required.
