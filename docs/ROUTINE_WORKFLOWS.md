# Operator-defined routines

This document describes optional workflows for an operator's own SIOS instance.
It does not register an agent, schedule a run, create records, or grant access.
Establish the instance, authenticated principal, scopes, and the authority for
any Mission or report before use. Keep local focus settings and private data
outside the checkout. Configure the operator's timezone, active Missions, focus,
and source list privately; do not infer them from these examples.

## Shared boundaries

- Read current applicable guidance and full source records. A search hit or
  matching revision is not proof that a conclusion is current or complete.
- Use explicit occurrence and request identifiers for supported report writes.
  Preserve the original identifier and payload after an uncertain outcome;
  reconcile using the actual backend's supported operation before retrying.
- A routine may produce a draft when its report authority or write scope is
  unavailable. Mark missing source coverage and unverified results explicitly.
- Research is read-only unless the operator separately authorizes a later
  external action. Never collect private personal contact information by default.
- Do not automatically promote Brain knowledge, alter Missions or change a
  schedule as a side effect of a routine.

## Morning brief

Read the operator-selected authoritative active Missions and their next actions.
Offer up to three actionable suggestions, and mark the section incomplete if a
required Mission is unavailable. If public discovery is requested, verify each
candidate against its original source, distinguish publication, event and access
dates, and separate an author's claims from independently checked facts. Check
prior authorized reports for duplicates. An unavailable history means duplicate
coverage is unknown.

## Lead radar

Use a privately configured region, offer and source list. Screen dated public
signals, prioritizing primary sources. Compare the actual changed fact with prior
reports; an updated page alone does not establish a new opportunity. Record source
outcomes, dates, exclusions and failed checks. A public signal indicates possible
need, not a sale or willingness to pay. Report zero qualifying signals when
supported, or partial coverage when an important source is unavailable.

## Weekly review

Read the designated Mission inventory and selected full revisions, current goals,
and the prior week's authorized evidence. Separate observed progress, inference,
missing information and future actions. Check any routine history against the
operator's currently authorized schedule; an unavailable schedule or history
makes missed-run status unknown. Flag stale information without automatically
editing or promoting it. Present a small set of specific next actions.

## Report handling

The existing SIOS report publication endpoint and verification rules are
installation-specific. Use it only where the operator has designated that
instance as report authority and the authenticated client has the necessary
scope. Inspect the response, read the resulting report and run metadata through
that route, and check indexed visibility where supported. A receipt alone does
not establish search completeness. Retain the draft if publication or
verification fails. These descriptions are portable procedures, not proof of
an end-to-end automated implementation.
