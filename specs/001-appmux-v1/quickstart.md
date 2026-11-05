# Quickstart: proving appmux v1

How each success criterion of the [spec](spec.md) is checked. The code was written in waves and [ARCHITECTURE.md](../../ARCHITECTURE.md) marks what exists; the order follows [research D15](research.md#d15-order-of-proof). The results of the first complete run, on the Free Edition workspace only, are in [the spike log](spikes/README.md#plan-checks-the-quickstart-run-2026-11-05).

## Prerequisites

- A development machine with Python 3.11 and `uv`; Node 22 for the UI build.
- A Databricks Free Edition workspace with the CLI configured, **or** a paid workspace once one is authorised (the owner dropped spike 3: nothing here targets one).
- On the workspace: one App named `appmux`, a Lakebase project with the group roles `appmux_sp` and `appmux_platform_writers` and the other roles of [design 13.1](../../docs/product/v1-design.md#131-what-the-operator-sets-up), and a workspace folder for the content store. Mask workspace identifiers in anything written to the repository.

## 1. Local checks (any machine)

```
uv sync
uv run ruff format --check . && uv run ruff check .
uv run mypy src
uv run pytest tests/unit tests/contract tests/load
```

Expected: formatting and lint clean, no type errors, all unit and contract tests pass. The unit set covers `authz` as a table (every row of [design 9.2](../../docs/product/v1-design.md#92-who-may-do-what)), `routing`, `rewrite`, the egress rule engine, the `app.yaml` parser, the reconciler state table, the module-boundary test and the baseline-migration check.

## 2. Deploy and self_check (Databricks container)

```
scripts/vendor-wheels.sh      # once, and again when uv.lock changes: build/requirements.txt, build/wheels/
scripts/fetch-slirp4netns.sh  # once, and again when the pin changes: build/slirp4netns/
scripts/sync-deploy.sh        # assemble build/deploy/, sync it to the workspace folder, deploy, wait
scripts/fetch-logs.sh         # fetch the app logs, identifiers masked
scripts/run-remote-tests.sh   # the container scenarios below, from this machine, as the CLI's token owner
scripts/run-container-tests.sh  # the ones that need to run inside the App (capacity, /proc); restarts the App
```

Expected: once the process is up, and until the registry is loaded and the start-up steps (the content store, `self_check`, the children) have run, `/` answers `200` "starting" and every other path `503` (the startup gate, [design 5.6](../../docs/product/v1-design.md#56-crash-restart-and-shutdown)); before the process is up the Apps proxy answers its own `502` page, which appmux does not control (on 2026-11-05 that lasted about 77 s and the gate was visible for about 10 s, [log](spikes/README.md#the-startup-gate-during-a-deploy)); the app is not restarted by the platform while `self_check` runs; then the platform page shows `self_check` **ok** with every check of [design 6.4](../../docs/product/v1-design.md#64-self-check-failures-and-gaps) listed; no child exists yet.

## 3. Capacity of the real edge (first integration scenario)

`tests/integration/test_edge_capacity.py`, run inside the App with `scripts/run-container-tests.sh` (the load generator and the edge's pid must share the container): 500 requests a second over 10 connections, paced, against a fast fixture app registered through the API. Expected: p95 and error rate reported, CPU of the edge process reported. If the edge cannot hold the rate with room to spare, stop and revisit [ADR 0014](../../docs/decisions/0014-build-the-edge-on-the-environments-http-stack.md) before building further.

## 4. The four success criteria

| Criterion | Scenario | Expected |
|---|---|---|
| **SC-001** No disruption | Steady load on app A (above); register B; update B; update A with a good release; update A with a release that fails its health check (A keeps serving); a heavy build in parallel; an egress rule edit | each operation causes no more failed requests than a no-op control run of the same length; p95 before and during is reported |
| **SC-002** Fail-closed isolation | Run `self_check` on a healthy sandbox; then weaken it once each way (skip the `/proc` overmount, skip the capability drop, disable the pump filter) | healthy: pass. Each weakening: fail, no route opens, the status page names the check. The hostile fixtures are contained |
| **SC-003** Capacity | Register the 10 well-behaved fixtures, start all, load them; try an 11th that exceeds the admission budget | RSS per app (idle, loaded), sandbox overhead, start time and CPU reported, total against the slot limit; a 100-app figure given as an estimate; the 11th refused with `needs … MB, … MB free` |
| **SC-004** No hand-edited database | A scripted walkthrough through the UI or API ([contracts/admin-api.md](contracts/admin-api.md)): the full list in SC-004 of the [spec](spec.md#success-criteria-mandatory) (register, request and approve, delegation and settings, rename by request, egress rule, platform-admin source view, sharing, resources and `databricks.yml` offers, the release actions, restart and stop) | no SQL typed; the final database state matches the expected rows |

## 5. Hostile fixtures

`tests/fixtures`: the environment snooper, package-environment writer (must get `EROFS`), localhost/container-IP/metadata scanner, start-up crash, crash loop, always-500 health check, slow start, memory hog, fork bomb, and a secret-and-registry snooper. Each is registered through the normal path and must be contained without affecting a neighbour under load.

## Not covered here

Measurements not yet made (the paid workspace, a 100-app run instead of an estimate, fairness inside the pinned CPU, a fast allocator between two watchdog readings) are listed in [TD-003](../../docs/tech-debt.md#td-003-cpu-and-memory-limits-are-best-effort) and [TD-005](../../docs/tech-debt.md#td-005-platform-measurements-come-from-free-edition-only).
