# Implementation Plan: appmux v1

**Branch**: `001-appmux-v1` | **Date**: 2026-10-30 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from [`specs/001-appmux-v1/spec.md`](spec.md)

**Status**: [tasks.md](tasks.md) generated 2026-10-30; implementation started on 2026-10-30 at the owner's request (`/speckit-implement`) and runs in waves — `tasks.md` is ticked only for work whose tests pass, and its open items are the record of what is not done. Owner decisions in this plan: spec question 5 answered (a) on 2026-10-30, so [ADR 0004](../../docs/decisions/0004-keep-decisions-in-a-nine-table-lakebase-registry-with-split-writers.md) is `accepted`. Assumptions A1–A5 were confirmed as written on 2026-10-30.

## Summary

appmux v1 is one Python 3.11 ASGI service that runs as a single Databricks App. It sits behind the platform's proxy, selects a child by path prefix, checks permissions, and forwards over a per-child unix socket into a sandbox built from unprivileged Linux namespaces. A reconciler takes each release through fetch → build → start → health → swap, the registry lives in Lakebase, and a Next.js static export is the admin UI. The behaviour is fixed by [design](../../docs/product/v1-design.md) and [data model](../../docs/product/v1-data-model.md); the 18 spikes that ran (3 was dropped) fixed what the platform allows. This plan decides what the design left to it: the HTTP stack ([ADR 0014](../../docs/decisions/0014-build-the-edge-on-the-environments-http-stack.md)), the source layout, tooling, the numbers behind the caps, and the order in which to prove it.

## Technical Context

**Language/Version**: Python 3.11 for services (the App's runtime: 3.11.17); Node 22 only to build the admin UI before deploy, never in the slot ([ADR 0012](../../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md))

**Primary Dependencies**: from the App environment, so nothing added to the base layer: `uvicorn`, `starlette`, `httpx`, `websockets`, `pydantic`, `pyyaml`, `databricks-sdk`. One addition: `psycopg` 3 with its binary wheel, vendored as a hashed wheel. `slirp4netns` is never committed; the release zip carries the upstream static binary with its source ([ADR 0013](../../docs/decisions/0013-license-appmux-under-apache-2-0.md), [ADR 0015](../../docs/decisions/0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md), accepted). Rationale: [ADR 0014](../../docs/decisions/0014-build-the-edge-on-the-environments-http-stack.md) and [research.md](research.md#d1-http-stack-and-registry-driver).

**Storage**: Lakebase (Postgres) for the nine tables; the appmux SP's workspace folder as the content store (500 MB per file, spike 4); `/tmp` for overlays and run directories, which a stop and start wipes.

**Testing**: `pytest`. Pure logic (`authz`, `routing`, `rewrite`, the egress rule engine, the `app.yaml` parser, the reconciler state machine) runs locally and is written test-first. Sandbox and platform behaviour runs only on a Databricks container, driven by a sync, deploy and log-fetch script. Details: [quickstart.md](quickstart.md).

**Target Platform**: a Databricks App container on Linux (kernel 6.1.188, AL2023, uid 1000, 8 GiB, about 2 CPUs of quota, no root, no cgroup writes, no `/dev/net/tun`), measured on Free Edition only. See [Platform at a glance](../../docs/reference/databricks-apps-platform.md#platform-at-a-glance).

**Project Type**: web service plus a static front end, with a sandbox launcher and egress pump in the same Python package.

**Performance Goals**: SC-001 load is 500 requests a second over 10 connections, paced (about 3 % of the CPU quota; the stand-in edge reached 10,850 requests a second). Pump throughput 100 MB/s measured. Idle cost per sandbox about 22 MB; the 100-app figure of about 2.2 GB is an estimate until SC-003.

**Constraints**: the proxy speaks HTTP/1.1, ends a request or stream at 300 s, allows 32 KB of headers, normalises paths and overwrites identity headers. A child gets no appmux environment. Every dependency added to appmux is paid by every child (base layer).

**Scale/Scope**: up to about 100 registered apps in one slot, 10 simulated in SC-003; 12 modules, 9 tables, 3 request kinds, 4 admin screens.

## Constitution Check

*Gate: before Phase 0 research; re-checked after Phase 1 design.*

| Principle | Check | Result |
|---|---|---|
| I. Child apps stay standard | The plan reads a child's `app.yaml` and `databricks.yml`, never writes them; no appmux-specific file exists; `$NAME` substitution mirrors the platform (spike 17). | pass |
| II. Health-gated routing | The reconciler is the only owner of the route table and it adds a route only after health; `failed` is never retried automatically. A test asserts no other code path calls the route-table setter. | pass |
| III. Non-disruptive change | The swap is atomic with a 30 s drain; admission control counts the update overlap; SC-001 compares each operation with a no-op control run. | pass |
| IV. Documentation ships with code | Every task that changes a surface or a module boundary carries its doc change; the plan itself updates [ARCHITECTURE.md](../../ARCHITECTURE.md), the platform reference and the debt log. | pass |
| V. Observable verification | [quickstart.md](quickstart.md) names the command and expected result for each success criterion; nothing is marked done without running it. | pass |

**After design (Phase 1):** no change. The layout below keeps every constraint of design 3.4 (the dependency direction is enforced by a test, not a convention) and adds no module the design lacks. No violations, so Complexity Tracking stays empty.

## Platform constraints that shape the plan

The platform is the narrowest part of this project. The full measured facts are in [Platform at a glance](../../docs/reference/databricks-apps-platform.md#platform-at-a-glance); this table says only what the plan does about each.

| Platform fact (Free Edition) | What the plan does |
|---|---|
| No root, no cgroups, no `/dev/net/tun`; user namespaces work | Sandbox from `unshare`; pump on a veth pair and `AF_PACKET`; no hard CPU or memory cap, only the mitigations in the Decisions below ([TD-003](../../docs/tech-debt.md#td-003-cpu-and-memory-limits-are-best-effort)). |
| PID 1 does not reap orphans | The launcher sets itself as a subreaper. |
| About 2 CPUs of quota, 4 visible | All children are pinned to one CPU at `nice 19`; the edge, pump and reconciler run elsewhere. |
| Proxy: HTTP/1.1, 300 s, 32 KB headers, rewrites identity headers | The edge reads the client address from `X-Real-Ip` (the proxy overwrites it; `X-Forwarded-For` is appended to and is not a client address, spike 8); per-request timeout defaults stay below 300 s. |
| `/tmp` is wiped by a stop and start; a deploy keeps it | The overlay cache is rebuilt after every stop; nothing durable lives in `/tmp`. |
| The environment holds 132 packages, installed by the platform from `requirements.txt` | appmux's own dependencies are vendored wheels; the environment is the base layer for children. |
| Lakebase checks a token only at login and ignores scopes | Admin writes open a connection per write; the residual is accepted ([TD-002](../../docs/tech-debt.md#td-002-an-obo-childs-token-reaches-the-registry)). |
| Permission and group changes lag 1.5–6 min; nested groups 60–75 s | Group cache 300 s; the UI says a change can take minutes. |
| Measured on Free Edition only | Every number is labelled; the first deploy on a paid workspace re-runs `self_check` and SC-001 ([TD-005](../../docs/tech-debt.md#td-005-platform-measurements-come-from-free-edition-only)). |

## Decisions from the spikes

One row per spike that ran (spike 0 archived the probe; spike 3 was dropped). The design and ADRs already carry the corrected behaviour; this table is the plan's record of what each result fixed and where the plan relies on it. Alternatives for the open choices are in [research.md](research.md).

| Spike | Decision for the plan | Gates / source |
|---|---|---|
| 8 Proxy | The edge handles HTTP/1.1 and trusts the forwarded identity headers; H2C stays a "may". Client address from `X-Real-Ip`. | [ADR 0012](../../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md), design 7.1 |
| 17 `app.yaml` | Parse only `command` and `env`; `$NAME` replaced in `command` arguments only; a missing `valueFrom` key fails the release (the platform only logs). | design 5.4 |
| 5 Base path | Prefix routing with the framework variables of design 5.4; an app with absolute links is not hostable. | [ADR 0006](../../docs/decisions/0006-route-by-path-prefix-behind-a-routeselector.md) |
| 2 Filesystem | The child's root is built from an allow-list; `--ipc` is required; `self_check` carries the 18 canaries. | [ADR 0003](../../docs/decisions/0003-isolate-children-with-unprivileged-namespaces-and-a-filtering-pump.md), design 6.1 |
| 18 Overlay | Overlay wins through `PYTHONPATH`; `pip check` output is a warning on the release, never a failure; entry points are started with the overlay path set. | [ADR 0007](../../docs/decisions/0007-resolve-packages-environment-first-with-a-wheel-overlay.md) |
| 13 Wheel bootstrap | appmux's own `requirements.txt` starts with `--no-index --find-links ./wheels`; the fallback bootstrap is documented, not built. | design 13.1 |
| 14 Offline sources | Wheel coverage is reported at Update. A **conflicting overlay warns only** (the health gate is the safety net). The **Node offline cache is not shipped in v1** (design 15); Node apps with dependencies need `index` mode. | design 5.3, 15 |
| 1 OBO to Lakebase | Question 5 answered (a): the residual is accepted and stated; a platform admin's default view mode stays *without token*; no `postgres` scope is configured. | [ADR 0004](../../docs/decisions/0004-keep-decisions-in-a-nine-table-lakebase-registry-with-split-writers.md), [TD-002](../../docs/tech-debt.md#td-002-an-obo-childs-token-reaches-the-registry) |
| 9 Cascade, licence | Table-level grants to `appmux_platform_writers` as in the data model; `slirp4netns` is never committed; the release zip carries it with its source. | ADR 0004, [ADR 0013](../../docs/decisions/0013-license-appmux-under-apache-2-0.md), [ADR 0015](../../docs/decisions/0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md) |
| 12 Nested groups | Group membership from SCIM `GET /Me` with the person's own token, cached 300 s (up to 1 h after a failure); nested groups count. | [ADR 0010](../../docs/decisions/0010-mirror-databricks-permissions-and-give-platform-admins-implicit-access.md) |
| 4 Content store | A folder in the appmux SP's workspace; **one archive per release named by its digest** (about one API call) and wheels stored one file each by hash; a file over 500 MB is refused naming it; no volume fallback in v1. | [ADR 0005](../../docs/decisions/0005-run-releases-from-immutable-snapshots-and-swap-after-health.md) |
| 19 Admin UI | Static export served by the edge; the edge hashes each page's inline scripts at start for the CSP. | [ADR 0012](../../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md), design 7.3, 10.1 |
| 11 Secrets | A secret is a `scope/key` reference read by the appmux SP at child start; the attach check runs as the attacher with the `sql` scope. | [ADR 0009](../../docs/decisions/0009-attach-resources-with-the-attachers-own-token-and-reference-secrets.md) |
| 15 Workspace read | Update reads the source folder with the presser's token and scope `workspace.workspace:read`; an empty listing or a 404 is reported as "empty, or you lack access". | design 5.2 |
| 16 Attach checks | 8 of 14 types have a verifiable check; the rest stay `unverified` and cannot be used with `shared_sp`. The warehouse the checks use is a platform setting. | design 8.2 |
| 10 Shared-SP token | appmux mints the shared-SP token and refreshes it before expiry; a static variable is never used. | [ADR 0011](../../docs/decisions/0011-gate-credentials-and-platform-fields-behind-a-request-catalogue.md), design 8.5 |
| 6 Egress | Each child has a veth pair, an `AF_PACKET` pump and `slirp4netns`; the pump enforces the rule lists; idle pumps wake on `SIGHUP`, not polling. | ADR 0003, design 6.2 |
| 7 Throughput and hogs | CPU pin + `nice 19` + seccomp refusing `sched_setaffinity` (setting `child_cpu_pin`, proved by a canary); launcher is a subreaper; `RLIMIT_NPROC` 256 per sandbox; build limit 600 s; run-dir cap by size-limited tmpfs with a watchdog fallback; SC-001 load as above. | design 5.7, 6.4, [TD-003](../../docs/tech-debt.md#td-003-cpu-and-memory-limits-are-best-effort), [TD-004](../../docs/tech-debt.md#td-004-a-childs-run-directory-size-cap-is-unproven) |

**Defaults fixed by this plan** (all `platform_settings` keys with level of effect *all children*, tuned after the SC-001 and SC-003 runs; names added to the [data model](../../docs/product/v1-data-model.md#41-platform_settings)): `request_timeout_s` 120 (maximum 290, below the platform's 300 s), `max_request_body_mb` 100 (the largest size tested through the proxy), `max_concurrent_requests` 64 per app, `build_timeout_s` 600, `max_processes_per_child` 256, `child_cpu_pin` on, `attach_check_warehouse_id` unset (an attach of a type that needs it is refused naming the setting). Smaller items (the `unshare` mapping, the child variables the platform sets, the per-app CSP sandbox, `404` on a denied child route) are in [research D16](research.md#d16-smaller-items-the-design-left-to-the-plan).

## Project Structure

### Documentation (this feature)

```text
specs/001-appmux-v1/
├── spec.md              # the feature (owner-approved)
├── plan.md              # this file
├── research.md          # Phase 0: open choices with alternatives
├── data-model.md        # Phase 1: pointer to docs/product/v1-data-model.md and the plan's deltas
├── quickstart.md        # Phase 1: how each success criterion is proved
├── contracts/           # Phase 1: edge-http.md, admin-api.md, examples/ (one JSON per admin schema + manifest.json)
├── spikes/              # evidence and the log (frozen record)
├── checklists/
└── tasks.md             # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
src/appmux/
├── __main__.py          # entry: settings → uvicorn binds the port with the startup gate → (background) registry →
│                        # self_check → reconciler → gate opens to the edge
├── settings.py          # environment variables (a BaseModel over os.environ) and the platform_settings schema
├── edge/                # startup gate (gate.py), ASGI app: the pipeline of design 3.4, /_appmux mount, static UI, CSP
├── identity/            # forwarded headers → Actor; group lookup and cache
├── authz/               # authorize(actor, action, app); pure, table-driven
├── routing/             # RouteSelector, PrefixSelector; pure
├── rewrite/             # request headers; Location and Set-Cookie
├── upstream/            # httpx over a unix socket, WebSocket relay, caps
├── reconciler/          # release state machines, route table, swap, restart
├── sandbox/             # launcher (stage 1), pump, relay, rule engine, self_check, canary
├── packages/            # package modes, overlay build, wheel store
├── appyaml/             # app.yaml and databricks.yml parsing, requirements check
├── secrets/             # secret reference → value at child start
├── registry/            # psycopg access, write-identity rule, migrations loader
├── admin/               # JSON API handlers under /_appmux/api
└── metrics/             # one-minute aggregates and the batch flush
migrations/              # 0001_baseline.sql (the DDL of the data model)
web/                     # Next.js project; `npm run build` → web/out
scripts/                 # assemble-deploy, sync-deploy, fetch-logs, vendor-wheels, fetch-slirp4netns
tests/
├── unit/                # pure logic, run locally
├── contract/            # HTTP contracts against the ASGI app with a stubbed registry
├── integration/         # needs a Databricks container; marked and skipped elsewhere
├── fixtures/            # the 10 well-behaved and about 10 hostile child apps
└── load/                # SC-001 load generator
pyproject.toml           # ruff, mypy, pytest config; dependencies
uv.lock
app.yaml                 # appmux's own App config (the platform reads it from the deployed root)
.github/workflows/release.yml   # on a tag: build, test, attach appmux-<version>.zip to a GitHub Release
build/                   # generated, git-ignored: wheels/, requirements.txt, deploy/ (the folder that is
                         # deployed), slirp4netns/, appmux-<version>.zip
```

**Structure Decision**: one Python package organised by the modules of design 3.4 (feature over layer), a separate `web/` because it has its own toolchain and ships only its build output, and a root `app.yaml`. What the platform installs (`requirements.txt`, `wheels/`) is generated into the git-ignored `build/`, because a `requirements.txt` or a `uv.lock` at a deployed root makes the platform install by itself (measured in another project on a workspace without PyPI access; not measured on the Free Edition). `scripts/assemble-deploy.sh` builds the deployment folder `build/deploy/` from an allow-list; `sync-deploy.sh` uses it for development, and the release workflow uses it to publish `appmux-<version>.zip` so an operator downloads it instead of building. The zip carries the upstream `slirp4netns` binary with its licence text and source under `licenses/slirp4netns/`, and the repository never does ([ADR 0015](../../docs/decisions/0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md), accepted). A test (`tests/unit/test_module_boundaries.py`, standard-library `ast`) fails when an import points against the dependency direction of the design. No `shared/` or `common/` package: a function needed by two modules waits for the third use before it moves.

## Complexity Tracking

No constitution violations. The plan adds no module, service or dependency beyond what the design and [ADR 0014](../../docs/decisions/0014-build-the-edge-on-the-environments-http-stack.md) already state.

## Phase outputs

| Phase | Artifact | State |
|---|---|---|
| 0 | [research.md](research.md) | written |
| 1 | [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md) | written |
| 2 | [tasks.md](tasks.md) | written by `/speckit-tasks` on 2026-10-30; ticked as the waves land |
