# Attach resources with the attacher's own token and reference secrets

**Status:** accepted — spike 11 showed an App's service principal reads secret
values, and spike 16 found a check with an observed refusal for 8 of the 14
resource types; the attach check differs from the first wording (see Spike
results).
**Date:** 2026-10-30
**Deciders:** Liam Lee (design approved 2026-10-30)

## Context

In a standalone App, `valueFrom` names a resource the App declares, Databricks
fills in its value, and whoever adds it needs `CAN MANAGE` on both the resource
and the App. A child inside appmux is not an App: it has no service principal,
Databricks cannot inject a secret into it, and appmux never creates service
principals (a workspace or account admin action). The user wants each app to
declare its **own** resources, with a record of who attached each, and wants a
secret attached as a resource, not typed into the UI.

## Decision

Each app has its own `app_resources` rows: **key**, **type** (Databricks' list,
with `secret` as a reference), target, permission, **who attached it** and
`access_as` (`obo`, `own_sp` or `shared_sp`). appmux never grants access to a
resource. Instead the **attach check** runs in the attacher's request with the
attacher's own token: one read-only call per type proves they can reach the
target (and, where the response carries the caller's permission level, that the
level is the requested one or better); the result is `ok` or `unverified`, a
"no access" answer refuses the attach, and `shared_sp` accepts only `ok`.
Resources belong to the app, not a release, and take effect at the next start.

A `secret` resource is a **reference** (`scope/key`). The attach check is
`SELECT length(secret('<scope>', '<key>'))` on a SQL warehouse, run as the
attacher with the `sql` OAuth scope: it succeeds only if the attacher can read
the secret, and it returns the length, never the value. When a release starts,
the appmux SP reads the value and passes it through that child's environment
only. Plaintext exists only in appmux's memory and that environment and is never
logged or stored. The scope's manager grants the appmux SP `READ`, one scope per
app.

The encrypted typed-value store that would have replaced the references if an
App could not read a secret value is not planned (spike 11:
[design §8.3](../product/v1-design.md#83-secrets-where-the-value-comes-from)).

## Alternatives considered

- **A typed-value store as the only secret source** — rejected: appmux then
  holds a master key and every child's secrets, and needs a write-only screen
  and a rotation story. Kept only as the fallback.
- **Attach every child secret as a resource of the appmux App** — rejected:
  every new secret would edit and restart the appmux App, interrupting every
  child (constitution III), and an app admin cannot do it.
- **appmux grants the resource permission itself** — rejected: that is the
  workspace-admin path premise 1 forbids.
- **Resources as a subset of a platform list** — rejected: each app declares its
  own, as a standalone App does.

## Consequences

**Better:**
- The value stays in Databricks under its owner's access control and rotation;
  nothing secret enters the registry (references).
- The attach check mirrors the standalone rule, so no workspace admin or SP
  grant is involved.

**Worse:**
- A human grants the appmux SP `READ` per scope, and the appmux SP's reach grows
  with every scope: a shared-SP child could read them all if the API allows
  (design §8.4, O1).
- The check proves the attacher's rights at attach time only; it is not repeated.
  It proves access, and the level only for types whose response carries it (the
  serving endpoint); the permissions API needs `CAN_MANAGE` and an OAuth scope no
  App accepts.
- `unverified` is stored for types with no usable call or missing OAuth scope
  (spike 16: six of the fourteen), and such a resource cannot be used with
  `shared_sp`.
- The checks of `secret`, `uc_function` and `sql_warehouse` run a statement, so
  the attacher needs `CAN_USE` on a warehouse that is running or can start.
- Permission changes are eventually consistent: a revoked `READ` on a scope kept
  working for minutes (spike 11), so removing the appmux SP's grant does not stop
  a child from starting at once.

**Must now be true:**
- The `secrets` module returns plaintext only to the launcher's environment
  builder; a secret value never appears in release config, `audit_log`, logs or
  an API response.
- `resource.attach` always runs the attach check with the acting person's own
  token; `access_as = 'shared_sp'` is stored only with result `ok`.
- A release start fails with a message naming the scope when the appmux SP
  cannot read a referenced secret.
- The App declares the OAuth scopes the checks need (`sql`, and per type those in
  [design §13.1](../product/v1-design.md#131-what-the-operator-sets-up)); a scope
  name an App does not accept (`secrets`, `mlflow`, `jobs`) makes that type
  `unverified`.

## Revisit if

A later platform change stops an App's service principal reading secret values;
the apps API starts accepting `secrets`, `mlflow` or `jobs` as an OAuth scope
(the `unverified` types could then be checked); or Databricks lets an App receive
a child's secret as a resource without restarting the App.

## Spike results

**Spike 11, 2026-10-30 (pass, design differs; [log](../../specs/001-appmux-v1/spikes/README.md#spike-11--secrets-api-from-an-app-2026-10-30)).**
*Free Edition only.* An App's service principal, not a workspace admin, read
secret values with only `READ` on the scope, by REST (`secrets/get`) and by two
SDK calls (the documented "`DBUtils` only" did not hold); a scope it was never
granted answered HTTP 403, and it could create and delete a scope of its own.
The Decision's attach check ("lists the scope's keys with the attacher's token")
does not work: the attacher's OBO token gets HTTP 403 `required scopes: secrets`,
and `secrets` is not an accepted app scope. The working check is the SQL
`secret()` call with the `sql` scope (owner `SUCCEEDED`; the SP `SUCCEEDED` where
it has `READ` and `FAILED` where it has not; a missing key fails the same way).
A grant took effect on the first call; a revocation did not: one scope kept
answering for 163 s, another alternated between allowed and denied for about six
minutes. The Decision text above was corrected; the fallback store is dropped.

**Spike 16, 2026-10-30 (pass, design differs; [log](../../specs/001-appmux-v1/spikes/README.md#spike-16--per-type-attach-checks-2026-10-30)).**
*Free Edition only.* Eight of fourteen types have a read-only call that the
attacher passes and a principal with no grant fails: `secret`, `sql_warehouse`
(a use test), `serving_endpoint`, `uc_table`, `uc_volume`, `uc_function`,
`uc_connection`, `genie_space`. Six stay `unverified`: `mlflow_experiment` and
`job` (the scope they need is not accepted), `databricks_app` (its metadata is
open to any identity), `vector_search_index` and `gateway_model_service` (nothing
to test on Free Edition), `lakebase_database` (only an expired token was
refused). A read proves the attacher can reach the object, and the permission
level only where the response carries it, so "at the requested permission or
better" was narrowed in the Decision. The "most types stay `unverified`"
revisit condition is not met, and the Decision is otherwise unchanged.
