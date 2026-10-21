# Brain, Memory and Missions in the isolated SIOS source

SIOS assigns curated understanding to Brain, historical evidence to
Memory/Basic Memory, and authoritative current work to Missions.

| Domain | Canonical SIOS location | Current source operation and authority | Index/migration treatment |
|---|---|---|---|
| Brain | `Brain/brain/`; proposals in `Brain/working/candidates/` | `brain_propose` needs `brain:propose`; canonical `brain_apply` needs separate `brain:approve` and exact reviewed revision (`service.py`, `brain.py`). The optional three-file bundle binds concept, category index and Brain log to one reviewed revision (`brain_bundle.py`). Ordinary MCP tools do not publish owner approval. | Not a Basic Memory write or index target. Review sources in `Brain/working/` and `Brain/inbox/` remain noncanonical. Later copy only approved canonical Brain content with provenance and approval state checked. |
| Memory | `Memory/notes/`, reports and journals | `memory_write` needs `memory:write`; create/replacement uses request ID and expected revision (`store.py`). | The 0.23.2 Basic Memory binding points to **only** `data_root/Memory` (`index_binding.py`). Markdown is source; the SQLite index is derived. Later migrate selected history, links, IDs and any DB-only records only after inventory and independent restore. |
| Missions | `Missions/<name>/mission.md` plus log | `mission_write`/`mission_log` use `missions:write`; completion has separate scope and verification (`service.py`, `store.py`). | Not indexed by the Basic Memory binding. Later preserve current status, revision, logs and references; do not synthesize mission state from Memory summaries. |
| Guidance/Policies | Approved guidance paths and registry | Authorized reads through bounded `read`; changing instructions requires its separate owner-governed workflow. | Not promoted from Memory search or thread messages. |

This is a **source design**, not proof that an instance has inherited knowledge
from another service. Synthetic tests cover specific paths only. Migration,
guidance revision and real client access require separate validation.
