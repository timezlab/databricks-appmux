# Give a shared-SP child the appmux service principal's client ID and secret

**Status:** accepted 2026-11-18 on the owner's word (they stated the rule after the trade-off
below was put to them once). Replaces how the shared service principal reaches a child, which
[design 8.5](../product/v1-design.md#85-choosing-a-mode-and-delivering-credentials) and the spike 10 note in
[ADR 0011](0011-gate-credentials-and-platform-fields-behind-a-request-catalogue.md) described
(a one-hour token behind a token command). ADR 0011's request catalogue, the master switch and the
admin approval are unchanged, and ADR 0011 itself is not edited (ADRs are append-only).
**Date:** 2026-11-18
**Deciders:** Liam Lee

## Context

appmux runs as one Databricks App, so the platform gives it a service principal (SP) and puts its
OAuth pair in appmux's own environment as `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET`.
A standard Databricks App gets exactly that pair, and the Databricks SDK, the CLI and most app
code read it and sign in with `oauth-m2m`.

Until now a child that was delegated the shared SP did **not** get the pair. appmux minted a
one-hour token with it, kept the token in a file in the child's run directory, and set
`DATABRICKS_AUTH_TYPE=databricks-cli` and `DATABRICKS_CLI_PATH` so the SDK would ask a stub script
for the file's content (spike 10: a static token fails once it expires, so a command had to renew
it). It worked for code that builds its client with the SDK defaults.

It broke the first real app that was tried, FinNeural. Its backend reads `DATABRICKS_CLIENT_ID` and
`DATABRICKS_CLIENT_SECRET` itself and builds the M2M request by hand, so under appmux it stopped
with `DATABRICKS_CLIENT_ID is missing`. That app is a standard Databricks App
([constitution I](../../.specify/memory/constitution.md)): appmux must not require its source to
change, and an app that works when deployed alone must work when hosted. The token command made a
child behave differently from a standalone deployment, and any code that reads the pair directly
could not tell why.

The owner's rule, in two lines:

1. A child that has only the shared SP gets appmux's `DATABRICKS_CLIENT_ID` and
   `DATABRICKS_CLIENT_SECRET`, the same pair it would have if it were deployed on its own.
2. A child that sets up its own SP overwrites that pair with its own.

## Decision

- **The shared SP delivers the pair.** For an app with `shared_sp_delegated` set and the master
  switch `shared_sp_delegation_allowed` on, the child's environment gets `DATABRICKS_CLIENT_ID` and
  `DATABRICKS_CLIENT_SECRET` with appmux's own values. It gets **no** `DATABRICKS_AUTH_TYPE`, no
  `DATABRICKS_CLI_PATH` and no `DATABRICKS_TOKEN`: the SDK then signs in with `oauth-m2m` and
  renews its tokens itself, exactly as it would for an app deployed alone.
- **An own SP wins.** An app whose `app_config` names an `own_sp_client_id_key` and an
  `own_sp_client_secret_key` gets those two secret values under the same two names. It overwrites
  the shared pair. If the own pair cannot be given (a resource is missing or unverified), the start
  is refused and the shared pair does **not** stand in: an app that asked for a smaller reach never
  quietly gets a larger one ([FR-056](../../specs/001-appmux-v1/spec.md)).
- **The gate does not move.** Two things must hold: the master switch is on, and a platform admin
  has delegated the shared SP to the app with a reason (ADR 0011). A child without both gets
  nothing.
- **The token machinery is deleted.** `ClientCredentialsMinter`, `SharedSpTokens`,
  `TokenFileKeeper`, the token command and the run-dir token file are removed, with their tests.
- **The UI and docs say what is lent.** The platform page, the app page, the request form and the
  Settings warning name the client ID and secret, say that a revoke does not recall a copy, and
  say that the change reaches a child at its next start.

## Alternatives considered

- **Keep the token command and add the pair as well.** Then the child holds the long-lived secret
  and the short-lived token, and the SDK would still pick the token command, so code that reads the
  pair and code that uses the SDK defaults would behave differently. It keeps all the cost and
  none of the benefit. Rejected.
- **Keep the token command and ask the child to read the token.** Needs the child's source to
  change (constitution I). Rejected.
- **Tell the owner of FinNeural to use their own SP.** Works today and remains the safer mode, but
  it makes the shared SP useless for any app that reads the pair, and it is a workaround for appmux
  being unlike a standalone deployment. The owner chose to remove the difference.
- **Mint a pair for each child** (a new SP per app). The platform makes an SP only with an App;
  appmux cannot create one without a workspace-admin right (design premise 1). Rejected.

## Consequences

- **The child holds appmux's long-lived client secret.** Before, a leaked token lasted an hour;
  now a copy of the secret stays valid until the SP's secret is changed. Re-creating the App gives
  a new SP and so ends it ([ADR 0022](0022-stop-every-app-when-the-appmux-service-principal-changes.md)).
- **A revoke does not recall a copy.** Revoking the delegation, or turning the master switch off,
  stops the pair from being given at the child's **next start** only. A copy the app already
  kept, or a token it already minted, works until the SP's secret changes. A running child is not
  stopped by a revoke ([TD-020](../tech-debt.md): the environment is built at start, so a delegation
  also reaches a running child only after a restart).
- **The reach is unchanged, the duration is longer.** The child can reach everything the appmux SP
  reaches, as before, including every secret scope it can read and the registry's writer role
  (design 8.4). What changed is how long a leaked credential works.
- **Narrowing the lent token's scopes (design 8.4, measure O3) is no longer possible**, because the
  child mints its own tokens with the scope it asks for. It was a measure, never a shipped feature.
- **Own SP and OBO remain the safer modes**, and the admin UI says so where an admin grants the
  shared SP. The master switch (default off) and the reason on every grant are what keep this
  rare.
- **The host-backed run directory has no caller.** It existed for the token files. The seam
  (`RunDirFiles`, `SandboxSpec.host_run_dir`, `watch_run_dir`) stays and is recorded in
  [TD-004](../tech-debt.md); a later change can delete it.
- **A child deployed alone and a child under appmux with the shared SP see the same environment
  variables for the SP.** A test asserts it, so the difference cannot return unnoticed.
