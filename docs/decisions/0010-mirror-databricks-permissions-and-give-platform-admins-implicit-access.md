# Mirror Databricks permissions and give platform admins implicit access

**Status:** accepted
**Date:** 2026-10-30
**Deciders:** Liam Lee (design approved 2026-10-30)

## Context

Databricks decides who may reach the appmux slot (`CAN USE` on the App); appmux
must decide which children that person may open and manage. The user wants the
model of Databricks Apps — two levels, granted by email or by Databricks group
— and no groups table of appmux's own. With about 100 apps, a platform admin
who needs a grant on every app, or a hand-edited database, cannot support them.
Premise 1 forbids any permission path that needs a workspace admin. A platform
admin's OBO token can write platform data, so it must reach a child only by
choice (user decision, 2026-10-30, round 8).

## Decision

Two permission levels, `CAN_USE` and `CAN_MANAGE` (which includes `CAN_USE`),
granted to a `user` (email), a `group` (a Databricks group) or `everyone`
(`CAN_USE` only). A person's groups come from SCIM `GET /Me` called with their
own token and cached for 300 s (at most one hour stale after a failure, none
means no groups); only direct memberships count until spike 12 measures nesting.
If user authorization is off there is no token and only `user` and `everyone`
grants match. There is no deny entry and no inheritance between apps.

Effective permission: `CAN_MANAGE` for the app's owner (one undeletable app
admin), `CAN_MANAGE` for any platform admin (**implicit**, with no row in
`app_grants`), else the highest matching `app_grants` row, else none. Every
action goes through one table-driven, pure `authorize(actor, action, app)`;
default is deny and a failed group lookup fails closed. A platform admin is
anyone listed in the `platform_admins` setting (a person or a group); the first
comes from `APPMUX_BOOTSTRAP_ADMINS`, honoured only while the setting is unset,
and a change that would leave no `user` entry is refused.

Implicit access lets a platform admin open any app, read its release source in a
read-only viewer (each view writes `release.view_source`) and edit its
configuration. For an OBO-delegated app on which they hold no grant, the
launcher offers **Open without token** (default: the edge strips
`X-Forwarded-Access-Token`) and **Open as a user** (the token passes; audited as
`app.view_as_user`); the choice is kept in an edge cookie the edge removes
before forwarding ([design §9.1–9.2](../product/v1-design.md#91-the-model)).

## Alternatives considered

- **appmux's own `groups` and `group_members` tables** — rejected: they
  duplicate Databricks' directory and need a platform admin to maintain them.
- **No platform-admin access to apps they do not own** — rejected: supporting
  100 apps would need a grant per app or a hand-edited database.
- **A platform admin grants themselves `CAN_USE` to try an OBO app** — rejected
  by the user for the two view modes: implicit `CAN_MANAGE` makes the self-grant
  unnecessary.
- **More levels, deny entries, or inheritance** — rejected: Databricks Apps has
  two levels, and matching it keeps the model learnable.

## Consequences

**Better:**
- Grants use the people and groups Databricks already has; no workspace admin
  or SP grant is involved.
- A platform admin can support any app, and every write and every source view
  is in the audit log.

**Worse:**
- A group removal takes effect within the 300 s cache TTL (a grant removal
  within seconds).
- A platform admin can read any app's source and change any app's configuration,
  `source_path` and egress included; they are the trust root.
- In **Open as a user**, an OBO-delegated child receives the admin's token, which
  can write platform data — an explicit, audited choice on an app whose OBO a
  platform admin already approved.
- "Everyone" means everyone Databricks lets into the slot.

**Must now be true:**
- No code path decides access outside `authorize()`; `authz` does no I/O.
- A platform admin's implicit access is computed, never stored in `app_grants`.
- The edge removes `X-Forwarded-Access-Token` unless the app has
  `obo_delegated` and the visitor is its owner or named by a grant, or is a
  platform admin who chose **Open as a user**.
- `release.view_source` and `app.view_as_user` each write an `audit_log` row.

## Revisit if

Spike 12 shows `Me` includes nested groups (widen group matching); group
managers who are not platform admins become a need; or the 300 s TTL proves too
slow for revocation.

## Spike results

**Spike 12, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-12--nested-groups-in-the-scim-me-response-2026-10-30)).**
The first "Revisit if" condition fired, and the Decision's clause "only direct
memberships count until spike 12 measures nesting" is resolved: SCIM `Me`
(called with the visitor's own token) lists groups reached through nesting, typed
`indirect`, three levels above the user's own group on Free Edition. A grant
therefore matches **direct and indirect** memberships, by the group's display
name, and the edge ignores the type. The design, the brief, the data model and
FR-024 changed in the same change.

*Consequences (added).* The lookup lags a membership change. After an addition
the answers alternated between old and new for about 50 s; after a removal the
first answer showed it, then stale answers returned for about 75 s. A fetch made
inside that window can be cached for the whole 300 s, so the revocation bullet
under **Worse** reads: a group change takes effect within the TTL plus about
75 s. A missed addition denies (fail closed); a missed removal still allows
until the next fetch. One user, one addition and one removal were timed, on
Free Edition; the TTL "Revisit if" condition stays open.
