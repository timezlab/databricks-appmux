# Let a service principal hold an app grant

**Status:** proposed — built and covered by tests on 2026-11-03 (T188); a call by another App's
service principal through the Apps proxy with this grant has not been measured yet, and the owner
has not reviewed the choice. Extends [ADR 0010](0010-mirror-databricks-permissions-and-give-platform-admins-implicit-access.md),
whose text is not changed.
**Date:** 2026-11-03
**Deciders:** Liam Lee

## Context

When another Databricks App calls a child through appmux with its own service principal (SP),
the Apps proxy forwards the SP's **application id**, a UUID, in `X-Forwarded-Email`, and no
access token ([reference](../reference/mcp-and-a2a.md#who-the-target-sees), measured on Free
Edition on 2026-11-03). appmux then needs a grant that matches that value, or the call ends in
the same `404` as an unknown app. Until now the only grant that matched was `everyone`, because a
`user` grant takes a lower-case email and a UUID has no `@`. An `everyone` grant admits every
identity the proxy lets in, people included, so letting one agent call one tool opened the tool
to the whole workspace.

## Decision

- **A fourth grant type, `service_principal`.** Its `name` is the SP's application id in lower
  case (`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`). The request schema,
  the row model and a new check of `migrations/0003_service_principal_grants.sql` refuse any
  other form. It matches the forwarded id case-insensitively, as a user grant matches an email.
- **`CAN_USE` only.** The schema and `authorize` refuse `CAN_MANAGE` with a `400`, as for
  `everyone`, and `authorize` reads a stored `CAN_MANAGE` on a service principal as `CAN_USE`,
  because a holder of the shared-SP token can write `app_grants` (data model section 6). An SP
  that can manage an app could change its sharing and its releases with no person behind the
  change; nothing in v1 needs that.
- **No group lookup and no token.** The SP's request carries no visitor token, so the match uses
  the forwarded id alone and works with user authorization off. The SP is not a platform admin:
  `platform_admins` stays `user` and `group` only.
- **No collision with users.** An application id has no `@` and an email always has one, so the
  two namespaces cannot name the same visitor.
- **An existing registry gets 0003 by hand.** The loader runs `migrations/*.sql` only on an empty
  database and keeps no record of what it applied ([TD-008](../tech-debt.md)).

## Alternatives considered

- **Keep `everyone` as the only way in.** No schema change, but it admits every person and every
  SP the proxy lets in, so a tool meant for one agent is open to all. Rejected.
- **A Databricks group of service principals.** A `group` grant would cover several SPs with no
  new type, but the SP's request carries no token, so appmux cannot ask SCIM `Me` for the SP's
  groups; it would need a second lookup with the platform token. Not tried and not measured
  (the reference lists it as not measured); a later ADR can add it if a real need appears.
- **`CAN_MANAGE` for a service principal too.** Would let an automation manage an app, but every
  manage action is meant to have a person in the audit log, and a stored row is writable by the
  shared SP. Rejected for v1.

## Consequences

- A child can be opened to exactly one other App. The `mcp-and-a2a` reference keeps the
  `everyone` route for cases where any caller is fine.
- The audit principal form gains `service_principal:<application id>`.
- The admin UI shows the type in the sharing list and offers no level change for it; picking an
  SP from the workspace directory is part of `GET /principals` (T187).
- To verify on the dev App: apply 0003, grant a real SP, and call a child through the proxy with
  that SP's token (expect `200`), then with another SP (expect `404`).
