# Research: appmux v1

Phase 0 of the [plan](plan.md). The spikes answered the platform questions ([log](spikes/README.md)); this file records the choices the design left to the plan, each as Decision / Rationale / Alternatives. No `NEEDS CLARIFICATION` remains. Numbers marked *estimate* or *untested* are checked by the named scenario in [quickstart.md](quickstart.md).

## D1. HTTP stack and registry driver

- **Decision.** `uvicorn` serving one ASGI app; Starlette for `/_appmux/`; `httpx` over a unix socket and `websockets` for upstream; `psycopg` 3 (async, no pool library) for Lakebase. Recorded in [ADR 0014](../../docs/decisions/0014-build-the-edge-on-the-environments-http-stack.md).
- **Rationale.** Six of the seven packages are in the App environment already (spike 18), so the base layer every child shares grows by one package; `psycopg` is the driver spike 1 measured.
- **Alternatives.** `asyncpg` (a second driver to measure), `psycopg_pool` (one more package for one SP connection), `hypercorn`/FastAPI (not installed). **Verified 2026-10-30 (T003):** `psycopg` 3.3.6 and `psycopg-binary` 3.3.6 (both published 2026-09-18, six weeks earlier; the binary wheel for CPython 3.11 `manylinux2014_x86_64` exists, 5.3 MB) have no OSV.dev advisory, `MAL-` included; `ruff` 0.17.1, `mypy` 2.4.0, `pytest` 9.1.1, `types-PyYAML` 6.0.12.20260906 and the build backend `uv_build` 0.12.23 have none either. Both psycopg wheels were downloaded and their SHA-256 matches PyPI; neither holds a `.pth` file or a script. `psycopg` needs `typing-extensions`, which the App environment already holds.

## D2. Layout and tooling

- **Decision.** One package `src/appmux/` split by the modules of design 3.4, `web/`, `tests/`, a root `app.yaml`, and a git-ignored `build/` for the generated `requirements.txt`, `wheels/` and deployment folder; `uv` with a committed `uv.lock`; `ruff` format and lint, `mypy`, `pytest`; an `ast` test for the dependency direction. Layout in the [plan](plan.md#source-code-repository-root).
- **Rationale.** Matches the design's modules one to one, so code that changes together lives together; the tools are the ones the Python rules name or imply, and none of them enters the deployed environment.
- **Alternatives.** `pyright` (pulls a Node download at install), a `core/` + `adapters/` split (layers the design does not have), a monorepo of several packages (only one deployable exists).
- **Verified 2026-10-30 (T171): the npm packages of `web/`.** Direct dependencies, exact versions with their publish dates: `next` 16.4.2 (2026-10-15), `react` and `react-dom` 19.3.1 (2026-10-14), `zod` 4.6.7 (2026-10-12); dev: `typescript` 6.0.3 (2026-04-16), `eslint` 10.12.0 (2026-10-02), `@eslint/js` 10.0.1 (2026-02-06), `typescript-eslint` 8.71.2 (2026-10-12), `eslint-config-prettier` 10.1.8 (2025-07-18), `eslint-plugin-react-hooks` 7.1.1 (2026-04-17), `prettier` 3.9.10 (2026-10-10), `@types/node` 22.20.5 (2026-10-01), `@types/react` and `@types/react-dom` 19.3.0 (2026-09-09). All fourteen are at least 14 days old and each passed `.agents/skills/security-audit/scripts/check-package.py` (OSV.dev, `MAL-` advisories included, plus registry metadata). The OSV `querybatch` over all 196 entries of `web/package-lock.json` (the direct packages and everything they pull in) returned no advisory, `MAL-` included. No lock entry has `hasInstallScript`, so no package runs an install script and none needed an exception; `web/.npmrc` still sets `ignore-scripts=true` and `save-exact=true`, so a later dependency that adds one does not run it. Every `resolved` URL is `https://registry.npmjs.org/`. The youngest locked packages are `@babel/parser`, `@babel/traverse` and `@babel/generator` 7.29.12 (2026-10-29, over 24 hours before the lock was written), then `caniuse-lite` and `@types/estree`; none is inside the 24-hour window. Two `overrides` in `web/package.json` pin `node-releases` 2.0.59 (2026-10-15) and `electron-to-chromium` 1.5.456 (2026-10-14): they are data packages reached through `browserslist` (from `@babel/core`, under `eslint-plugin-react-hooks`), and on the install day their newest releases (`node-releases` 2.0.62, 2026-10-30, and `electron-to-chromium` 1.5.468, 2026-10-30) were under 24 hours old, which would have resolved a version inside the window. The pins hold earlier versions that passed the OSV check; they are older than the newest release outside the window (`node-releases` 2.0.61 of 2026-10-29, `electron-to-chromium` 1.5.467 of 2026-10-27), so they can move up to those or to later releases once a day old and OSV-clean. Three direct pins are not the newest: `next` 16.4.2, because 16.4.3 (2026-10-23) was under the 14-day preference and 16.4.0 carries three OSV advisories that 16.4.2 and 16.4.3 do not (T176 re-runs the CSP check on whatever version is pinned); `typescript` 6.0.3, because `typescript-eslint` 8.71.2 declares the peer range `>=4.8.4 <6.1.0`; `eslint` 10.12.0, held below 10.14.0 (published the day of the install), where 10.13.0 (2026-10-16) was not tried and could replace it. The npm CLI here is 11.6.2, which has no `min-release-age` (npm 11.10 and later do), so the cooldown was enforced by hand with the script and the two overrides; a CI job that installs with a newer npm should set `min-release-age` and keep the lock file as the record of what ran.
- **Verified 2026-10-31 (the admin UI rebuilt on the owner's usual stack, [ADR 0016](../../docs/decisions/0016-build-the-admin-ui-with-tailwind-shadcn-and-tests-outside-src.md)).** Added exact pins: `radix-ui` 1.6.7, `lucide-react` 1.48.0, `class-variance-authority` 0.7.1, `cn` 0.4.0, `recharts` 3.8.0; dev `tailwindcss` and `@tailwindcss/postcss` 4.3.3, `tw-animate-css` 1.4.0, `vitest` 5.0.5, `vite` 8.3.5 (the peer of vitest), `jsdom` 29.1.1, `@testing-library/react` 16.3.3, `@testing-library/dom` 10.4.2, `@testing-library/jest-dom` 7.0.1, `@testing-library/user-event` 14.6.7, `@playwright/test` 1.63.0 (2026-09-04). Method: registry documents, OSV `querybatch` (`MAL-` included) and tarballs fetched with `npm pack --ignore-scripts`; nothing was installed to read it, and every install used the committed lock with `--ignore-scripts`. Four pins differ from the owner's other project: `vitest` 5.0.5 not 5.0.2 (`@vitest/mocker` 5.0.2 carries two moderate advisories, fixed in 5.0.3), `jsdom` 29.1.1 not 30.1.1 (30.x needs Node 22.22.2 or newer and this UI's floor is 22.18), `@testing-library/jest-dom` 7.0.1 (6.10.0 is deprecated by its authors), and `vite` 8.3.5 (8.3.1 has six advisories, fixed in 8.3.3). `cn` 0.4.0 is the owner's choice and was kept: a real package (repository `shadcn-ui/cn`, MIT, published with provenance, no install script), but the name was reused in 2026-09 after sitting idle since 2013 and the package is 0.x, so the vetting advised `clsx` plus `tailwind-merge`; a grep of its `dist/` and `bin/` found no `eval(`, `new Function` or `Function(`. The Recharts closure added 150 lock entries with no OSV advisory, no `MAL-` advisory and no install script (the only one in the lock is the macOS-only optional `fsevents`); `es-toolkit` is overridden to 1.53.0 because 1.53.1 was 11 hours old at install; the youngest remaining packages are `react-redux` 9.3.1 (77 hours) and `victory-vendor` 37.4.1 (103 hours). `@playwright/test` added 3 packages with no install script and no OSV result; no browser is downloaded (the Chromium build the package wants, revision 1243, was already on the machine), and that browser binary is not vetted. Rejected because of the CSP, from reading their source: `sonner` (inserts a `<style>` element at import), Radix ScrollArea (an inline `<style>`), the Radix modal primitives (`react-remove-scroll` inserts a `<style>`), `motion` (prerendered `style` attributes) and the default entry of `react-toastify`; `echarts` is not needed. The browser behaviour of the new UI under the CSP is checked by the Playwright smoke test (T176), not by this reading.

## D3. Caps (FR-020)

- **Decision.** Settings with level of effect *all children*, applied to each app separately: `request_timeout_s` 120 (maximum 290), `max_request_body_mb` 100, `max_concurrent_requests` 64; over-limit answers `504`, `413`, `429`.
- **Rationale.** The platform ends a request at 300 s, so the edge answers first (290 s at most) instead of the user seeing the platform's page; 100 MB is the largest body measured through the proxy (spike 8); 64 concurrent requests per app keeps a stuck child from taking every connection of the shared edge.
- **Alternatives.** Per-app overrides (a column and a screen field for a need nobody has shown); a rate cap (not in FR-020). *Untested:* whether 64 is right for a Streamlit app with many WebSockets; SC-003 reports it.

## D4. SC-001 load profile

- **Decision.** 500 requests a second over 10 connections, paced, against a fast fixture app, in a control run and during each operation.
- **Rationale.** About 3 % of the CPU quota, so the operation under test, not saturation, explains any difference (spike 7 measured about 130 µs of CPU per request for a stand-in edge).
- **Alternatives.** Open-loop saturation (it would measure the quota, not disruption). *Untested:* the real edge with identity, routing, ACL and metrics in the path — measure it **first** among the integration scenarios.

## D5. Snapshot and wheel storage

- **Decision.** A release's snapshot is one archive named by its digest in the content store; wheels are stored one file each, named by sha256; a file over 500 MB is refused naming the file; no Unity Catalog volume fallback in v1.
- **Rationale.** One archive keeps an Update to about one API call and makes the digest check at start a single file; per-file storage would multiply calls by the number of files, and wheels need individual reuse across apps ([design 5.3](../../docs/product/v1-design.md#53-build-and-package-sources)).
- **Alternatives.** Per-file snapshots (many calls, no gain), a volume (unavailable on Free Edition, so untestable here). *Untested:* the workspace-file import cap beyond 500 MB.

## D6. A conflicting overlay

- **Decision.** Warn, never fail: `pip check` output is shown on the release.
- **Rationale.** Spike 18 overrode `opentelemetry-api` and broke two packages that pin it, yet the app answered; whether that matters depends on the app, and the health check is the real gate.
- **Alternatives.** Fail the build on any `pip check` error (would refuse apps that work), allow-list per package (maintenance with no demand).

## D7. appmux's own wheels

- **Decision.** Vendored `requirements.txt` with `--no-index --find-links ./wheels`; the fallback bootstrap (installing into `/tmp` at start) is documented in [design 13.1](../../docs/product/v1-design.md#131-what-the-operator-sets-up), not built.
- **Rationale.** Spike 13 showed the platform honours both lines; the fallback is only needed if a platform change breaks that.
- **Alternatives.** Build the fallback now (code nobody runs).

## D8. Node children

- **Decision.** Node apps run in v1; an offline npm source (lock file plus cache, spike 14) is **not** shipped, so a Node app with dependencies needs `index` mode (shipping `node_modules` inside the snapshot is possible but untested). It stays in [design 15](../../docs/product/v1-design.md#15-beyond-v1).
- **Rationale.** Spike 14 proved it works, but the Python wheel path covers the success criteria and the fixtures need one Node app, which can use `index` mode.
- **Alternatives.** Ship it now (new build path and tests for no listed success criterion).

## D9. Narrowing the shared-SP token (O3)

- **Decision.** Not in v1. The shared SP stays off until a platform admin enables `shared_sp_delegation_allowed` and approves a request ([design 8.4](../../docs/product/v1-design.md#84-shared-sp-what-it-reaches-and-what-can-be-done)).
- **Rationale.** The master switch and the review gate already bound the exposure, and a narrowing needs a per-app scope list nobody has asked for.
- **Alternatives.** Per-app scopes (extra UI and tests).

## D10. Attach-check warehouse

- **Decision.** Platform setting `attach_check_warehouse_id`, unset by default; attaching a type that needs a warehouse is refused naming the setting.
- **Rationale.** Secret, warehouse and function checks run as the attacher through the SQL API (spike 16) and need a warehouse the attacher can use; one setting beats guessing.
- **Alternatives.** Ask the attacher to pick (a form field that changes every time).

## D11. CPU isolation

- **Decision.** Pin every sandbox to one CPU at `nice 19` and refuse `sched_setaffinity` by seccomp; setting `child_cpu_pin` (default on), proved by a canary in `self_check`.
- **Rationale.** Without it a CPU hog stalls everyone about 50 ms; with it a victim's p95 was 1.35 ms (spike 7). cgroups are not writable ([TD-003](../../docs/tech-debt.md#td-003-cpu-and-memory-limits-are-best-effort)).
- **Alternatives.** Nothing (the measured stall), a CPU watchdog that stops hogs (reactive, needs thresholds). *Untested:* fairness among children that share the pinned CPU, and a pin without `nice 19`.

## D12. Process and run-directory limits

- **Decision.** The launcher is a subreaper; `RLIMIT_NPROC` is set per sandbox (`max_processes_per_child`, 256); a build has a wall-clock limit (`build_timeout_s`, 600). The run directory is a size-limited tmpfs, with a size watchdog as fallback.
- **Rationale.** PID 1 does not reap (spike 7); `RLIMIT_NPROC` is counted per sandbox user namespace; a denied build otherwise takes 128 s to fail and more than 300 s when names resolve but connections drop; a tmpfs gave `ENOSPC` at its cap.
- **Alternatives.** A pre-bound socket descriptor so the run directory need not be writable. *Untested* when the plan was written; built afterwards as the way the relay's socket reaches the sandbox, and the run directory's cap was measured in a container on 2026-11-05 ([TD-004](../../docs/tech-debt.md#td-004-a-childs-run-directory-size-cap-is-unproven)).

## D13. Pump behaviour when idle

- **Decision.** The pump wakes on `SIGHUP` to reload its rules and does not poll.
- **Rationale.** A polling timer costs a wake-up per child at 100 children for nothing; a signal is enough for a rule edit.
- **Alternatives.** Poll the registry (rejected, wake-ups at scale).

## D14. Retention and CI

- **Decision.** The 7-day metrics retention is a unit-tested job. CI is deferred except for the release workflow (T184, on a `v*` tag): the repository has been under git since 2026-10-30 with a remote but no commit, so day to day the checks of [quickstart.md](quickstart.md) run by hand.
- **Rationale.** A scheduled test of retention would need weeks; CI needs a host that does not exist yet.
- **Alternatives.** A CI file written now (cannot be run, so cannot be verified).

## D15. Order of proof

- **Decision.** Build the pure logic first (`authz`, `routing`, `rewrite`, `appyaml`, the rule engine), then the sandbox and `self_check` on the container, then the edge and the load run, then the reconciler end to end, then the registry and the admin API, last the UI.
- **Rationale.** The risks that could change the design are on the container (sandbox, pump, real-edge capacity); cheap local logic gives a safe base, and a failing `self_check` blocks everything else.
- **Alternatives.** UI first (visible progress, none of the risk retired).

## D16. Smaller items the design left to the plan

- **Decision.** (a) The sandbox keeps `unshare --map-root-user` as measured; `--map-current-user` was never tried. (b) Child variables the platform sets: `LC_CTYPE=C.UTF-8` and `PYTHONUNBUFFERED=1` as the platform does, `HOME` the child's private writable temp directory, `UV_PYTHON_INSTALL_DIR` not set, `DATABRICKS_APP_URL` the parent URL plus `/<slug>`. (c) The per-app `Content-Security-Policy: sandbox` is not adopted. (d) A request to a child the actor may not use answers `404`, the same as an unknown slug ([contracts/edge-http.md](contracts/edge-http.md)).
- **Rationale.** (a) the measured sequence works and `self_check` proves it; (b) each follows the standalone behaviour where the sandbox allows it; (c) it is optional hardening, untested, and the same-origin limit is already accepted in v1 ([design 7.3](../../docs/product/v1-design.md#73-same-origin-limit-accepted-in-v1)); (d) the launcher lists only apps the actor can open, so a `403` would only let a visitor enumerate slugs.
- **Alternatives.** (a) `--map-current-user` (untested), (c) trying the sandbox CSP now (a second hardening layer with no success criterion), (d) `403` with a status page (friendlier, leaks which slugs exist). The owner can reverse (d) with a change to the contract and one test.

## D17. Open the port before the slow steps

- **Decision.** uvicorn starts with a small startup gate (`edge/gate.py`) and the registry connect, `self_check()` and the reconciler run in a background task. While starting, `GET`/`HEAD` on `/` answer `200` "starting" and every other path `503`; the gate opens to the real application only after `self_check()` passes, and stays shut (`503` everywhere, `/` included, naming the reason) when the registry is unreachable or a check fails. Settings that fail to validate stop the process before any bind.
- **Rationale.** The platform probes `/`; a sibling project measured a container restart when nothing answered during its start (company workspace, not measured on Free Edition). The old order left no listener while the registry connect and the canary children ran. The gate keeps the design fail-closed: the only thing it answers early is a static page without identity, registry or environment data.
- **Alternatives.** Bind after `self_check()` (simplest, but keeps the restart risk for as long as the canaries take), answer `200` on `/` even when blocked (stops a possible restart loop but turns a failed check into a green probe).
- **Untested.** Whether the platform restarts an app whose `/` answers `503` after a failed `self_check()`; if it does, the blocked page on `/` moves to `200` (the page still opens no route) and T048 changes with it.
