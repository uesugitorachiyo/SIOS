# Protected completion source contract

Development-Mission completion is an optional validation feature, disabled by
default. Enabling validation alone grants no eligible target: the server must
also receive an explicit finite list through repeated `--completion-mission`
arguments. An empty list denies every target. This repository contains no
instance allowlist, identity configuration or live Mission records.

The source includes the existing guarded completion implementation:

- Retained control binds the exact Mission revision, proposed content,
  evidence producers and independent reviewer identities/sessions.
- Each retained operation and final consumer rechecks the target allowlist.
- QA and Security evidence is assessed with its declared producer/session;
  an eligible advisory result does not itself grant completion authority.
- The final file replacement repeats its guard after staging. Temporary-file
  placement stays on the same filesystem and outside the inventoried target
  directory, with directory synchronization after replacement.
- Malformed security timestamps and unavailable required date-time checking
  fail closed.

`completion_review` is a nonmutating preview restricted to the retained owner
approval principal and the `completion:approve` scope. It returns the exact
diff, current/bound revisions, eligibility, evidence digest and an expiring
review identity. It does not approve or complete a Mission.

`sios completion-approve` requires an authenticated owner client and interactive
terminal confirmation. The caller supplies the expected Mission, control digest
and revision. The command checks those pins, displays the authenticated preview,
then obtains and compares another preview before submitting approval. It rejects
changed, expired or incomplete review state. This terminal command is not
exposed as an agent MCP tool.

The module and CLI source were reconciled with an existing deployed package;
the repository also retains source-only functionality that was already ahead
of that package. This reconciliation is not a new deployment, does not enable
the feature, and does not establish qualification of the combined working tree.
No private operational records or instance credentials belong here.

`tests/test_owner_completion.py` carries portable pre-existing fake-transport
owner-command regression cases. They passed as part of the synthetic public
regression suite described in [validation scope](VALIDATION.md).
Full filesystem/evidence/final-consumer behavior needs separate targeted
qualification; private historical receipts are not portable public fixtures.
