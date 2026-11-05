# Build the edge on the environment's HTTP stack

**Status:** accepted
**Date:** 2026-10-30
**Deciders:** Liam Lee (plan for [appmux v1](../../specs/001-appmux-v1/plan.md))

## Context

[ADR 0012](0012-build-services-in-python-and-the-admin-ui-in-nextjs.md) chose
Python and left the HTTP library, package layout and tooling to the first plan.
Every package in appmux's own environment is also the base layer of every child
([ADR 0007](0007-resolve-packages-environment-first-with-a-wheel-overlay.md)),
so each extra dependency is paid by up to 100 apps. The Databricks Apps
environment already ships `uvicorn 0.30.6`, `starlette 0.38.6`, `httpx 0.28.1`,
`websockets 12.0`, `pydantic 2.13.4`, `pyyaml`, `uvloop`, `httptools`, `h11`,
`orjson` and `databricks-sdk 0.33.0` (spike 18, Free Edition). Lakebase needs a
Postgres driver, which the environment lacks; spike 1 logged in with
`psycopg 3.3.6`.

## Decision

- **Server:** `uvicorn` running one ASGI application. `/_appmux/` goes to a
  Starlette sub-application (admin API and the exported UI); every other path
  goes to the edge pipeline of [design 3.4](../product/v1-design.md#34-module-boundaries).
- **Upstream:** `httpx` with a unix-socket transport for requests and
  `websockets` (`unix_connect`) for upgrades.
- **Schemas and settings:** Pydantic models. The environment settings are a
  `BaseModel` validated from `os.environ` at start: `BaseSettings` lives in
  `pydantic-settings`, which the environment lacks (spike 18 lists 132
  packages, none of them it), and one more base-layer package is not worth it.
- **Registry:** `psycopg` 3, async API, with no pool library. The appmux SP
  keeps one connection and reopens it when its token is minted anew; a platform
  admin's write opens one connection with that admin's token and closes it.
- **The only new runtime dependency is `psycopg` (and its binary wheel).** It is
  vendored as a hashed wheel in the deployment folder
  ([design 13.1](../product/v1-design.md#131-what-the-operator-sets-up)) after the
  OSV.dev and publish-age checks of the security rules.
- **Tooling:** `uv` with a committed `uv.lock`, `ruff` (format and lint),
  `mypy`, `pytest`. A stdlib `ast` test enforces the dependency direction of
  design 3.4. The layout is in the [plan](../../specs/001-appmux-v1/plan.md#source-code-repository-root).

## Alternatives considered

- **`asyncpg`** — not chosen: spike 1 measured `psycopg`, and a second driver
  would need its own login and token-expiry measurements.
- **A pool library (`psycopg_pool`)** — not chosen: one more base-layer package
  for a registry that sees one SP connection and rare admin writes.
- **A different ASGI server or framework (`hypercorn`, FastAPI)** — not chosen:
  `hypercorn` and FastAPI are not in the environment; spike 8 saw only HTTP/1.1,
  which `uvicorn` serves.

## Consequences

**Better:**
- The base layer grows by one package instead of five or six.
- Everything on the request path is already installed and matches what the
  spikes ran against.

**Worse:**
- appmux is pinned to the environment's versions of `starlette`, `uvicorn` and
  `httpx`; a platform upgrade can change them
  ([design 5.3](../product/v1-design.md#53-build-and-package-sources) re-checks
  at every start).
- `uvicorn` does not speak H2C; ADR 0012 keeps that a "may".

**Must now be true:**
- The edge's measured capacity meets SC-001 (500 requests a second over 10
  connections) in the first load run; the stand-in measured 10,850 requests a
  second without identity, routing, ACL or metrics.
- No dependency is added to appmux's requirements without a check of the
  base-layer cost and a hashed wheel.

## Measured (2026-11-05)

The real edge ran at SC-001's rate inside the Free Edition dev App's container
([spike log](../../specs/001-appmux-v1/spikes/README.md#sc-001-the-edge-at-500-requests-a-second-t086)),
which is the first "Must now be true" item above:

- 15 000 requests in 30 s at 500 a second over 10 connections to one sandboxed child: 0 failed,
  p95 3.98 ms, the edge process used 62 % of one core (T086).
- 145 000 requests at the same rate across a control run and six operations (register, update,
  swap, a failed-health release, three builds at once, an egress edit): 0 failed; the edge used
  68 % of a core in the control and up to 86 % while three builds ran, when p95 rose to 1.9 s
  ([SC-001 table](../../specs/001-appmux-v1/spikes/README.md#sc-001-no-disruption-under-operations-t108), T108).

A second invocation of the same tests, in order with the rest of the quickstart on one head
([spike log](../../specs/001-appmux-v1/spikes/README.md#plan-checks-the-quickstart-run-2026-11-05)),
served the whole load again with no failed request, and moved two figures: the edge test read p95
247 ms and 75 % of a core (the first minutes after the App's restart; a later control run at the same
rate read a median slice p95 of 5.2 ms), and the heavy-build slice p95 was 2.2 s with the edge at
85 % ([the edge at 500 a second, twice](../../specs/001-appmux-v1/spikes/README.md#the-edge-at-500-a-second-twice)).

The revisit trigger below is not met. One workload (an empty `GET`), one container, Free Edition
only; whether 62 to 86 % of a core is room to spare is the owner's judgement, and no figure for it
is written here.

## Revisit if

The first load run of the real edge misses SC-001 by more than a tuning change
can recover, or the environment drops one of the listed packages.
