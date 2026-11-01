# appmux — Product Design

**Status:** pre-alpha
**Owner:** Liam Lee
**Last updated:** 2026-10-30

## Problem

Every Databricks App takes one app slot in its workspace and keeps compute
running for as long as it is deployed. Two costs follow:

- **A hard ceiling** — a workspace can host at most 100 apps.
- **Always-on compute per app** — many small or lightly used apps each pay for
  their own running app.

## Product

**appmux** is a reverse proxy that runs as one Databricks App and lets many
child apps share that single slot. Child apps stay ordinary Databricks Apps —
each keeps its own `app.yaml` and can still be deployed independently.

To bring a child into appmux, register it in the registry with its source path
and configuration. appmux downloads the source, builds the environment, starts
the app, checks its health, and only then opens its route. Adding or updating a
child therefore does not interrupt the apps already running.

## Users

Teams that run many Databricks Apps in one workspace and are limited by the
per-workspace app cap or by the always-on cost of one app per child.

## Goals

- Host many child apps behind a single Databricks App slot.
- Keep child apps unchanged: own `app.yaml`, independently deployable.
- Onboard a child with a registry entry only (source path + configuration).
- Open a child's route only after it has been fetched, built, started, and
  found healthy.
- Add or update a child without interrupting running apps.

## Non-goals

- Replacing Databricks Apps — children remain standard Databricks Apps and
  appmux does not change how they deploy on their own.
- Requiring changes inside a child app — onboarding never asks for edits to the
  child's `app.yaml` or source.

## Decisions and open questions

The v1 design ([v1-design.md](v1-design.md)) answers the questions this page
used to list as open. Each answer is an ADR, and all are `accepted`.

| Question | Settled by |
|---|---|
| Stack — language, runtime, proxy framework | [0012](../decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md): Python services, Next.js admin UI as a static export served by the edge; [0014](../decisions/0014-build-the-edge-on-the-environments-http-stack.md): uvicorn, Starlette and httpx |
| Registry shape — where it lives, how appmux notices changes | [0004](../decisions/0004-keep-decisions-in-a-nine-table-lakebase-registry-with-split-writers.md) (accepted; spike 9 passed, spike 1 failed as written and the owner accepted its residual on 2026-10-30) |
| Source locations — kinds of source path, how appmux authenticates | [0005](../decisions/0005-run-releases-from-immutable-snapshots-and-swap-after-health.md): a workspace folder, read with the OBO token of whoever presses Update; Git is later |
| Environment build — dependencies and isolation between children | [0007](../decisions/0007-resolve-packages-environment-first-with-a-wheel-overlay.md) and [0003](../decisions/0003-isolate-children-with-unprivileged-namespaces-and-a-filtering-pump.md) |
| Routing — how a child is addressed | [0006](../decisions/0006-route-by-path-prefix-behind-a-routeselector.md): path prefix only in v1 |
| Identity and permissions — user identity, credentials, the boundary between children | [0010](../decisions/0010-mirror-databricks-permissions-and-give-platform-admins-implicit-access.md), [0011](../decisions/0011-gate-credentials-and-platform-fields-behind-a-request-catalogue.md), [0009](../decisions/0009-attach-resources-with-the-attachers-own-token-and-reference-secrets.md) |
| Trust model — what appmux validates and constrains before running child source | [0003](../decisions/0003-isolate-children-with-unprivileged-namespaces-and-a-filtering-pump.md), [0008](../decisions/0008-follow-the-standalone-app-yaml-contract.md), [0011](../decisions/0011-gate-credentials-and-platform-fields-behind-a-request-catalogue.md) |
| Shared resources — keeping one child's load from starving the others | [0005](../decisions/0005-run-releases-from-immutable-snapshots-and-swap-after-health.md) (memory budgets, admission control); process counts are bounded per sandbox and CPU stays an accepted gap with a measured mitigation (spike 7; [0003](../decisions/0003-isolate-children-with-unprivileged-namespaces-and-a-filtering-pump.md)) |
| License | [0013](../decisions/0013-license-appmux-under-apache-2-0.md): Apache-2.0 |

**Still open:** what the spikes could not test (the assumptions A1–A5 of the spec
were confirmed on 2026-10-30). The spikes in the first feature spec
([specs/001-appmux-v1/spec.md](../../specs/001-appmux-v1/spec.md)) ran on
2026-10-30 on the Free Edition workspace only; spike 3, which needed a paid
workspace, was dropped. Their results are in the
[spikes log](../../specs/001-appmux-v1/spikes/README.md), each entry lists what
it did not cover, and the [plan](../../specs/001-appmux-v1/plan.md) fixes what
the design left to it.
