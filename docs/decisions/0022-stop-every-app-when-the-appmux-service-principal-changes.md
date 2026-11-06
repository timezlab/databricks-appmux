# Stop every app when the appmux service principal changes

**Status:** accepted 2026-11-06 on the owner's word (they asked for children to be stopped after
the App is created again, and confirmed the plan below). Extends
[ADR 0009](0009-attach-resources-with-the-attachers-own-token-and-reference-secrets.md), whose
rule that appmux never grants access to a resource stands, and changes the start-up of
[design 5.6](../product/v1-design.md#56-crash-restart-and-shutdown) in one case only.
**Date:** 2026-11-06
**Deciders:** Liam Lee

## Context

A Databricks App has one service principal (SP), made with the App. Delete the App and create it
again and the new App has a **new** SP. Everything that was granted to the old SP stays behind: the
`READ` on a secret scope, `CAN_USE` on a warehouse, a Unity Catalog privilege, a folder.

The registry (Lakebase) survives if its project is kept, so the new appmux reads every app as
`running` and starts them all. What happens next depends on what the app uses:

- An app that names a `secret` resource fails to start, with the message that names the scope
  (FR-038). That check exists.
- An app that holds the shared-SP token and reads a table, a volume or an endpoint at run time
  **passes its health check and runs**. Its calls then answer 403, and nothing in appmux can see
  it: appmux does not know what a child does with the token.

appmux cannot list what a child needs from the SP, and it may not hold the right to grant it. Only
the app's admin, with their own rights, can give the new SP what the app needs. That is the rule
already in [design 8.2](../product/v1-design.md#82-resources-and-env-entries): **appmux never grants
access to a resource.**

## Decision

- **appmux remembers the SP it ran as.** At every start, before any app is started, it reads the
  newest `audit_log` row with the actor `appmux` and the action `platform.service_principal`. Its
  `detail.client_id` is the application id appmux ran as last time.
- **A different id stops every running app.** In one registry transaction appmux sets
  `desired_state = stopped` (with `updated_by = appmux`) on each app that was `running`, writes one
  `app.stop` audit row per app (actor `appmux`, automatic, reason `the appmux service principal
  changed`) and a new `platform.service_principal` row with `client_id` and `previous_client_id`.
  Nothing starts. The apps' admins give the new SP what their app needs and press Start.
- **No remembered id stops nothing.** A registry with no such row (a first install, or the first
  start after upgrading to this release) only gets the row written. Without it, a registry that was
  never marked would stop every app on its first start.
- **The same id changes nothing and writes nothing.** A restart, a redeploy and an upgrade are not
  a new SP; stopping the apps on each of them would break constitution III.
- **The step fails closed.** If the read or the write fails, the start-up stops and the gate stays
  shut (`STARTUP_FAILED`). Starting the apps without having decided would run them under an SP
  that has none of their access. The step runs first, before the content-store folder, so the
  stop is stored even when a later step (the new SP cannot reach the old folder yet) stops the
  start.
- **The start error names the SP.** When a secret cannot be read, the message says which service
  principal needs `READ`, by its application id, so the person does not have to find it.
- **The app page says why it is stopped.** An app that is `stopped` with `app_config.updated_by =
  appmux` shows a notice on its Overview: appmux runs as another service principal, so give the
  new one what the app needs, then press Start. The app detail carries the application id
  (`service_principal`). Pressing Start sets `updated_by` to the person, which clears the notice.

appmux still grants nothing. The deploy notebook's `SP_GRANTS` stays for what appmux itself needs.

## Alternatives considered

- **A column on `app_config`** (the SP an app last started under). It needs a migration applied by
  hand to every existing registry ([TD-008](../tech-debt.md)), and an app registered after the last
  start has no value, so a recreation straight after would not stop it. The audit row is one value
  for the whole platform and needs neither.
- **A row in `platform_settings`.** Only a platform admin's own role writes it (FR-047) and nobody
  is signed in when appmux starts.
- **Stop every app on every start.** Would end each upgrade and redeploy in an outage of every
  app; constitution III forbids interrupting running apps.
- **Grant on Start, with the admin's token.** Needs a scope the Apps platform does not accept for
  secrets (`secrets`, [design 8.3](../product/v1-design.md#83-secrets-where-the-value-comes-from)),
  reverses the rule above, and still cannot cover what appmux cannot predict. Rejected.
- **Do nothing; document it.** Leaves the quiet 403s described above. Rejected.

## Consequences

- After the App is deleted and created again, **no app runs until its admin presses Start**. That is
  the cost of the safe default, and the point of it.
- The record is a log row, not a table. No role can delete an audit row, so it stays. A holder of
  the shared-SP token can already insert audit rows and set `desired_state` on any app (design
  8.4, residual), so a forged row can do no more than stop apps at the next start, which that
  holder could do directly.
- The first start under a **new SP and a new release at once** (recreate the App and upgrade in
  one step) finds no row and is not protected. The next recreation is.
- It works only while the registry survives. If the Lakebase project is deleted too, there are no
  apps to stop and the operator registers them again.
- A new audit action, `platform.service_principal`, and two `detail` keys (`client_id`,
  `previous_client_id`). The action list is checked in code, so no migration is needed
  (data model 4.9).
- The reason of `app.stop` is a fixed sentence, so one stopped app shows the same text in the audit
  trail as the others.
