# Keep decisions in a nine-table Lakebase registry with split writers

**Status:** accepted (2026-10-30) — spike 9 (cascade privileges) passed and
spike 1 (OBO login to Lakebase) found a residual the owner accepted on
2026-10-30 (see Spike results). The fallback in the Decision was not taken.
**Date:** 2026-10-30
**Deciders:** Liam Lee (design approved 2026-10-30)

## Context

appmux needs durable state for apps, grants, releases, requests, settings and
an audit trail. Lakebase (Autoscaling) is the database a Databricks App can
reach: a service principal connects through an OAuth Postgres role, and the
token is checked only at login. A child that holds the shared-SP identity must
not be able to overwrite platform data, and no permission path may need a
Databricks workspace admin. An earlier draft used 20 tables, some of them
holding only runtime state.

## Decision

Lakebase is the registry. It holds **decisions and history, not runtime
state**, in nine tables, each with one writer class: `platform_settings`,
`apps`, `permission_requests`, `app_config`, `app_grants`, `releases`,
`app_resources`, `metrics`, `audit_log`. Run state (app states, route table,
counters, log tails) lives in memory and the reconciler rebuilds it at start;
snapshots and wheels are files in the content store.

Platform data (`platform_settings`, `apps`, the answers in `permission_requests`
and the first `app_config` row at registration) is written with the **platform
admin's own OBO token and Postgres role**, a member of
`appmux_platform_writers`. Everything else is written with the appmux service
principal. The rule is plain table-level `GRANT`s, which is why `apps`
(platform-written) and `app_config` (app-admin-written) are two tables.
`app_config.target_release_no` is the desired release and the `active` release
is the observed one. Writes use optimistic concurrency through `version`; the
reconciler re-reads the small tables every few seconds. If Lakebase is down
while running, appmux serves from memory; down at startup, it fails closed
([design §4](../product/v1-design.md#4-registry), [data model](../product/v1-data-model.md)).

If logging in to Lakebase with an OBO token cannot be made to work (spike 1),
shared-SP delegation is dropped and the SP writes everything.

## Alternatives considered

- **SP-only writes** — rejected: a child holding the shared-SP token could
  overwrite `apps` (delegation flags, egress rules, source folder) and settings.
- **OBO-only writes** — rejected: every app admin would need a Postgres role,
  and the OBO login is unproven.
- **Row-level security keyed on a session variable** — rejected: a holder of
  the SP token can set the variable.
- **One `apps` table with column-level `GRANT`** — rejected: column grants are
  unverified on Lakebase and harder to review than two tables with one writer
  class each.
- **Twenty tables, or persisting run state and events** — rejected: more
  writers, joins and migrations, and rows with no decision to audit.

## Consequences

**Better:**
- A compromised child with the shared SP can write app-level tables but cannot
  write an `apps` row or a setting, so it cannot grant itself a delegation.
- The database stays small enough to reason about: nine tables, each with a
  documented writer, columns, sample rows and DDL.

**Worse:**
- Each platform admin needs a Postgres role in `appmux_platform_writers`, which
  the Lakebase project owner grants by hand; without it the UI works but writes
  fail with a message naming that step.
- Lakebase is a hard dependency at startup; a run without it serves only what
  is in memory.
- `permission_requests` is written by both classes (it grants nothing, so the
  exposure is spam and forged requests only).
- Renaming an app relies on `ON UPDATE CASCADE` and deferrable composite keys
  under the platform admin's role, which no one has run (spike 9).

**Must now be true:**
- Only the `registry` module talks to Lakebase, and it picks the identity per
  write from the table (`apps`, `platform_settings` → platform admin's role).
- `INSERT`, `UPDATE` and `DELETE` on `apps` and `platform_settings` are granted
  to `appmux_platform_writers` and to no SP role.
- `audit_log` is insert-only.
- No table stores runtime state; no authorization or reconciler code reads
  `permission_requests` to decide anything.

## Revisit if

Spike 1 shows an OBO token cannot log in to Lakebase (switch to the fallback
above and write a superseding ADR); spike 9 shows cascades fail under the
platform admin's role; or Lakebase offers row- or column-level grants that make
one table per writer class unnecessary.

## Spike results

**Spike 9, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-9--cascade-privileges-on-lakebase-and-the-slirp4netns-licence-2026-10-30)).**
The cascade risk is closed. On PostgreSQL 17.11 (Lakebase, Free Edition only), a
member of `appmux_platform_writers` deleted an app and renamed another in one
transaction, and the cascades reached `app_config`, `app_grants`, `releases`,
`app_resources` and `permission_requests` although that role holds no write
right on them: PostgreSQL runs a cascaded write with the table owner's rights.
The deferred composite keys of `app_config` resolved at commit. A direct `UPDATE`
or `DELETE` on `app_config` by the group role was refused (42501), as was every
other write §6 of the data model withholds. `SET LOCAL ROLE` works to a role the
login holds the `SET` option on and is refused otherwise. The grants of the data
model give exactly its writer table.

*Consequences (added).* The data model's DDL did not load as written (the three
composite keys of `app_config` named tables created later) and was corrected.
The **Worse** item on renames is answered by the run. Lakebase adds its own
grants to every new table: `databricks_superuser` holds every privilege on it, so
the split of writers binds the two appmux roles and not a member of that role,
which includes the project owner's role by default. Operators are told so.

**Spike 1, 2026-10-30 (fail as written; [log](../../specs/001-appmux-v1/spikes/README.md#spike-1--obo-login-to-lakebase-2026-10-30)).**
Free Edition only. A user's OBO token as the Postgres password logs in as that
user's own role (not a superuser), and a write succeeds only through
membership of `appmux_platform_writers`: before the grant all four statements
were refused, after it only the granted `INSERT` worked, and after the revoke all
four were refused again. The fallback therefore does not fire. Two things
differ from the spike's pass condition. (1) The login **does not need** the
`postgres` scope: a token with only the identity scopes logged in and wrote, while
random and altered tokens were refused, so Lakebase checks validity and not
scope. (2) The token is checked **only at login**: a connection opened with it
answered 17 queries after the token expired, the last 1014 s later, and a
fresh login with the expired token was refused. The decision (split writers, the
platform admin's own identity writes platform data) is unchanged and works. What
is new is a residual: because scopes do not limit Lakebase, any OBO-delegated
child receives a token that can log in to the registry as the visitor, and for a
platform admin that includes the platform-data writes. The owner
accepted the residual on 2026-10-30 (spec question 5, option a) instead of the
fallback, which would give up the separation between a person and the SP. The
residual is stated, not removed: a platform admin's default view mode stays
*without token* (assumption A1), and the gap is logged as TD-002 in
[tech-debt](../tech-debt.md).

