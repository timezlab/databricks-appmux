# Gate credentials and platform fields behind a request catalogue

**Status:** accepted — renaming an app depended on spike 9 (cascade privileges), which passed on 2026-10-30.
**Date:** 2026-10-30
**Deciders:** Liam Lee (design approved 2026-10-30)

## Context

A child has no Databricks identity unless one is configured. Premise 1 prefers
OBO, then a service principal, and puts personal access tokens out of v1;
premise 5 says a risk that mechanism cannot remove is gated by a per-app
approval from a platform admin with a recorded reason, and gates stay few.
How far a credential reaches, and what code runs in the slot, is the platform's
judgment, not an app admin's. Asking a platform admin outside appmux leaves no
record and gives the reviewer no context. An earlier draft had eight request
kinds, one per field.

## Decision

Three credential modes: **OBO** (the visitor's token, stripped unless the app is
delegated), **shared SP** (a short-lived token of the appmux SP, only while the
master switch `shared_sp_delegation_allowed` is on) and **own SP** (an existing
service principal attached by the app admin as two `secret` resources, needing
no approval). Delegations are two flags on `apps` that only a platform admin
sets.

An app admin asks inside appmux through one table, `permission_requests`, with
three kinds defined in a code catalogue: `register` (a new app; any visitor may
ask, at most 3 pending per person), `delegation` (`obo`, `shared_sp` or both)
and `settings` (any of `slug`, `display_name`, `source_path`,
`memory_budget_mb`). Being a gate belongs to a **field**: the gates are
`register`, `source_path`, `obo` and `shared_sp`, and the gates stay three
(register an app, OBO, shared SP). Approval is one transaction under the
platform admin's own role, applies every field or none, and needs a reason when
it holds a gate field. There is one pending request per (app, kind). **A request
grants nothing:** authorization and the reconciler read only `apps`. A taken slug
at approval closes the request as `denied` automatically, recorded under the
admin who pressed Approve. Self-approval is allowed and recorded. The owner and
the egress rules are a platform admin's direct edit, not requests; deleting an
app is not a request ([design §8.1](../product/v1-design.md#81-the-three-modes-and-delegation-requests),
[§9.3–9.4](../product/v1-design.md#93-config-levels-and-review-gates)).

## Alternatives considered

- **The app admin turns OBO or the shared SP on for their own app** — rejected:
  reach is the platform's decision.
- **A request kind per field** — rejected: a form per field, and a new name with
  a new budget needed two requests.
- **A fixed slug for the life of an app** — rejected by the user: renaming is a
  real need.
- **Egress rules as a request, or edited by the app admin** — rejected by the
  user: where code may connect is a platform admin's judgment from the start.
- **A second-person approval rule** — rejected: it would lock out a team with one
  platform admin.
- **A pooled SP shared by apps of equal trust** — deferred: someone must create
  the SP and every app using it shares its reach.

## Consequences

**Better:**
- Every request has a reason, a reviewer and an audit row, and a forged one
  changes nothing because `apps` is the only authority.
- Adding a field or a kind is a catalogue entry and its tests, not a migration.

**Worse:**
- The gates cost review latency, rest on one person's judgment and can be
  rubber-stamped; shared SP carries the largest exposure (it reaches everything
  the appmux SP reaches, and can write app-level registry tables).
- Anyone who can reach appmux may ask for an app, so the inbox can carry asks a
  platform admin would never have made; a child holding the shared SP can fill it
  with invented names, past the per-person cap.
- Renaming moves every link to the app and restarts a running app through the
  normal swap; the old URL answers `404`.

**Must now be true:**
- `apps.obo_delegated` and `apps.shared_sp_delegated` are written only by a
  platform admin's role, through an approval or `delegation.grant`.
- `authorize()` and the `reconciler` never read `permission_requests`.
- `kind` has no SQL list; the catalogue in code owns kinds, fields and gates.
- `permission_requests` allows at most one `pending` row per (app, kind) (the
  `one_pending_request` index) and at most 3 pending `register` per person.
- A `shared_sp` request or grant is refused while
  `shared_sp_delegation_allowed` is off.

## Revisit if

Reviewers start rubber-stamping (add a second-person rule for the gates); a
user asks for access requests, deletion requests or time-limited approvals;
spike 9 shows renames cannot cascade; or a pooled SP becomes a need.

## Spike results

**Spike 9, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-9--cascade-privileges-on-lakebase-and-the-slirp4netns-licence-2026-10-30)).**
The rename in the Decision works under the platform admin's role: `UPDATE apps
SET slug` cascaded into the five child tables through the deferrable composite
keys of `app_config`, in one transaction, and a rename onto a taken slug failed
as a whole and changed nothing. The "Revisit if" condition that renames cannot
cascade did not fire.

**Spike 10, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-10--shared-sp-token-refresh-2026-10-30)).**
*Free Edition only.* A shared-SP token needs no helper in the child: an
unmodified Databricks SDK in a child that never held the client secret kept
working across the one-hour expiry, either through a local metadata endpoint
(`DATABRICKS_METADATA_SERVICE_URL`) or through a token command
(`DATABRICKS_AUTH_TYPE=databricks-cli` with `DATABRICKS_CLI_PATH` naming a stub
that prints a token file the supervisor rewrites); a static `DATABRICKS_TOKEN`
failed from the first call after expiry. The token request also accepts a scope
narrower than `all-apis` and enforces it. So the Decision's "short-lived token"
is deliverable to unmodified code and constitution I holds. Narrowing does not
stop a child with the `sql` scope from reading secrets through the SQL
`secret()` function, so it does not remove the shared SP's largest exposure.
The Decision and the "Revisit if" conditions are unchanged.
