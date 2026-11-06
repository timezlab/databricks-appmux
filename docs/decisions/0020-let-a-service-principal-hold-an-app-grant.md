# Let a service principal hold an app grant

**Status:** accepted 2026-11-06 on the owner's word to follow the builder's recommendation (it was
proposed from 2026-11-03). It was built and covered by tests on 2026-11-03 (T188) and measured
through the real Apps proxy on 2026-11-04 on the Free Edition workspace (see
[Measured](#measured-on-2026-11-04)). The caveat of that measurement stands: taking back `CAN_USE`
on appmux left the principal able to reach appmux for about an hour, so an admin removes the
`service_principal` grant first. Extends [ADR 0010](0010-mirror-databricks-permissions-and-give-platform-admins-implicit-access.md),
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

## Measured on 2026-11-04

Free Edition workspace only. The caller was a throwaway service principal that is not an App (the
Apps proxy treats an App's service principal the same way), with a token of its own, calling the
`light_fastapi` fixture child through the dev App's address. The test is
`tests/integration/test_service_principal_grant.py`; the dev App's direct permission list was
saved first, changed only by adding `CAN_USE` for the principal, and put back, and a comparison
afterwards found it identical to the saved list.

| Step | Result |
|---|---|
| no `CAN_USE` on appmux | the **Apps proxy** refuses the call before appmux sees it |
| `CAN_USE` on appmux, no grant on the child | the proxy let the principal in 8 s after the permission was given; appmux answers `404`, the same as for an unknown app |
| a `service_principal` grant naming a *different* application id | still `404` |
| a grant naming this principal | `200`, in under a second (the test prints whole seconds: `0`); the child saw the application id in `X-Forwarded-Email` and no `X-Forwarded-Access-Token`, as the context above says |
| the grant removed | `404` again, in under a second |
| `CAN_USE` on appmux taken back | the proxy kept admitting the principal for **59 to 61 minutes** (every probe returned `200`, once `502` while the App restarted), then refused it; restarting the App did not shorten the wait |

What this settles and what it does not:

- The grant does what the Decision says: one SP in, every other identity out, and appmux's own
  grant takes effect within a second in both directions. This is the control an admin should reach for to stop an
  SP quickly.
- The Apps proxy's `CAN USE` on appmux took 8 s to take effect but is **not** quick to revoke on this platform. Taking it back
  leaves the SP able to reach appmux (and then any child that still has a grant for it, or an
  `everyone` grant) for about an hour. Remove the `service_principal` grant first, then the proxy
  permission; do not rely on the second to cut a caller off. For comparison, a secret-scope `READ`
  taken back flapped between allowed and refused for about 280 seconds
  ([spike log](../../specs/001-appmux-v1/spikes/README.md#service-principal-grant-through-the-apps-proxy-2026-11-04)).
- One workspace, one day, one principal. The revoke time was inferred from the order of the calls
  (the platform reports none), so read "59 to 61 minutes" as the width of the probe interval, not
  as a platform guarantee. A paid workspace may differ and was not used.
- The status was `proposed` because the measurement does not replace the owner's review; the owner
  accepted the ADR on 2026-11-06 (see Status).
