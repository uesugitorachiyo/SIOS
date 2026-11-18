# macOS protected Hindsight recovery

This experimental, foreground-only administrator MCP exposes one operation,
`memory_store_recover(request_id)`. A separately provisioned macOS host must
supply a protected PostgreSQL instance, administrator-owned job definitions,
reviewed recovery data, a private journal and a recovery gate. Installation of
these dependencies is an operator task. An ordinary satellite device uses the
existing four-tool relay and has no administrator operation.

The recovery process requires real and effective UID 0. Install the Python
runtime, dependencies and this package in a root-owned location whose files,
parent directories and ACLs cannot be modified by the application account,
satellite user or other untrusted users. Use a dedicated local administrator
launch mechanism and verify the runtime and package before granting root
execution. Configure the three manually started system-domain launchd jobs for
PostgreSQL, the normal Hindsight writer and the recovery gate with three
separate, unprivileged OS identities. Each definition must be root-owned,
mode 0600, and have both `RunAtLoad` and `KeepAlive` set to false. Keep the
recovery endpoint and its credentials inaccessible to ordinary clients.

PostgreSQL must use only its protected Unix socket and the peer rules produced
by `aios.macos_hindsight.protected_postgres_access.production_access_files()`.
The controller closes both writer logins and terminates their connections before
restarting PostgreSQL and enabling exactly one writer. It rejects administrative
role membership, unknown logins, queued work and changed peer rules. The
operator must separately inventory all possible writers, queues, replicas and
privileged principals, and must fence every old route throughout recovery.
The configured route inventory is an operator assertion and cannot discover an
unlisted route.

## Protected deployment record

Place a root-owned 0400 or 0600 `deployment.json` in the protected
`administrator` directory. Supply actual values from the operator-reviewed
handoff and service installation. Use a different protected PostgreSQL port
from every old listener. The values below are placeholders, including all
hashes and inode pairs.

```json
{
  "legacy_uid": 501,
  "port": 15433,
  "source_id": "111",
  "target_id": "222",
  "receipt_sha256": "<64 lowercase hexadecimal characters>",
  "original_pgdata": "/absolute/old/database",
  "original_inode": [1, 2],
  "original_journals": "/absolute/old/journals",
  "original_journal_inode": [1, 3],
  "legacy_labels": ["example.old.database", "example.old.gateway"],
  "legacy_ports": [15432, 18080],
  "legacy_socket_port": 15432,
  "jobs": {
    "database": "<64 lowercase hexadecimal characters>",
    "application": "<64 lowercase hexadecimal characters>",
    "recovery": "<64 lowercase hexadecimal characters>"
  }
}
```

The witness validates the root-only retired vault, original directory identities,
old socket, listed launchd jobs and listeners, and the four archived artifacts
against the root-owned handoff receipt. It rechecks those conditions before and
during recovery. `legacy_labels` and `legacy_ports` must enumerate all retired
routes for the actual deployment. If a prior application can still connect or
an original data path can be restored by an untrusted process, the boundary is
not established.

The root-only `administrator/recovery-review.json` binds one request ID, target
cluster and exact payload. The recovery service receives the matching payload
and a separate private gate key. A historical unknown write can be reconciled
when the exact record already exists. A missing record can be submitted only
with a protected pre-dispatch receipt matching the request, payload, cluster and
unchanged database fingerprint. Without such evidence, recovery refuses to
resend. A durable exclusive reservation is created before any recovery send;
if its outcome is uncertain, reconcile rather than repeating the write.

Run `sios-hindsight-recovery-admin` as the authorized local administrator in a
fresh MCP session. Ordinary sessions must not register this command. The
operation stops the recovery writer and closes both writer roles on exit. An
explicit administrator-controlled `WriterSupervisor.start_application()`
transition is required to restart ordinary service after recovery. The local
administrator can run `sios-hindsight-resume-application` from the protected
runtime to perform that transition. Confirm the
writer boundary and use fresh Mac and Windows sessions to verify ordinary
access after the transition.

The current repository does not automate operating-system account creation,
protected runtime installation, service provisioning, data migration, receipt
creation or operator approval. Those steps require a separately reviewed host
integration. Run the isolated tests and qualify the full deployment before
using this interface with production data.
