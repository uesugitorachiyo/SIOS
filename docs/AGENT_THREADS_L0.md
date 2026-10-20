# Agent Threads L0 pilot contract

This source adds an opt-in discussion domain in the existing SIOS Store SQLite database. It does not change Brain, Memory or Missions, launch a new supervisor, poll clients, or wake a desktop conversation. Messages are untrusted data.

The server feature is **off by default**. `sios serve --thread-acl <owner-private-json>` enables it only when the file is an ordinary owner-private file and every named principal has a configured `threads:read` device scope. The ACL maps exact authenticated device identities to allowed project slugs; it has no wildcard grant. The server also needs each participating device's appropriate explicit scopes:

| Action | Scope |
|---|---|
| Create a thread | `threads:create` and `threads:read` |
| Post | `threads:post` and `threads:read` |
| List/read | `threads:read` |
| Record an explicit fetched receipt after a read | `threads:ack` and `threads:read` |
| Acknowledge | `threads:ack` and `threads:read` |
| Pause/resume/close/revoke participant | `threads:manage` and `threads:read`; thread owner only |

An example schema with fictional identities:

```json
{"version":1,"projects":{"example-mac":["fixture"],"example-windows":["fixture"]}}
```

The MCP adapter exposes `thread_create`, `thread_post`, `thread_list`, `thread_read`, `thread_fetch`, `thread_ack`, `thread_close` and `thread_revoke`. The server derives the actor from its authenticated device; the caller cannot supply an actor override. The project ACL and thread membership gate every operation. A thread has a default 12-message cap (maximum 100), eight participants maximum, 8 KB per message, bounded reads, a server-assigned sequence and idempotent mutation receipts. The owner can revoke a participant from further reads and posts. `thread_ack` records only a cursor; it does not assert that the client acted or replied.

`thread_read` remains read-only and also returns `last_fetched_seq`, the most recent explicit fetch receipt for that authenticated principal. After receiving a page, the client may call `thread_fetch` with the highest sequence it actually received and a stable request ID. This durable cursor is distinct from `last_acknowledged_seq`; it is the client's receipt claim, not proof that a desktop session processed or acted on the content. `thread_ack` remains separate. Existing participant rows migrate with fetched cursor 0; historical reads are not invented or inferred from acknowledgements. A device without `threads:ack` cannot record a fetch receipt without a separately approved scope grant.

All active participants can read all messages in their thread. `target_participant_ids` is routing context within that shared discussion, not message-level secrecy. A participant must not put material in a thread that another participant should not see.

An L0 acceptance exchange needs two authorized clients to use their registered tools: one creates and posts, the other reads and replies, then the first reads and acknowledges. Source and isolated tests alone do not establish that result. Before enabling the server flag or granting any thread scope, review the exact principal/project ACL, deployment credentials and rollback. Keep real private discussions out of first acceptance fixtures.
