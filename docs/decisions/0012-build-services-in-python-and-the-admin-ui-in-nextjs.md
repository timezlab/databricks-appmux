# Build the services in Python and the admin UI in Next.js

**Status:** accepted — the delivery form (a static export served by the edge)
was confirmed by spike 19, with a hash-based CSP.
**Date:** 2026-10-30
**Deciders:** Liam Lee (design approved 2026-10-30)

## Context

The first feature plan was to pick the language and record it. appmux runs as
one Databricks App with Python 3.11 and Node 22 available, and children run on
the parent's Python, so the packages installed in appmux's own environment are
every child's base layer ([ADR 0007](0007-resolve-packages-environment-first-with-a-wheel-overlay.md)).
The Databricks docs require an app to support H2C, while the probe measured
plain HTTP/1.1 from the proxy. On 2026-10-30 the user answered "python +
nextjs".

## Decision

The supervisor, edge, reconciler, sandbox launcher, egress pump, registry client
and JSON API are written in Python 3.11. The admin UI is a Next.js application
built as a **static export** before deploy, shipped with the appmux source and
served by the Python edge, which also serves the JSON API the pages poll every
few seconds, under a strict CSP. The edge accepts both HTTP/1.1 and H2C. A
faster, non-Python pump is built only if throughput is measured to need it. The
HTTP library, package layout and tooling are chosen in the first feature plan.

## Alternatives considered

- **Go or Rust for the data plane** — not chosen: the user chose Python, and
  every dependency of appmux adds to the base layer children share; revisit if
  the pump measures too slow.
- **Server-rendered UI from the Python edge, no front-end build** — the earlier
  plan; replaced by the user's choice of Next.js.
- **A Next.js server process inside the slot** — rejected: another long-running
  process and memory share in a slot whose budget the children need.

## Consequences

**Better:**
- One language for everything on the request path and in the sandbox launcher,
  and a conventional front-end stack for the admin screens.
- The export needs no Node process at run time.

**Worse:**
- Python carries every frame in the pump and every request in the edge;
  throughput is unmeasured (spike 7).
- A front-end build step joins the repository, and the export must work under a
  strict CSP with no inline script and a base path behind appmux's prefix, which
  is unverified (spike 19); if the platform cannot build a `package.json` in a
  Python App, the export is built before deploy and ships in the deployment
  folder.
- Dependency choices (Python and npm) enlarge the supply-chain surface the
  security rules require checking.

**Must now be true:**
- No Node process runs in the slot; the edge serves the exported files.
- The admin UI runs no inline script except those its CSP allows by SHA-256
  hash (spike 19), sends no cross-origin request, and every action goes through
  the JSON API behind `authorize()`.
- appmux's own requirements stay lean because they are the base layer of every
  child.

## Revisit if

Spike 7 shows Python cannot carry the pump or the edge at the load of success
criteria 1–3; spike 19 shows the export cannot meet the CSP; or the base-layer
coupling forces a split between appmux's dependencies and the children's.

## Spike results

**Spike 8, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-8--behaviour-of-the-databricks-proxy-2026-10-30)).**
The proxy spoke HTTP/1.1 to the app both for a standard-library server and for
a server that supports H2C (hypercorn 0.18.0); no H2C request was observed. The
edge must therefore handle HTTP/1.1; H2C support, which the vendor docs ask for,
stays a "may" until something is seen using it. The Decision's wording ("accepts
both") is satisfiable and unchanged. The "Revisit if" conditions are unchanged.

**Spike 19, 2026-10-30 (pass with hashes; [log](../../specs/001-appmux-v1/spikes/README.md#spike-19--nextjs-admin-ui-on-the-platform-2026-10-30)).**
A Next.js 16.4.3 static export (Turbopack, `basePath`, `trailingSlash`) renders
and polls behind a base path, but it cannot start under `script-src 'self'`
alone: every App Router page carries three executable inline scripts and every
Pages Router page one, and Chromium blocked them (3 and 1 violations; the page
stayed on "loading"). With `script-src 'self'` plus the SHA-256 of each page's
inline scripts, computed from the served file, both routers rendered, polled and
had zero violations. The "Must now be true" line "sends no inline script"
therefore becomes: the admin UI runs no inline script except those its CSP
allows by hash, and the edge computes the hashes from the export's HTML files
(they change with every page and every build). The export is still served by the
edge and no Node process runs in the slot. On the Free Edition platform the
same policy held behind the proxy (the proxy passed the header through and
injected no script), and the platform builds a `package.json` in a Python App
(`npm install`, then `npm run build`, then the app command), so the export may
be built at deploy; shipping it prebuilt stays the lean, because a build at
deploy runs `npm install` with install scripts in the parent App. The Decision is
unchanged and the status stays `accepted`; the "Revisit if" condition about the
CSP does not fire.

**Spike 7, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-7--throughput-hogs-forks-and-builds-2026-10-30)).**
The "Revisit if" condition about the pump and the edge does not fire. Free
Edition only, with a stand-in edge (asyncio, plain HTTP/1.1 proxying, no identity,
routing, ACL or metrics). The Python pump carried 100 MB/s toward the child and
59 MB/s back, using 124 % and 91 % of one CPU, and 1 KiB exchanges at 8,000,
25,000 and 28,000 per second at 1, 10 and 100 connections. That is far above what
the platform lets through (about 10 MB/s of body in spike 8, 11–12 MB/s for
package downloads). The stand-in edge, relay and a trivial child sustained 10,850
requests/s at 10 connections for about 130 µs of CPU per request, against about
500 requests/s proposed for SC-001. What limits the container is its CPU quota
(about 2 of the 4 visible CPUs), not the language. Two costs to carry: the pump
spends about 12 ms of CPU per MB, so a child's bulk egress is CPU shared with
every other app, and an idle pump should wake on a file change instead of
polling every 10 ms (3 % of one CPU for ten sandboxes). The real edge costs more
than the stand-in, so the plan measures it against the SC-001 load; the Decision
is unchanged and the status stays `accepted`.

## Update 2026-11-06 (T218): how often the dashboard polls

The Decision is unchanged and the status stays `accepted`. Its words "the pages poll every few
seconds" are made exact for the dashboard and an app's Insights, where the owner chose on
2026-11-06 to amend the documents to match the code rather than the code to match the documents:
the metrics panel polls **every 5 seconds for a range of up to 1 hour, every 15 seconds for a range
of up to 24 hours, and every 60 seconds for a longer range**. Up to a day the answer holds up
to 1440 rows and changes once a minute; a longer range is read in steps of 5 or 15 minutes whose
newest step also grows once a minute, so asking once a minute is enough. The tiers are in `web/src/lib/dashboard/scope.ts`; [FR-051](../../specs/001-appmux-v1/spec.md),
[design 10.1](../product/v1-design.md) and [TD-009](../tech-debt.md) name them. Other pages keep
their own intervals (the logs page polls every 2 seconds).
