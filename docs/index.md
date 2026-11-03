# appmux Documentation Index

Navigation only — content lives in the linked documents.

## Entry points (repo root)

- [README.md](../README.md) — human entry point: what appmux is, how it works, status.
- [AGENTS.md](../AGENTS.md) — canonical agent entry point and routing table.
- [ARCHITECTURE.md](../ARCHITECTURE.md) — module map with what exists, child app lifecycle, key boundaries.
- [LICENSE](../LICENSE) — Apache-2.0 ([ADR 0013](decisions/0013-license-appmux-under-apache-2-0.md)).
- [specs/001-appmux-v1/spec.md](../specs/001-appmux-v1/spec.md) — the active feature spec: appmux v1 and the spikes that gated its plan.
- [specs/001-appmux-v1/plan.md](../specs/001-appmux-v1/plan.md) — the implementation plan: stack, source layout, decisions from each spike; with [research](../specs/001-appmux-v1/research.md), [data model](../specs/001-appmux-v1/data-model.md), [contracts](../specs/001-appmux-v1/contracts/) and [quickstart](../specs/001-appmux-v1/quickstart.md).

## Product

- [product/DESIGN.md](product/DESIGN.md) — standing product direction: problem, product, goals, non-goals, and which ADR settles each early question.
- [product/v1-design.md](product/v1-design.md) — what appmux v1 is and how it behaves: architecture, lifecycle, isolation, routing, credentials, registry, roles, failure modes, threat model. Planned, not implemented.
- [product/v1-data-model.md](product/v1-data-model.md) — the nine Lakebase system tables for v1: business view, every table with columns, example rows and DDL, who writes what, walkthroughs, retention. Planned, not implemented.
- [product/v1-design-brief.md](product/v1-design-brief.md) — decision record of the 2026-10-29 and 2026-10-30 brainstorm: constraints, alternatives that lost, success criteria, spikes that gate the plan.

## Decisions (ADRs — append-only)

- [0001 — Adopt spec-driven development with Spec Kit](decisions/0001-adopt-spec-driven-development-with-spec-kit.md) — size-gated `specs/NNN-*`, constitution for standing principles.
- [0002 — Harness content in `.agents/`, linked into `.claude/`](decisions/0002-keep-harness-content-in-agents-with-claude-links.md) — real files under `.agents/`, symlinks from `.claude/`, hooks wired in settings.
- [0003 — Isolate children with unprivileged namespaces and a filtering pump](decisions/0003-isolate-children-with-unprivileged-namespaces-and-a-filtering-pump.md) — user/mount/net/pid namespaces, veth + `AF_PACKET` pump + `slirp4netns`, fail-closed `self_check()`.
- [0004 — Nine-table Lakebase registry with split writers](decisions/0004-keep-decisions-in-a-nine-table-lakebase-registry-with-split-writers.md) — decisions and history only; platform data written with the platform admin's OBO role. *Accepted 2026-10-30 (spike 9 passed; spike 1 found the OBO residual, which the owner accepted).*
- [0005 — Immutable snapshots, swap after health](decisions/0005-run-releases-from-immutable-snapshots-and-swap-after-health.md) — release lifecycle, rollback to any release, locks and caps.
- [0006 — Path-prefix routing behind a `RouteSelector`](decisions/0006-route-by-path-prefix-behind-a-routeselector.md) — the only v1 routing mode; no body rewriting.
- [0007 — Environment-first packages with a wheel overlay](decisions/0007-resolve-packages-environment-first-with-a-wheel-overlay.md) — wheels in a content-addressed store, read-only overlay.
- [0008 — Standalone `app.yaml` contract](decisions/0008-follow-the-standalone-app-yaml-contract.md) — `command`/`env` only, `databricks.yml` as an offer, requirements checked first.
- [0009 — Resources attached with the attacher's own token; secret references](decisions/0009-attach-resources-with-the-attachers-own-token-and-reference-secrets.md) — attach check (the SQL `secret()` call for a secret), `access_as`, secret references. *Accepted (spikes 11 and 16).*
- [0010 — Databricks permissions and implicit platform-admin access](decisions/0010-mirror-databricks-permissions-and-give-platform-admins-implicit-access.md) — `CAN_USE`/`CAN_MANAGE`, groups via SCIM, two view modes.
- [0011 — Credentials and platform fields behind a request catalogue](decisions/0011-gate-credentials-and-platform-fields-behind-a-request-catalogue.md) — OBO / shared SP / own SP, three request kinds, three gates.
- [0012 — Python services, Next.js admin UI](decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md) — static export served by the edge.
- [0013 — Apache-2.0](decisions/0013-license-appmux-under-apache-2-0.md) — licence and the `slirp4netns` arm's-length rule.
- [0014 — The edge on the environment's HTTP stack](decisions/0014-build-the-edge-on-the-environments-http-stack.md) — uvicorn, Starlette, httpx and `psycopg`; the base layer grows by one package.
- [0015 — `slirp4netns` in the release zip, with its source](decisions/0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md) — the zip deploys as unpacked; the GPL source travels inside it, and a written offer covers the LGPL libraries in the binary. *Accepted 2026-11-01.*
- [0016 — Admin UI on Tailwind and shadcn, tests outside `src/`](decisions/0016-build-the-admin-ui-with-tailwind-shadcn-and-tests-outside-src.md) — tokens, CSP-safe primitives, Recharts, `web/` layout; extends 0012.
- [0017 — Admin UI sub-pages and Databricks red](decisions/0017-split-the-admin-ui-into-sub-pages-and-use-databricks-red.md) — five sub-pages each for the app and platform pages, red primary with non-hue danger cues, the Databricks mark in the header; extends 0016.
- [0018 — Admin UI layout rules and the Settings save bar](decisions/0018-settle-the-admin-ui-layout-rules-and-give-settings-one-save-bar.md) — one field anatomy, control height, button order, spacing scale and table pattern; the launcher as a searchable table; Settings as one list with one save bar; a flat capacity strip for memory; extends 0017.
- [0020 — A service principal may hold an app grant](decisions/0020-let-a-service-principal-hold-an-app-grant.md) — a `service_principal` grant names another App's SP by its application id, `CAN_USE` only, instead of opening the child to `everyone`; extends 0010. *Proposed 2026-11-03.*

## Reference

- [reference/agent-harness.md](reference/agent-harness.md) — layout, installed items, hook wiring, local changes to upstream vibe, how to update.
- [reference/databricks-apps-platform.md](reference/databricks-apps-platform.md) — the Apps platform: a one-page *Platform at a glance* (container, privileges, proxy, identities, Lakebase, storage, packages, lifecycle), then the detail of the request path, identities, `app.yaml` and bundle resources, with the spike measurements.
- [reference/databricks-free-edition.md](reference/databricks-free-edition.md) — Free Edition limits (documented), the container tables measured by the probe and the spikes, what is still unconfirmed, and what it means for development.
- [reference/mcp-and-a2a.md](reference/mcp-and-a2a.md) — MCP and A2A between Apps inside and outside appmux: the three hops, what each failure looks like (302, 401 `{}`, 404), who the target sees, the shared-SP and `CAN USE` setup, all measured on the Free Edition workspace.

## Debt

- [tech-debt.md](tech-debt.md) — flat, grep-able debt log.
