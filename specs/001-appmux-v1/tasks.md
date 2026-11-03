# Tasks: appmux v1

**Input**: Design documents from `specs/001-appmux-v1/`: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Prerequisites**: plan.md and spec.md (both exist); the owner asked for this file on 2026-10-30

**Status**: not started. No application code exists. Run `/speckit-implement` only when the owner asks.

**Tests**: included. [Design §14](../../docs/product/v1-design.md#14-testing) names the tests, [data model §7](../../docs/product/v1-data-model.md#7-rules-enforced-in-code) requires one test per rule, and the Python rules ask for pure logic test-first. In each story phase the tests come first and must fail for the right reason before the code is written. A test that needs a Databricks container is marked `integration` and runs through `scripts/sync-deploy.sh`; every container number is Free Edition only ([TD-005](../../docs/tech-debt.md#td-005-platform-measurements-come-from-free-edition-only)).

**Organization**: tasks are grouped by user story. The story phases are **ordered by dependency and by [research D15](research.md#d15-order-of-proof), not by priority number**: pure logic first (US4), then the sandbox (US3), then the edge and the host-a-child path (US1), then the rest. The `[USn]` labels are the spec's own. Stories are independent in what they prove, but not in what they build on: FR-011 makes the sandbox the only way to start a child, so US1 and everything after it build on US3.

## Format: `- [ ] T### [P?] [USn?] Description with file path`

- **[P]**: can run in parallel with other `[P]` tasks of the same phase (different files, no unfinished dependency)
- **[USn]**: the user story of [spec.md](spec.md); present only in story phases
- Every path is relative to the repository root. `src/appmux/<module>/` is a module of [plan.md](plan.md#source-code-repository-root)
- A module folder is created by the first task that puts a file in it (no empty scaffolding)
- A prototype under `specs/001-appmux-v1/spikes/scripts/` is a starting point to port and test, never a file to import

## Rules every task follows

- Python rules: modern typing, Pydantic at the boundary, narrow exceptions, no suppression comment for a tool the project does not run. Run `uv run ruff format .`, `uv run ruff check .`, `uv run mypy src` and the task's tests before marking a task done.
- A secret value never reaches a log, an audit row, a release row or a build log (data model rule 15). Mask workspace identifiers and emails in anything written to the repository.
- Docs ship with code ([constitution IV](../../.specify/memory/constitution.md)): each story phase ends with a task that updates [ARCHITECTURE.md](../../ARCHITECTURE.md) and any reference page its code invalidates. Do not mark behaviour implemented before its tests pass.
- Do not change a child's `app.yaml` or source, and do not add a route, file name or variable that a standard Databricks App would not have (constitution I).

---

## Phase 1: Setup (shared infrastructure)

**Purpose**: a package that installs, formats, lints, type-checks and runs an empty test suite.

- [X] T001 Create `pyproject.toml`: project `appmux`, Apache-2.0, `requires-python = "==3.11.*"`, `src` layout, runtime dependencies pinned to the App environment's versions from `specs/001-appmux-v1/spikes/evidence/spike-18-environment-packages.txt` (`uvicorn==0.30.6`, `starlette==0.38.6`, `httpx==0.28.1`, `websockets==12.0`, `pydantic==2.13.4`, `pyyaml==6.0.3`, `packaging==24.2`, `databricks-sdk==0.33.0`) plus `psycopg[binary]` (the version is fixed by the vetting task), a dev group with `ruff`, `mypy`, `pytest`, `types-PyYAML`. Async tests use the anyio pytest plugin that Starlette already brings (anyio 4.13.0 is in the environment); do not add `pytest-asyncio`.
- [X] T002 Configure the tools in `pyproject.toml`: `[tool.ruff]` (format and lint rules from `.agents/rules/python/coding-style.md`), `[tool.mypy]` strict for `src`, `[tool.pytest.ini_options]` with `testpaths = ["tests"]`, `anyio_backend`, and the marker `integration` ("needs a Databricks container; skipped elsewhere").
- [X] T003 Vet every new third-party package before it enters the lock (`psycopg`, `psycopg-binary` and the dev tools): confirm each exact name, check the version on OSV.dev (`MAL-` advisories included), confirm it was published more than 24 hours ago, read its install scripts. Replace the "Verification still owed" sentence of D1 in `specs/001-appmux-v1/research.md` with the versions chosen and the date checked.
- [X] T004 Run `uv lock` and `uv sync`; commit-ready `uv.lock` at the repository root. Check `uv run python -c "import appmux"` after creating `src/appmux/__init__.py` (only `__version__`).
- [X] T005 [P] Add the missing entries to `.gitignore`: `build/`, `web/out/`, `web/node_modules/`, `.venv/`, `__pycache__/`.
- [X] T006 [P] Write `tests/unit/test_module_boundaries.py` (standard-library `ast`): parse every file under `src/appmux/` and fail on an import that points against [design §3.4](../../docs/product/v1-design.md#34-module-boundaries). Encode the "Must not" column as a forbidden-import table per module (`authz` and `routing` import no I/O library and no `registry`/`upstream`; `rewrite` imports neither `routing` nor `authz`; `upstream` imports no `registry`; `sandbox` imports no `reconciler`, `registry`, `packages` or `appyaml`; `registry` and `admin` do not import `edge`; only `__main__` imports `edge`). It passes on the tree as it is and is the guard for every later task.
- [X] T007 [P] Write `tests/unit/test_licence_and_binaries.py` (FR-054): `LICENSE` holds the Apache-2.0 text; no file named `slirp4netns*` and no archive of its source exists anywhere in the tree except under the ignored `build/` folder.
- [X] T008 Replace the "Build / test / lint: not defined yet" line of `AGENTS.md` (Commands) with the four commands that now run (`uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src`, `uv run pytest tests/unit tests/contract`) and the marker rule for `integration`.

**Checkpoint**: the four commands listed in `AGENTS.md` pass on the empty package.

---

## Phase 2: Foundational (blocking prerequisites)

**Purpose**: settings, the registry, the actor, the admin API skeleton, the entry point and the deploy tooling. No story can start before this phase is done.

**⚠️ CRITICAL**: no user story work begins until this phase is complete.

- [X] T009 [P] Write `tests/unit/test_settings.py` first: a missing required variable stops the start with a message naming it; no default looks like a secret; `APPMUX_BOOTSTRAP_ADMINS` becomes a list; the `platform_settings` schema accepts exactly the keys of [data model §4.1](../../docs/product/v1-data-model.md#41-platform_settings) with the defaults of the "Defaults fixed by this plan" paragraph of `plan.md`, rejects an unknown key (rule 14), rejects `max_locked_releases` above `max_releases_per_app - 4` (rule 10), and each key carries its level of effect.
- [X] T010 Implement `src/appmux/settings.py`: `EnvironmentSettings` as a Pydantic `BaseModel` validated from `os.environ` (the variables the platform sets, `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`, `DATABRICKS_APP_PORT`, `PGHOST`, plus `APPMUX_LAKEBASE_DATABASE` and `APPMUX_BOOTSTRAP_ADMINS` of [design §13.1](../../docs/product/v1-design.md#131-what-the-operator-sets-up)); `PlatformSettings` model for the registry keys. `pydantic-settings` is not used ([ADR 0014](../../docs/decisions/0014-build-the-edge-on-the-environments-http-stack.md)).
- [X] T011 [P] Write `migrations/0001_baseline.sql`: the DDL of the nine tables from [docs/product/v1-data-model.md](../../docs/product/v1-data-model.md) §4 (including the partial unique index on pending requests), starting from `specs/001-appmux-v1/spikes/scripts/spike-09/extract_ddl.py`. No table beyond the nine.
- [X] T012 [P] Write `migrations/0002_grants.sql`: the table-level grants of [data model §6](../../docs/product/v1-data-model.md#6-who-writes-what) to `appmux_platform_writers` and to the appmux SP's role (the roles are created by the operator, [design §13.1](../../docs/product/v1-design.md#131-what-the-operator-sets-up)).
- [X] T013 Write `tests/unit/test_baseline_migration.py` first: parse the `CREATE TABLE` statements of `migrations/0001_baseline.sql` and assert that every table and column named in data model §4 exists, and that the field names of each row model in `src/appmux/registry/models.py` equal the column names of its table.
- [X] T014 Implement `src/appmux/registry/models.py`: one frozen Pydantic row model per table (`platform_settings`, `apps`, `permission_requests`, `app_config`, `app_grants`, `releases`, `app_resources`, `metrics`, `audit_log`); JSON columns typed as models, not `dict`.
- [X] T015 [P] Write `tests/unit/test_write_identity.py` first: every write is routed to the identity of data model §6 (appmux SP or the platform admin's OBO role `appmux_platform_writers`); a platform-table write attempted with the SP identity, and an SP-table write with an admin identity, raise.
- [X] T016 Implement `src/appmux/registry/writers.py`: the write-identity rule as a table from write kind to identity.
- [X] T017 Implement `src/appmux/registry/client.py`: `psycopg` 3 async; one long-lived connection for the appmux SP that reopens when its OAuth token is minted anew; `as_platform_admin(token)` opens one connection with that admin's token, runs one transaction and closes it ([ADR 0014](../../docs/decisions/0014-build-the-edge-on-the-environments-http-stack.md)). Map failures to `RegistryUnavailable`, `RegistryRoleMissing` (message `your database role cannot write platform data; ask the Lakebase project owner`) and `ChangedByOthers` (message `changed by someone else, reload and retry`). Port the login from `specs/001-appmux-v1/spikes/scripts/spike-01/`.
- [X] T018 Implement `src/appmux/registry/protocol.py` (the `Registry` protocol the rest of the code depends on) and `tests/contract/stub_registry.py` (an in-memory implementation used by contract tests; it enforces `version` on `apps` and `app_config`).
- [X] T019 [P] Write `tests/unit/test_registry_snapshot.py` first, then implement `src/appmux/registry/snapshot.py`: load every table except `metrics` and `audit_log` (the audit log is read through `Registry.read_audit`) into an immutable in-memory snapshot, reload after each write and poll every few seconds ([design §4.3](../../docs/product/v1-design.md#43-change-propagation-and-failure)); when Lakebase is down keep serving the last snapshot and report the registry error; at startup with Lakebase down raise so the caller can show the status page (fail closed). Validate each row with its model from `registry/models.py`; a row that fails its model is quarantined (left out of the snapshot, counted and reported with the table and key, never its content) and never fails the whole table, because the database cannot check the shape of a `jsonb` value and a holder of the shared-SP token can write a malformed one. Test it with a malformed `releases.detected`.
- [X] T020 [P] Implement `src/appmux/registry/audit.py`: `AuditEntry` (actor email, action, app, detail before/after, `automatic` flag) and the writer; the detail model has no field that can hold a secret value (rule 15). Test in `tests/unit/test_audit_entry.py`.
- [X] T021 Implement `src/appmux/registry/migrations.py`: apply `migrations/*.sql` in file-name order in one transaction and refuse when `apps` already exists. It invents no migration table (v1 has the baseline only); a later migration needs its own decision.
- [X] T022 [P] Write `tests/unit/test_actor.py` first, then `src/appmux/identity/actor.py`: forwarded headers become an `Actor` (email, user, preferred username, optional visitor token, client address from `X-Real-Ip`, which the proxy overwrites; `X-Forwarded-For` is not used because the proxy appends hops after the client); no `X-Forwarded-Email` raises `MissingIdentity` and so does a repeated identity header (the edge answers `401`).
- [X] T023 Write `tests/contract/test_admin_conventions.py` first: error body shape `{ "error": { "code", "message" } }` with no stack trace, SQL message or path; `401` with no identity; `403` for a mutating call without `Sec-Fetch-Site: same-origin`; `409` with `changed by someone else, reload and retry` on a stale `version`; `503` when the registry is down and the route needs it ([contracts/admin-api.md](contracts/admin-api.md#conventions)).
- [X] T024 Implement `src/appmux/admin/errors.py` (exception types and the one error response) and `src/appmux/admin/api.py` (Starlette sub-application factory mounted at `/_appmux/api/v1`: identity from headers, an injected `Authorizer` callable, the same-origin check on mutating methods, audit on every mutating route).
- [X] T025 Write `tests/contract/test_startup_gate.py` first, then implement `src/appmux/edge/gate.py`, `src/appmux/edge/app.py` and `src/appmux/__main__.py` ([design §5.6](../../docs/product/v1-design.md#56-crash-restart-and-shutdown), FR-008). The gate is the ASGI application uvicorn is started with, so the port is bound before anything slow runs. Phase *starting*: `GET` and `HEAD` on `/` answer `200` with a static "appmux is starting" page (the platform probes `/`), every other path, `/_appmux/` included, answers `503` with a static page. Phase *blocked* (registry unreachable here, a failed `self_check` in T067): every path, `/` included, answers `503` with a page naming the reason. Phase *open*: every request goes to the top-level app in `edge/app.py` (`/_appmux/` to the admin sub-application, everything else `404` until the pipeline exists). The startup and blocked pages carry no identity, registry or environment data, and `gate.py` imports only the standard library so nothing slow loads before the bind. `__main__.py`: validate settings with `EnvironmentSettings.from_environment()` from `src/appmux/settings.py` (a `SettingsError` names every faulty variable and stops the start), start uvicorn on `DATABRICKS_APP_PORT` with the gate, and run the rest in a background task that connects to the registry and then opens the gate; `SIGTERM` cancels that task. `self_check` and the reconciler are plugged in by T067 and T095. The test covers the three phases, `HEAD`, and that nothing but `/` is answered before the gate opens.
- [X] T026 [P] Write `app.yaml` at the repository root (the `command` and `env` of [design §13.1](../../docs/product/v1-design.md#131-what-the-operator-sets-up) item 4). There is no `requirements.txt` in the repository: next to `pyproject.toml` and `uv.lock` it is a trap for any tool that deploys the repository root, so it is generated into `build/` by the next task ([spike 13](spikes/README.md)).
- [X] T027 [P] Write `scripts/vendor-wheels.sh`: read the `psycopg` and `psycopg-binary` versions pinned in `uv.lock`, download their wheels for Python 3.11 and `manylinux2014_x86_64` (`--only-binary=:all:`) into `build/wheels/`, only for the packages the App environment lacks (names in `specs/001-appmux-v1/spikes/evidence/spike-18-environment-packages.txt`), and write `build/requirements.txt`: `--no-index`, `--find-links ./wheels`, then each requirement with its `--hash`. Refuse a wheel published in the last 24 hours without `--allow-recent`. Neither output is committed.
- [X] T028 [P] Write `scripts/assemble-deploy.sh`, `scripts/sync-deploy.sh` and `scripts/fetch-logs.sh`. `assemble-deploy.sh` is the one place that builds the deployment folder `build/deploy/`, from an allow-list: `app.yaml` from the repository root, `requirements.txt` and `wheels/` from `build/`, a copy of `src/appmux` as `appmux/` without `__pycache__`, a copy of `web/out` as `web/out/`, `LICENSE` from the repository root, and `bin/slirp4netns` plus `licenses/slirp4netns/` from `build/slirp4netns/` (written by `fetch-slirp4netns.sh`, T029; [ADR 0015](../../docs/decisions/0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md)). It then checks the assembled tree and fails when the tree holds `pyproject.toml`, `uv.lock` or `package.json`, when `requirements.txt` does not start with `--no-index`, or when `bin/slirp4netns` is missing or not executable, or `licenses/slirp4netns/` lacks `COPYING`, `SOURCE.txt` or the source archive (the message names `fetch-slirp4netns.sh`). `--zip` also writes `build/appmux-<version>.zip` and its `.sha256`. `sync-deploy.sh` runs `assemble-deploy.sh`, syncs `build/deploy/` to the workspace folder named in a local, uncommitted environment variable, deploys the App and waits for the deployment state; `fetch-logs.sh` fetches the logs. Start from `specs/001-appmux-v1/spikes/scripts/deploy.sh`; never print a token or a workspace identifier. Tested in `tests/unit/test_assemble_deploy.py` with a stub CLI; the scripts have not yet run against a real workspace, which the first real deploy does.
- [X] T029 [P] Write `scripts/fetch-slirp4netns.sh` ([ADR 0015](../../docs/decisions/0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md), accepted 2026-11-01, route 1). The pinned release (spike 9 ran `v1.3.6`), the URLs of the upstream static binary and of the source archive, and the SHA-256 of each are named constants at the top of the script. It downloads both into `build/slirp4netns/`, deletes the download and fails on a mismatch, then writes `build/slirp4netns/bin/slirp4netns` (executable, unmodified) and `build/slirp4netns/licenses/slirp4netns/` holding `COPYING` (taken from the archive), `SOURCE.txt` (release, URLs, both SHA-256 values, "unmodified upstream build", and the statement that the source is the archive beside it) and the archive itself. The script also pins and fetches the `libslirp` v4.9.5 source archive and writes `LICENSE-parson` (from the `slirp4netns` archive), `LICENSE-libslirp` and `COPYRIGHT-libslirp` beside `SOURCE.txt`, which names four URLs and four SHA-256 values. The binary embeds `slirp4netns`, `parson`, `libslirp`, glibc, glib and libseccomp (libcap is not linked). The script does not fetch glibc, glib or libseccomp (`LGPL`; their corresponding source is the Ubuntu jammy packages). It pins and fetches the LGPL-2.1 text (written as `LICENSE-LGPL-2.1`) and `SOURCE.txt` carries a written offer, valid for three years, for that source and for what a recipient needs to relink (LGPL-2.1 section 6c; route 1 of [ADR 0015](../../docs/decisions/0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md), chosen by the owner on 2026-11-01); `assemble-deploy.sh` refuses a folder without the LGPL text. The contact the offer names (`SOURCE_OFFER_CONTACT_URL`) must be reachable by a recipient before the first release tag (ADR 0015, Must now be true). `build/` is git-ignored, so the repository holds no binary or source (FR-054).
- [X] T030 Write `tests/conftest.py`: skip every `integration` test unless `APPMUX_TEST_CONTAINER=1`, and provide the `anyio_backend` fixture (`asyncio`) shared by unit and contract tests.
- [X] T186 [P] Write `tests/contract/test_admin_schema_examples.py` first, then `src/appmux/admin/schemas.py`: one Pydantic model per request and response schema in the Schemas section of [contracts/admin-api.md](contracts/admin-api.md) (requests `extra="forbid"`). The test loads `specs/001-appmux-v1/contracts/examples/manifest.json`, validates every example against the model it names, and fails when a schema has no example, no model or no manifest entry, and when a mutated example (a required field removed, an unknown field added) is still accepted. The UI half of the same check is T171; later route tasks import these models instead of defining their own. Beyond the shape, the backend carries the same negative rules the UI encodes, each with a test that fails when the rule is dropped: grants (`everyone` is never `CAN_MANAGE`), egress overlap, attach `access_as`, the `own_sp` key pair and the admins list. The routes follow the contract's collection envelope (`items`), the up-to-date response, `DELETE` of a delegation with its reason as a query parameter (never a body), the `version` in every write response, the truncation flag on metrics and the single-call platform view.

**Checkpoint**: `uv run pytest tests/unit tests/contract` passes; `scripts/sync-deploy.sh` deploys an appmux that answers `404` on `/` and `401` without an identity header.

---

## Phase 3: User Story 4 - Give access through Databricks users and groups (Priority: P1) 🎯

**Goal**: every decision goes through one pure `authorize(actor, action, app)` that defaults to deny; groups come from the person's own token; sharing works through the API.

**Independent Test**: run the table of [design §9.2](../../docs/product/v1-design.md#92-who-may-do-what) against `authorize()` with a stubbed Databricks lookup; `GET /apps` for Bob lists only what he may open.

### Tests for User Story 4 (write first, watch them fail)

- [X] T031 [P] [US4] Write `tests/unit/test_authorize_table.py`: one row per action of design §9.2 for each actor kind (no grant, `CAN_USE`, `CAN_MANAGE`, owner, `everyone`, group member, platform admin), default deny, failed group lookup fails closed, user authorization off (only `user` and `everyone` grants match), the owner cannot be lowered (rule 1), the last-`user`-entry guard on `platform_admin.remove` (rule 2), implicit platform-admin `CAN_MANAGE` with no `app_grants` row (rule 17).
- [X] T032 [P] [US4] Write `tests/unit/test_groups_lookup.py`: a stubbed SCIM `GET /Me` called with the person's own token; nested groups count; a 300 s cache; a cached value used for at most one hour after a failure; a failed lookup with nothing cached returns "unknown" (spike 12).
- [X] T033 [P] [US4] Write `tests/unit/test_rewrite_request.py`: client-supplied `X-Forwarded-Email`, `X-Forwarded-User`, `X-Forwarded-Preferred-Username`, `X-Real-Ip`, `X-Request-Id` are replaced by the proxy's values; `X-Forwarded-Prefix` is `/<slug>`; `X-Forwarded-Access-Token` is removed unless the app is OBO-delegated and the visitor is its owner or named by a grant ([contracts/edge-http.md](contracts/edge-http.md#request-headers-a-child-receives)).
- [X] T034 [P] [US4] Write `tests/unit/test_connections.py`: an `OpenConnections` registry closes the handles of an actor whose access to an app no longer authorizes, on the next refresh, and leaves other handles open (FR-027).
- [X] T035 [P] [US4] Write `tests/contract/test_me_apps_grants.py`: `GET /me`, `GET /apps` (only apps the actor can open; all for a platform admin), `PUT /apps/{app}/grants`, `DELETE /apps/{app}/grants/{type}/{name}` (the owner cannot be removed; `everyone` is `CAN_USE` only), a removed grant denies on the next snapshot.

### Implementation for User Story 4

- [X] T036 [US4] Implement `src/appmux/authz/actions.py`: the action catalogue (`app.register`, `app.edit_platform_fields`, `app.rename`, `app.set_owner`, `app.delete`, `app.start`, `app.stop`, `app.restart`, `app.update`, `app.view_as_user`, `release.*`, `config.edit`, `resource.*`, `grant.app`, `request.*`, `delegation.*`, `settings.edit`, `platform_admin.*`, `egress.edit`) with the minimum level each needs, from design §9.2.
- [X] T037 [US4] Implement `src/appmux/authz/policy.py`: pure `authorize(actor, action, app, data) -> Decision` over an `AuthzData` value built from the registry snapshot; the default is deny; no I/O import ([FR-022 to FR-026](spec.md#functional-requirements)).
- [X] T038 [US4] Implement `src/appmux/identity/groups.py`: the SCIM `GET /Me` lookup with the person's token through an injected `httpx.AsyncClient`, the 300 s cache and the one-hour stale window. Port from `specs/001-appmux-v1/spikes/scripts/spike-12/poll_me.py`.
- [X] T039 [US4] Implement `src/appmux/rewrite/request.py`: request header rewriting per the table in `contracts/edge-http.md`; the token rule takes a decision value from `authz`, it does not decide.
- [X] T040 [US4] Implement `src/appmux/edge/connections.py`: `OpenConnections` (register, unregister, `close_unauthorized(authorizer)`), called after each group-cache or snapshot refresh.
- [X] T041 [US4] Implement `src/appmux/registry/grants.py` and `src/appmux/admin/identity.py` (`GET /me`, `GET /apps`) and `src/appmux/admin/grants.py` (grant routes, `grant.app`, audit row; the grant routes carry no `version`, per [contracts/admin-api.md](contracts/admin-api.md)).
- [X] T042 [US4] Wire `authorize` into `src/appmux/admin/api.py` as the `Authorizer`; wire the platform-admin bootstrap (`APPMUX_BOOTSTRAP_ADMINS` honoured only while `platform_admins` is unset, FR-026) in `src/appmux/authz/data.py`, with its test in `tests/unit/test_bootstrap_admins.py` written first.
- [X] T043 [US4] Update `ARCHITECTURE.md`: mark `identity`, `authz`, the request side of `rewrite` and `registry` as implemented, each with the name of the test that backs it.

**Checkpoint**: the `authorize` table passes; `GET /apps` is correct for three stubbed actors; US4 stands alone.

---

## Phase 4: User Story 3 - Treat every child as potentially malicious (Priority: P1)

**Goal**: each child runs in its own sandbox; `self_check()` proves the sandbox with a canary and, when any check fails, no route opens and a status page names the check.

**Independent Test**: run the hostile fixtures on a healthy sandbox, then weaken the sandbox on purpose three ways and start appmux again (SC-002).

### Tests for User Story 3 (write first)

- [X] T044 [P] [US3] Write `tests/unit/test_egress_rules.py`: ordered, first match wins, no match allows; evaluation order is the app's own list, then the global list, then allow; `10.0.2.0/24` is always allowed; CIDR and port rules; IPv6 frames and IP fragments dropped when port rules exist; per-rule drop counters; the editor refuses an `allow` that overlaps `@container_ip`, `@appmux_port` or `169.254.0.0/16` (rule 18), and `@container_ip`/`@appmux_port` resolve at start ([design §6.3](../../docs/product/v1-design.md#63-egress-rules)).
- [X] T045 [P] [US3] Write `tests/unit/test_self_check_model.py`: the result lists one entry per check; any failed check makes the whole result failed; a canary that cannot run, times out or raises counts as failed (fail closed, FR-013); the result carries the check names for the status page.
- [X] T046 [P] [US3] Write `tests/unit/test_child_environment.py`: a child gets only the values of `app.yaml`, then the release `env`, then appmux's variables, which cannot be overridden; no variable of appmux's own environment reaches it; `LC_CTYPE=C.UTF-8`, `PYTHONUNBUFFERED=1`, `HOME` a private temp directory, `DATABRICKS_APP_URL` the parent URL plus `/<slug>` ([research D16](research.md#d16-smaller-items-the-design-left-to-the-plan)).
- [X] T047 [P] [US3] Write `tests/unit/test_sandbox_spec.py`: the `unshare` command line for user, mount, net, pid and ipc namespaces with `--map-root-user --fork --kill-child`; the mount allow-list; `omit_layers` exists only as a constructor argument and is never read from the environment or the registry.
- [X] T048 [P] [US3] Write the contract test `tests/contract/test_self_check_gate.py`: with a failed `self_check` result the edge is *blocked* (2026-10-31: by `SelfCheckGate` in `edge/app.py`, not by the startup gate of T025, whose blocked phase never reopens and would 503 `/_appmux/` too, so the re-run below could not be reached) and every route except `/_appmux/`, `/` included, answers `503` with a status page that names the failed check, and `POST /_appmux/api/v1/platform/self-check` re-runs it and returns the per-check result (requires `CAN_MANAGE` on the platform).
- [X] T049 [P] [US3] Write the contract test `tests/contract/test_egress_api.py` (FR-014): `PUT /platform/egress` and `PUT /apps/{app}/egress` accept an ordered rule list from a platform admin only (an app admin gets `403`), each item is parsed with `EgressRule.model_validate` (a `ValidationError` comes back as `400`) and the list then goes through `refuse_overlapping_allows` from `src/appmux/sandbox/rules.py`; either refusal names the offending rule or the overlapping placeholder, and a saved list is signalled to the child's pump (`SIGHUP`).

### Fixtures for User Story 3

- [X] T050 [P] [US3] Create the fixture `tests/fixtures/env_snooper/` (`app.py`, `app.yaml`): a small FastAPI app that tries to read `/proc/*/environ` of every pid and PID 1, `runtime.cfg` and the app's own environment, and reports each result as JSON at `/report`.
- [X] T051 [P] [US3] Create the fixture `tests/fixtures/pkg_env_writer/`: tries to create a file in the parent's `site-packages` and in its overlay directory; must report `EROFS`.
- [X] T052 [P] [US3] Create the fixture `tests/fixtures/network_scanner/`: tries appmux's loopback port, every port of the container IP, `169.254.169.254:80` and a denied destination; reports each connect result.
- [X] T053 [P] [US3] Create the fixture `tests/fixtures/fork_bomb/`: forks until refused; reports the count (must stop at `RLIMIT_NPROC`).
- [X] T054 [P] [US3] Create the fixture `tests/fixtures/light_fastapi/` (`app.py`, `app.yaml`, `requirements.txt`): a FastAPI app with `/` and `/healthz` that answers fast; the load fixture of SC-001 and the base of the ten well-behaved apps.

### Implementation for User Story 3

- [X] T055 [US3] Implement `src/appmux/sandbox/rules.py`: the egress rule engine and its editor refusals, pure, no I/O. Port `Ruleset` and `decide` from `specs/001-appmux-v1/spikes/scripts/spike-06/egress_pump.py`.
- [X] T056 [US3] Implement `src/appmux/sandbox/environment.py`: the child environment builder; the only place a secret value is placed in an environment (FR-015, [design §5.4](../../docs/product/v1-design.md#54-appyaml-requirements-and-start)).
- [X] T057 [US3] Implement `src/appmux/sandbox/rootfs.py`: the child's root built from an allow-list; tmpfs over `/proc`, `/tmp`, `/var/tmp`, `/dev/shm` and `/databricks/runtime-secrets`; the shared package environment and every overlay mounted read-only (FR-016). Port from `specs/001-appmux-v1/spikes/scripts/spike-02/sandbox_fs.py`. *Progress 2026-10-31: `sandbox/rootfs.py` is written and its logic is tested (`tests/unit/test_sandbox_rootfs.py`); it ran in a Free Edition container on 2026-10-31: a hostile child saw an empty `/proc`, could not open `runtime.cfg` and could not create a file in the package environment (`EROFS`), and `self_check` passed `platform_files`, `read_only_environment` and `appmux_directories` (`test_hostile_fixtures.py`, `test_self_check_container.py`).*
- [X] T058 [US3] Implement `src/appmux/sandbox/seccomp.py`: a filter that refuses `sched_setaffinity` (and nothing else the child needs), loadable for the process and its children. Port from `specs/001-appmux-v1/spikes/scripts/spike-07/load_ops.py`. *Progress 2026-10-31: `sandbox/seccomp.py` is written and its logic is tested (`tests/unit/test_seccomp.py`); it ran in a Free Edition container on 2026-10-31: with the pin on, `self_check` passed `cpu_pin`, whose canary asks to widen its CPU affinity and must get `EPERM` (`test_self_check_container.py`).*
- [X] T059 [US3] Implement `src/appmux/sandbox/launcher.py` (stage 1): the `unshare` sequence, full capability drop, `no_new_privs`, `RLIMIT_NPROC` from `max_processes_per_child`, `RLIMIT_FSIZE`, CPU pin at `nice 19` when `child_cpu_pin` is on, launcher as a subreaper (`prctl` `PR_SET_CHILD_SUBREAPER`), kill of the sandbox's init on stop, a wall-clock limit for builds. The launcher also sets the child's `PATH` and `PYTHONPATH`, which `build_child_environment` leaves to it, and passes `/home/app` (the private `tmpfs` of design 6.1 step 5, which §5 calls the child's private writable temp directory) as `ChildIdentity.home_directory`. `SandboxSpec.omit_layers` lets the integration test weaken a layer; nothing else reads it. *Progress 2026-10-31: `sandbox/launcher.py` is written and its logic is tested (`tests/unit/test_sandbox_launcher.py`); it ran in a Free Edition container on 2026-10-31: children start, serve, crash and stop in their sandboxes (`test_host_a_child.py`, `test_crash_and_rollback.py`), a fork bomb stopped with `EAGAIN` after 247 forks at `RLIMIT_NPROC` 256, and `self_check` passed `capabilities`, `nested_user_namespace`, `pid_isolation`, `sysv_ipc` and `inherited_descriptors`. The wall-clock limit for builds has not run in a container.*
- [ ] T060 [US3] Implement `src/appmux/sandbox/run_dir.py`: the child's run directory as a size-capped tmpfs with a size watchdog as the fallback; document the unproven cap in the module docstring and keep [TD-004](../../docs/tech-debt.md#td-004-a-childs-run-directory-size-cap-is-unproven) open until measured. *Progress 2026-10-31: `sandbox/run_dir.py` is written and its logic is tested (`tests/unit/test_run_dir.py`); nothing run in a container on 2026-10-31 measured the run directory's size cap, so the task and TD-004 stay open.*
- [ ] T061 [US3] Implement `src/appmux/sandbox/pump.py`: veth pair created over rtnetlink, `AF_PACKET` frame pump applying `rules.py`, `slirp4netns --netns-type=tapfd --disable-host-loopback` on a `SOCK_SEQPACKET` pair, drop counters per rule with sample destinations, rule reload on `SIGHUP` (no polling). The decision, the counters and ARP pass-through already exist as `Ruleset.decide` and `DropCounters` in `src/appmux/sandbox/rules.py`; the pump calls them and does not re-implement them. It starts the binary found at `bin/slirp4netns` in the deployment folder (the parent of the `appmux/` package, not the working directory); a missing binary fails the `self_check` row that names it. Port from `specs/001-appmux-v1/spikes/scripts/spike-06/egress_stack.py` and `egress_pump.py`. *Progress 2026-10-31: `sandbox/pump.py` is written and its logic is tested (`tests/unit/test_sandbox_pump.py`); in a container on 2026-10-31 the pump forwarded a public destination (`1.1.1.1:443`, the default allow), dropped the metadata address, and dropped the destination again once the app's own deny rule was in force at the start of a new child (`test_hostile_fixtures.py`); the drop counters and the `SIGHUP` reload of a running pump have not run there, so the task stays open.*
- [X] T062 [US3] Implement `src/appmux/sandbox/relay.py`: the per-child unix-socket relay from appmux into the child's network namespace. Port from `specs/001-appmux-v1/spikes/scripts/spike-07/relay.py`. *Progress 2026-10-31: `sandbox/relay.py` is written and its logic is tested (`tests/unit/test_sandbox_relay.py`); it ran in a Free Edition container on 2026-10-31 (`test_host_a_child.py`, 10 of 10): the run found that the relay listened before the child's port was bound, which is fixed (it now listens once the port accepts).*
- [X] T063 [US3] Implement `src/appmux/sandbox/canary.py`: the canary program that runs inside a real sandbox and performs the 12 checks of [design §6.4](../../docs/product/v1-design.md#64-self-check-failures-and-gaps) (they cover the 18 canaries of spike 2) plus the CPU-pin check; it prints one JSON line per check. *Progress 2026-10-31: `sandbox/canary.py` is written and its logic is tested (`tests/unit/test_sandbox_canary.py`); it ran in a Free Edition container on 2026-10-31: all 13 checks passed in a real sandbox (`test_self_check_container.py`).*
- [X] T064 [US3] Implement `src/appmux/sandbox/self_check.py`: run the canary, build the result model of the unit test, run at startup and on demand, fail closed. (13 checks: the 12 of design 6.4 plus `cpu_pin`; the canary is reached through the `CanaryRunner` seam until T063 and the wiring of T067 land.)
- [X] T065 [US3] Implement `src/appmux/edge/status_pages.py` and the self-check gate in `src/appmux/edge/app.py`: with a failed result every route (except `/_appmux/`) answers `503` with a page naming the failed check; add `POST /platform/self-check` in `src/appmux/admin/platform.py`.
- [ ] T066 [US3] Add the egress routes: `PUT /platform/egress` in `src/appmux/admin/platform.py` and `PUT /apps/{app}/egress` in `src/appmux/admin/apps.py` (action `egress.edit`, audit row, rules validated by `sandbox/rules.py`), store the lists in `platform_settings` (`egress_global`) and `apps.egress` through `src/appmux/registry/apps.py`, and make `sandbox/pump.py` reload on `SIGHUP`. *Progress 2026-10-31: the routes, audit rows, registry storage and the `EgressReloader` seam are done and tested (`tests/contract/test_egress_api.py`); the reloader defaults to a no-op and the pump's `SIGHUP` reload is not wired, so this stays open.*
- [X] T067 [US3] Plug `self_check()` into the background startup task of `src/appmux/__main__.py`, after the registry connect and before the gate opens: a failed result makes `SelfCheckGate` (T048, T065) answer `503` with the failed check's name on every route except `/_appmux/`; a passed result lets requests through. The startup gate of T025 opens as before: its blocked phase never reopens, so it is not the mechanism (amended 2026-10-31). The port is already bound, so the check never delays the platform's probe of `/`. *Done 2026-10-31: the step runs at start-up and `POST /platform/self-check` re-runs it; `_canary_runner` now hands this container's facts (address, port, `sys.prefix`, default egress rules and CPU pin) to `sandbox/canary.run_canary`, and an unknown container address fails closed with `CanaryUnavailable` (`tests/contract/test_self_check_gate.py`). The plumbing is proven with `run_canary` replaced by a stand-in; that the canary runs inside a real sandbox is container-only and stays with T063.*

### Integration for User Story 3 (Databricks container, Free Edition)

- [X] T068 [US3] Write `tests/integration/test_self_check_container.py`: deploy with `scripts/sync-deploy.sh`; assert `self_check` passes every check of design §6.4 on the container; save the result to `specs/001-appmux-v1/spikes/evidence/` only if it differs from spike 2, 6 and 7. Passed 2 of 2 on 2026-10-31 against the Free Edition dev App, from the dev machine (`scripts/run-remote-tests.sh`): all 13 checks passed in 7.4 s. The result differs from spikes 2, 6 and 7, which ran probes rather than `self_check()` itself, so it is saved in `specs/001-appmux-v1/spikes/evidence/container-self-check-2026-10-31.json`.
- [X] T069 [US3] Write `tests/integration/test_hostile_fixtures.py`: register `env_snooper`, `pkg_env_writer`, `network_scanner` and `fork_bomb` through a test-only entry that calls the launcher directly (the registry path arrives with US1); each attempt fails as FR-011 and the acceptance scenarios 3 to 5 say; a neighbour `light_fastapi` keeps its p95. *Progress 2026-10-31: `tests/integration/test_hostile_fixtures.py` passed 6 of 6 on the Free Edition dev App from the dev machine, through the registry path (no entry of its own, as the owner decided): `env_snooper` isolated; `pkg_env_writer` got `EROFS` in site-packages and in a real overlay (the fixture now pins one wheel); `fork_bomb` stopped by `EAGAIN` after 247 forks at the limit of 256 with the app still running; `network_scanner` timed out on the metadata address; a destination reached by the control (`1.1.1.1:443`) timed out after the app's own deny rule and a new release; and a neighbour kept its p95 (236 ms idle, 235 ms beside the hostile children). Two scanner targets are not tried here and are covered by the canary of T068 (`loopback_and_container_ip`): `appmux_loopback` collides with the child's own port 8000, and `container_ip` is unknown to a caller outside the container.*
- [ ] T070 [US3] Write `tests/integration/test_self_check_fail_closed.py` (SC-002): build the sandbox with `omit_layers` set to the `/proc` overmount, the capability drop and the pump filter in turn; each makes `self_check` fail, opens no route and shows the status page naming the check.
- [ ] T071 [US3] Update `ARCHITECTURE.md` (mark `sandbox` implemented), `docs/reference/databricks-apps-platform.md` only where a run contradicts it, and [TD-003](../../docs/tech-debt.md#td-003-cpu-and-memory-limits-are-best-effort)/[TD-004](../../docs/tech-debt.md#td-004-a-childs-run-directory-size-cap-is-unproven) with what the run measured.

**Checkpoint**: SC-002 passes on the container; every hostile fixture is contained.

---

## Phase 5: User Story 1 - Host a child safely and open it only after it is healthy (Priority: P1) 🎯 MVP

**Goal**: register an app, press Update, and `/<slug>/` opens only after the new release was fetched, built, started and answered its health check.

**Independent Test**: register one fixture app, press Update, and request `/<slug>/` before and after the health check passes.

### Tests for User Story 1 (write first)

- [X] T072 [P] [US1] Write `tests/unit/test_routing.py`: prefix matching on the whole first segment (`/sales-old/x` never reaches `sales`), `/_appmux/` reserved and not a valid slug, normalisation (percent-decode once, dot segments, repeated slashes) and `400` for ambiguous input (encoded slash, backslash, NUL, path that changes after one more decode), no match is `404`, the upstream path and query string are kept (FR-017).
- [X] T073 [P] [US1] Write `tests/unit/test_rewrite_response.py`: `Location` beginning with `/` gets the prefix; `Set-Cookie` `Path` forced to `/<slug>/`, `Domain` dropped, other attributes kept; a child cookie named `__Host-appmux_open` is dropped; bodies untouched (FR-019).
- [X] T074 [P] [US1] Write `tests/unit/test_appyaml.py` for the `command` and `env` parser: `app.yaml` and `app.yml` together are refused; an `env` entry with both or neither of `value` and `valueFrom` is refused; unknown keys warn; `$NAME` is replaced in `command` arguments only; the manifest is stored (FR-033, [spike 17](spikes/README.md)).
- [X] T075 [P] [US1] Write `tests/unit/test_snapshot_digest.py`: a folder listing becomes one archive named by its sha256; the same content gives the same digest regardless of listing order; an empty listing raises `empty, or you cannot read it`; a file over 500 MB is refused naming the file (FR-003, [research D5](research.md#d5-snapshot-and-wheel-storage)).
- [X] T076 [P] [US1] Write `tests/unit/test_release_states.py`: the state table `pending → building → starting → healthy → active` plus `failed` and `superseded`; illegal transitions raise; `failed` is never retried automatically but can be activated by hand (FR-005); the route table setter is called only from the reconciler's swap (constitution II): a source-scan test fails if another module calls it.
- [X] T077 [P] [US1] Write `tests/unit/test_health_clock.py`: polling once a second from process start, any status below 500 is healthy, `health_deadline_s` bounds 5 to 600, the failure reason holds the last error (`health check did not pass within 60 s (last error: connection refused)`).
- [X] T078 [P] [US1] Write `tests/contract/test_edge_pipeline.py` against [contracts/edge-http.md](contracts/edge-http.md): the order `identity → routing → authz → rewrite → upstream → rewrite`, each status the edge itself produces (`400`, `401`, `404` for no slug and for no `CAN_USE` alike, `413`, `429` with `Retry-After: 1`, `502`, `503`, `504`), error bodies free of stack traces and paths, `X-Forwarded-Prefix`, streaming and WebSocket pass-through against a stub upstream.
- [X] T079 [P] [US1] Write `tests/contract/test_register_update.py`: `POST /apps` (platform admin only), `POST /apps/{app}/releases` with a stubbed workspace read, `GET /apps/{app}/releases/{id}` showing the progress and the reason of a failed release, "already up to date" creates no release, a read with no access fails with a message that says why, and nothing changes.

### Fixtures for User Story 1

- [X] T080 [P] [US1] Create the fixtures `tests/fixtures/startup_crash/`, `tests/fixtures/always_500/` and `tests/fixtures/slow_start/` (`app.py`, `app.yaml`): exit at start, answer `500` on the health path, answer only after a configurable delay.
- [X] T081 [P] [US1] Create the fixtures `tests/fixtures/absolute_links/` and `tests/fixtures/base_path_aware/`: the first hard-codes `/static/x.css` and must produce the `404` of design §7.2, not a misroute; the second honours `X-Forwarded-Prefix` and `APPMUX_BASE_PATH`. The fixtures report what they saw: `absolute_links` answers `/api/stats` with a `hits` counter per path (zero for `/static/x.css` when the stray request was refused), and `base_path_aware` answers with `base_path` and `source`. T078 and T096 assert those fields, not only the status codes.

### Implementation for User Story 1 - the edge

- [X] T082 [US1] Implement `src/appmux/routing/selector.py` (`RouteSelector` protocol and `PrefixSelector`) and `src/appmux/routing/normalise.py` (path normalisation and rejection), pure. Port from `specs/001-appmux-v1/spikes/scripts/spike-05/prefix_proxy.py`.
- [X] T083 [US1] Implement `src/appmux/rewrite/response.py`: `Location` and `Set-Cookie` rewriting.
- [X] T084 [US1] Implement `src/appmux/upstream/client.py` (httpx over a unix socket, streaming both ways), `src/appmux/upstream/websocket.py` (`websockets.unix_connect` relay that registers each socket with `OpenConnections`), and `src/appmux/upstream/caps.py` (per-app concurrency, body-size and timeout caps from `max_concurrent_requests`, `max_request_body_mb`, `request_timeout_s`; FR-020). *Progress 2026-10-31: `client.py` and `websocket.py` are written and tested against a real unix-socket server (`tests/unit/test_upstream_client.py`, `test_upstream_websocket.py`); `caps.py` is not written, so the task stays open.*
- [X] T085 [US1] Implement `src/appmux/edge/pipeline.py` and extend `src/appmux/edge/app.py`: the fixed order of design §3.4, the status codes of the edge contract, the per-app in-memory route table read (written only by the reconciler), the `404` for a denied child route, the identity from `actor_from_headers(request.headers)` (pass Starlette's `Headers`, so a repeated identity header is seen; a `MissingIdentity` becomes the `401` of the contract and the visitor token is read with `visitor_token.get_secret_value()` only where it is forwarded), the client address from `X-Real-Ip`, and the status pages for `502`, `503` and `504` in `src/appmux/edge/status_pages.py`.
- [ ] T086 [US1] **Capacity run first** (quickstart §3): write the load generator `tests/load/http_load.py` (500 requests a second over 10 connections, paced; reports p95, error rate and edge CPU; port from `specs/001-appmux-v1/spikes/scripts/spike-07/http_loadgen.py`) and `tests/integration/test_edge_capacity.py`, which seeds the route table with one sandboxed `light_fastapi` through the launcher and runs the load on the container. If the real edge cannot hold the rate with room to spare, stop and revisit ADR 0014 before building further; record the numbers in `specs/001-appmux-v1/spikes/README.md` under a dated "plan checks" heading. *Progress 2026-10-31:* the load generator `tests/load/http_load.py` is written and tested (21 tests, `uv run pytest tests/load`) and `tests/integration/test_edge_capacity.py` is written (asserts zero failed requests and at least 95% of the rate, no p95 or CPU limit); the capacity run has NOT happened, so no number is recorded and ADR 0014's premise is still unmeasured on the real edge.

### Implementation for User Story 1 - the release path

- [X] T087 [US1] Implement `src/appmux/appyaml/appyaml.py`: the `command` and `env` parser and the manifest model (FR-033); `databricks.yml` and the requirements check come in US6. The manifest's `runtime` is taken from the marker files (`requirements.txt`, `package.json`); when there is none and the first word of `command` starts with `python`, it is Python, because fixtures such as `absolute_links` carry no `requirements.txt`; otherwise `runtime` stays unset and the release is not refused for that.
- [X] T088 [US1] Implement `src/appmux/packages/store.py`: the content store in the appmux SP's workspace folder (archive named by digest, `put`, `get`, `exists`, 500 MB refusal). Port from `specs/001-appmux-v1/spikes/scripts/spike-04/store_probe.py`.
- [X] T089 [US1] Implement `src/appmux/reconciler/snapshot_source.py`: read `source_path` with the presser's OBO token and scope `workspace.workspace:read`, build the archive, compute the digest, store it, answer "already up to date" for a live digest, refuse an empty listing. Port from `specs/001-appmux-v1/spikes/scripts/spike-15/workspace_read_probe.py`.
- [X] T090 [US1] Implement `src/appmux/packages/environment.py`: list the parent environment's installed distributions with `importlib.metadata` and answer whether a requirements file is satisfied by them. Wheel and index modes come in US7; until then an unsatisfied requirement fails the release listing every unresolved line.
- [X] T091 [US1] Implement `src/appmux/reconciler/states.py` (release state table), `src/appmux/reconciler/route_table.py` (the one place that sets routes) and `src/appmux/reconciler/health.py` (the health clock). *Progress 2026-10-31: `states.py` and `route_table.py` are tested in `tests/unit/test_release_states.py`, and `health.py` in `tests/unit/test_health_clock.py`.*
- [X] T092 [US1] Implement `src/appmux/reconciler/update.py`: the Update flow of [design §5.2](../../docs/product/v1-design.md#52-register-update-roll-back-and-keep-releases) steps 1 to 6 (authorize, read, snapshot, parse `app.yaml`, create the release as target, hand over to the reconciler loop); a failure before the health check leaves the route table and the running release untouched and stores the reason and a log tail (FR-002).
- [X] T093 [US1] Implement `src/appmux/reconciler/reconciler.py`: the loop that takes a release through build → start (through `sandbox/launcher.py` only) → health → `active`, and at startup reads the registry and opens routes one by one as health passes (FR-008). Add `SIGTERM` handling to `src/appmux/__main__.py`: stop accepting, flush metrics if present, stop children in parallel within 15 s. *Progress 2026-10-31:* `reconciler.py` (build, start, health, activate, restore at start-up, `stop_all(within_s)`) passes 62 tests against fakes. `SIGTERM` is wired through the gate's shutdown hooks (`assembly.Service.stop`, tested in `tests/contract/test_service_wiring.py`): children stop first, then the registry closes. The children get `CHILD_STOP_BUDGET_S` = 4 s, not 15 s: uvicorn waits up to 10 s for open connections first and the platform ends the process 15 s after `SIGTERM` ([TD-015](../../docs/tech-debt.md)). "Flush metrics if present": no metrics sink exists yet, so nothing is flushed; the sink's flush plugs into the shutdown hooks when T168 wires it. Nothing here has run in a container.
- [X] T094 [US1] Implement `src/appmux/registry/apps.py`, `src/appmux/registry/releases.py` and `src/appmux/admin/apps.py`, `src/appmux/admin/releases.py` for `POST /apps`, `GET /apps/{app}`, `POST /apps/{app}/releases`, `GET /apps/{app}/releases/{id}` (`app.register`, `app.update`); `register` writes the app stopped with no release (the owner and `source_path` are platform fields written under the admin's role).
- [X] T095 [US1] Plug the edge and the reconciler into `src/appmux/__main__.py` in the order of the plan: settings, uvicorn bound with the gate, then in the background task registry, `self_check`, reconciler, and last the gate opens to the edge (routes then open one by one as health passes, FR-008). *Progress 2026-10-31:* the order is registry → content-store folder → `self_check` → reconciler → gate opens, pinned by `tests/contract/test_service_wiring.py` against fakes (workspace over `MockTransport`, a recording reconciler). A failed self-check opens the gate and starts no app; a store folder that cannot be made keeps the gate shut. The reconciler starts only if the self-check passes at start-up, so a later pass needs a restart ([TD-015](../../docs/tech-debt.md)). Not yet run in a container.

### Integration for User Story 1 (Databricks container)

- [X] T096 [US1] Write `tests/integration/test_host_a_child.py`: register `light_fastapi`, request `/<slug>/` before any release (no child receives it), press Update, request again after health; then `startup_crash`, `always_500` and `slow_start` (past and within the deadline) and assert the acceptance scenarios 1 to 5 of US1, including the route never opening for a failed release. For the two routing fixtures, assert the `hits` counter of `absolute_links` (the stray `/static/x.css` request never reached the child) and the `base_path` and `source` of `base_path_aware`. Passed 10 of 10 on 2026-10-31 against the Free Edition dev App, from the dev machine (`scripts/run-remote-tests.sh`); the run found that `slow_start` read "connection closed before an answer" because the relay listened before the child's port, so the relay now listens only once the port accepts (`sandbox/relay.py`).
- [ ] T097 [US1] Update `ARCHITECTURE.md` (mark `edge`, `routing`, `rewrite` response side, `upstream`, `reconciler` and the first part of `packages` and `appyaml` implemented) and [contracts/edge-http.md](contracts/edge-http.md) only if a run changed a status code or header.

**Checkpoint**: MVP: US4 + US3 + US1 together host a healthy child safely and never open a route for an unhealthy one.

---

## Phase 6: User Story 2 - Update with zero downtime and roll back to any release (Priority: P1)

**Goal**: update beside the running copy, swap atomically after health, roll back to any live release without reading a source folder, restart a crashed child with backoff.

**Independent Test**: hold steady load on app A and run the operations of SC-001.

### Tests for User Story 2 (write first)

- [X] T098 [P] [US2] Write `tests/unit/test_swap.py`: the swap changes the route table once and, in one transaction, makes the new release `active` and the old one `superseded`; two healthy releases of one app leave exactly one active; in-flight requests get 30 s and open WebSockets are closed at that point; a failed new copy leaves the old release and route untouched (FR-004).
- [X] T099 [P] [US2] Write `tests/unit/test_crash_backoff.py`: restart delays 1, 2, 4, 8, 16 s, then `crashed`; only that app's route answers `503`; no automatic activation of an older release (FR-007).
- [X] T100 [P] [US2] Write `tests/unit/test_rollback.py`: activating any live release uses the stored snapshot, wheels and config, reads no source and needs no token; a release whose requirement keys are missing fails before it starts and lists them; a release that never passed health can be activated (FR-006).
- [X] T101 [P] [US2] Write `tests/unit/test_config_release.py`: a config change creates a new release with the same snapshot and goes through the same swap; resources, secrets and own-SP keys are not part of a release (FR-009); a rename restarts a running app under the new prefix, the old URL answers `404`, a failing new copy leaves the old one serving, a stopped app only changes its row (FR-010).
- [X] T102 [P] [US2] Write `tests/contract/test_activate_config_restart.py`: `POST …/activate`, `PUT /apps/{app}/config` (needs `version`), `POST /apps/{app}/start|stop|restart`, `PATCH /apps/{app}` with `slug`.

### Fixtures for User Story 2

- [X] T103 [P] [US2] Create the fixture `tests/fixtures/crash_loop/`: starts, serves, then exits after a delay on every start.

### Implementation for User Story 2

- [X] T104 [US2] Implement `src/appmux/reconciler/swap.py`: start the new copy on a private route beside the old one, health-check it, swap the route table atomically, run the registry transaction (new `active`, old `superseded`), drain 30 s, close remaining WebSockets, stop the old copy.
- [X] T105 [US2] Implement `src/appmux/reconciler/restart.py`: crash detection, backoff, the `crashed` state, exit code and last log lines kept.
- [X] T106 [US2] Implement `src/appmux/reconciler/activate.py` (rollback: admission is US10, requirements check is US6, here the swap with stored artefacts) and `src/appmux/reconciler/rename.py` (restart through the swap under the new prefix).
- [X] T107 [US2] Implement the routes in `src/appmux/admin/releases.py` and `src/appmux/admin/apps.py`: `POST …/activate`, `PUT /apps/{app}/config` (`config.edit`), `POST …/start`, `…/stop`, `…/restart`, `PATCH /apps/{app}` and `PUT /apps/{app}/owner`, with `version` checks and audit rows.
- [ ] T108 [US2] Write `tests/load/ops_under_load.py` (port from `specs/001-appmux-v1/spikes/scripts/spike-07/load_ops.py`) and `tests/integration/test_zero_downtime.py` for SC-001: a no-op control run, register B, update B, update A with a good release, update A with a release that fails health (A keeps serving), a heavy build in parallel, an egress rule edit; report failed requests and p95 before and during each operation against the control. **Still open:** a reduced run (20 requests a second, Free Edition, through the platform's proxy) ran twice on 2026-11-01: run 1 had one `502` made by the edge during the failed-health operation, run 2 had none ([log](spikes/README.md#reduced-zero-downtime-run-from-the-dev-machine-2026-11-01), TD-019); it is not SC-001 evidence (the rate is 4 % of the task's, and the figure to tick is the full 500 a second with the edge's CPU read).
- [X] T109 [US2] Write `tests/integration/test_crash_and_rollback.py`: `crash_loop` ends `crashed` with only its route at `503`; rollback to release 3 of 5 starts from the stored snapshot with the source folder deleted. Passed 2 of 2 on 2026-10-31 against the Free Edition dev App, from the dev machine (`scripts/run-remote-tests.sh`); the source folder is "deleted" by pointing `source_path` at a folder that never existed, because the test cannot write to the workspace.
- [ ] T110 [US2] Update `ARCHITECTURE.md` (swap, restart, rollback implemented) and write the SC-001 numbers into `specs/001-appmux-v1/spikes/README.md` under the dated "plan checks" heading.

**Checkpoint**: SC-001 holds on the container: no more failed requests than the control run.

---

## Phase 7: User Story 5 - Manage releases: name, tag, lock, delete, caps (Priority: P2)

**Goal**: keep the releases that matter within the caps a platform admin sets.

**Independent Test**: drive an app past `max_releases_per_app` with some releases locked.

### Tests for User Story 5 (write first)

- [X] T111 [P] [US5] Write `tests/unit/test_release_retention.py` (rules 8, 9, 10, 13): the active, target, in-progress and locked releases are protected; tags protect nothing; the oldest unprotected release is deleted at the cap and `release.delete` is recorded by `appmux`; a full cap with every release protected refuses the Update with `app 'sales' has 10 releases and all are protected (active, target or locked); unlock one`; a fourth lock is refused with `app 'sales' already has 3 locked releases; unlock one first`; two simultaneous locks at the cap admit one; a release is deletable the moment it stops being active; release numbers are never reused; a deleted release cannot be activated. *Done 2026-10-31:* `tests/unit/test_release_retention.py` (42 tests) over the in-memory registry; "a deleted release cannot be activated" is tested at the state-machine and `activation_changes` level, the activate route has its own test in T100.
- [X] T112 [P] [US5] Write `tests/unit/test_content_store_cleanup.py` (rule 11): a file is removed only when no live release references it and it is older than a day.
- [X] T113 [P] [US5] Write `tests/contract/test_release_management.py`: `PUT …/releases/{id}` (name, tags), `PUT|DELETE …/lock`, `DELETE …/releases/{id}` (`409` for active, target, in-progress, locked). *Done 2026-10-31:* 58 tests against the in-memory stub registry.

### Implementation for User Story 5

- [X] T114 [US5] Implement `src/appmux/reconciler/retention.py`: protection rules, oldest-unprotected clean-up, lock counting in one transaction that locks the `app_config` row; call it from `src/appmux/reconciler/update.py` before a release is created. *Done 2026-10-31:* `retention.py` is called from `update.py` before the release is created. Deviation: the lock count is guarded by the `app_config` `version` (a stale write answers `409`), not by a row lock, because the `Registry` protocol has no row-lock primitive; not run against a real Lakebase.
- [X] T115 [US5] Implement the routes in `src/appmux/admin/releases.py`: label, tag, untag, lock, unlock, delete (`release.label`, `release.tag`, `release.untag`, `release.lock`, `release.unlock`, `release.delete`). *Done 2026-10-31:* `PUT`/`DELETE` of a release and `PUT`/`DELETE` of its lock, with the `release.*` audit actions; against the stub registry only.
- [X] T116 [US5] Implement the daily content-store clean-up job in `src/appmux/packages/cleanup.py` and schedule it from `src/appmux/__main__.py`. *Progress 2026-10-31:* the job `cleanup.py` and its store adapter (`list_folder`, `delete_path`, `run_cleanup_once`) pass their tests against `MockTransport`; the daily schedule is wired (`Service.start_cleanup`, a step of `__main__.py`: first pass 1 h after the start, then every 24 h) and tested; the workspace `list` `modified_at` and `delete` calls have not run against a real workspace.
- [X] T117 [US5] Update `ARCHITECTURE.md` (retention implemented). *Done 2026-10-31:* retention, release routes and the package routes of the Update are in the `reconciler`, `admin` and `appyaml, packages, secrets` rows.

**Checkpoint**: an app driven past its cap keeps the locked releases and refuses when all are protected.

---

## Phase 8: User Story 6 - Declare resources and secrets the way a standalone app does (Priority: P2)

**Goal**: a child's own `app.yaml` and optional `databricks.yml` name what it needs; the app admin attaches resources checked with their own token; secrets are references.

**Independent Test**: the two `app.yaml` fixtures and the `databricks.yml` fixture of design §14.

### Tests for User Story 6 (write first)

- [X] T118 [P] [US6] Write `tests/unit/test_requirements_check.py` (FR-035): every `valueFrom` key that is not a resource of the app is listed at once, before any build; attaching the keys and activating starts the release with no source read.
- [X] T119 [P] [US6] Write `tests/unit/test_databricks_yml.py` (FR-034, rule 12): resources mapped to types, a `${…}` value left unresolved, several apps in one file, ignored keys warned, an invalid file never refuses an Update, nothing is attached.
- [X] T120 [P] [US6] Write `tests/unit/test_attach_check.py` (FR-037, rules 4 to 6) against stubbed Databricks calls: `ok`, `unverified`, no access (refused, nothing written), `shared_sp` refused for an `unverified` resource, a resource's `permission` is a level of its `type`, `access_as` obeys the delegation flags, `own_sp_*_key` must point to a `secret` resource of the app.
- [X] T121 [P] [US6] Write `tests/unit/test_secret_resolution.py` (FR-038, rule 15): a `scope/key` reference is read by the appmux SP at child start, an unreadable one fails the release naming the scope, the value never appears in a log, audit row, release row or build log (a build log that echoes a value is scrubbed).
- [X] T122 [P] [US6] Write `tests/contract/test_resources.py`: `GET /apps/{app}/requirements`, `PUT /apps/{app}/resources/{key}`, `DELETE …`, with a `secret` stored as `scope/key`.

### Fixtures for User Story 6

- [X] T123 [P] [US6] Create the fixtures `tests/fixtures/appyaml_missing_resource/` (a `valueFrom` key with no resource), `tests/fixtures/appyaml_invalid/` (both `app.yaml` and `app.yml`; an `env` entry with both `value` and `valueFrom`) and `tests/fixtures/bundle_offer/` (a `databricks.yml` with resources). Tests compare the listed keys and resources as a set, not as an ordered list.
- [X] T124 [P] [US6] Create the fixture `tests/fixtures/secret_registry_snooper/`: tries other apps' secrets and the registry, with and without a shared-SP token; every attempt must fail.

### Implementation for User Story 6

- [X] T125 [US6] Extend `src/appmux/appyaml/appyaml.py` with the `valueFrom` model and add `src/appmux/appyaml/requirements.py` (the check) and `src/appmux/appyaml/bundle.py` (`databricks.yml` reader that only produces offers).
- [X] T126 [US6] Implement `src/appmux/secrets/resolver.py`: read the value of a `secret` resource with the appmux SP at child start; return it only to `sandbox/environment.py`; port from `specs/001-appmux-v1/spikes/scripts/spike-11/secrets_probe.py`.
- [X] T127 [US6] Implement `src/appmux/registry/resources.py` and `src/appmux/admin/resources.py`: attach check with the attacher's own token and the `sql` scope (port from `specs/001-appmux-v1/spikes/scripts/spike-16/attach_probe.py`), the 8 verifiable types, `unverified` for the other 6, the warehouse from `attach_check_warehouse_id` (an attach that needs it and finds it unset is refused naming the setting), who attached it recorded.
- [X] T128 [US6] Call the requirements check from `src/appmux/reconciler/update.py` and `src/appmux/reconciler/activate.py` before any build, and the secret resolver from `src/appmux/sandbox/environment.py`'s caller in `src/appmux/reconciler/reconciler.py`.
- [ ] T129 [US6] Write `tests/integration/test_resources_container.py`: the `appyaml_missing_resource` release fails before any build listing every key; after attaching and activating it starts; a secret in a scratch scope on the Free Edition workspace reaches only that child.
- [ ] T130 [US6] Update `ARCHITECTURE.md` (`appyaml`, `secrets` implemented) and `docs/reference/databricks-apps-platform.md` only where a run contradicts it.

**Checkpoint**: both `app.yaml` fixtures and the `databricks.yml` fixture behave as the acceptance scenarios say.

---

## Phase 9: User Story 7 - Build with no package index (Priority: P2)

**Goal**: packages come from the environment first, then from hashed wheels in workspace folders, then from the index only in an index mode.

**Independent Test**: the four offline-install fixtures with the index unreachable.

### Tests for User Story 7 (write first)

- [X] T131 [P] [US7] Write `tests/unit/test_package_resolution.py` (FR-031): environment first; a hashed wheel overwrites a differing version in the overlay for that release only; a pin neither the environment nor a wheel covers produces one list of every unresolved requirement; a store hit by sha256 fetches nothing; wheels fetched in order; a wheel list per release; the overlay cache key; the re-check at every start fails the release before it starts when the environment no longer satisfies a requirement; `pip check` output is a warning, never a failure ([research D6](research.md#d6-a-conflicting-overlay)).
- [X] T132 [P] [US7] Write `tests/unit/test_build_command.py` (FR-032): the overlay is built with `pip install --no-index --no-deps --require-hashes --find-links W --target O`; `wheelhouse` mode gives the build an all-deny egress list; the child starts with `PYTHONPATH=O`; entry points start with the overlay path set.

### Fixtures for User Story 7

- [X] T133 [P] [US7] Create the fixtures `tests/fixtures/wheels_only/` (dependencies only in its wheel folder), `tests/fixtures/wheel_reuse/` (needs a wheel already in the store), `tests/fixtures/env_covered/` (fully covered by the environment; must fetch no wheel) and `tests/fixtures/newer_wheel/` (needs a newer version of a package the environment has; the wheel must win), each with a `make_wheels.py` that builds its wheels offline (port from `specs/001-appmux-v1/spikes/scripts/spike-13/make_marker_wheel.py`).

### Implementation for User Story 7

- [X] T134 [US7] Implement `src/appmux/packages/modes.py` (the package modes, including `index` and `wheelhouse`), `src/appmux/packages/resolve.py` (environment first, then wheels, then index; one list of unresolved requirements; the coverage report at Update) and `src/appmux/packages/wheels.py` (the wheel store by sha256 and the fetch in order). Port coverage logic from `specs/001-appmux-v1/spikes/scripts/spike-14/coverage.py` and `plan_and_check.py`.
- [X] T135 [US7] Implement `src/appmux/packages/overlay.py`: build the overlay inside the sandbox with the app's egress rules and `build_timeout_s`; mount it read-only; cache by key under `/tmp`. Port from `specs/001-appmux-v1/spikes/scripts/spike-18/sandbox_overlay.py`. *Progress 2026-10-31:* `overlay.py` holds the build plan, cache key, atomic publish and shared time budget. *Done 2026-10-31:* `SandboxStepRunner` runs the overlay build through the launcher's public seams (`BuildSandbox`/`StartSandbox`), tested against fakes; a real `pip` in a real sandbox has not run (container only).
- [X] T136 [US7] Replace the unresolved-requirement failure of `src/appmux/packages/environment.py` with the call into `resolve.py`; extend `src/appmux/reconciler/reconciler.py` to build the overlay in the `building` state and to re-check the environment at every start.
- [X] T137 [US7] Write `tests/integration/test_offline_install.py`: the four fixtures with the index unreachable on the container; `env_covered` fetches no wheel. *Written 2026-10-31:* four tests, skipped without `APPMUX_TEST_CONTAINER=1`.
- [X] T138 [US7] Update `ARCHITECTURE.md` (`packages` implemented, with the coverage-report behaviour and the warn-only conflict rule).

**Checkpoint**: the four offline-install fixtures pass with the index blocked.

---

## Phase 10: User Story 8 - Ask a platform admin through a request inbox (Priority: P2)

**Goal**: a visitor asks for an app, an app admin asks for delegation or a platform-field change, and a platform admin answers in an inbox; a request grants nothing by itself.

**Independent Test**: the request walkthrough of SC-004 and the request rows of the `authorize()` table.

### Tests for User Story 8 (write first)

- [X] T139 [P] [US8] Write `tests/unit/test_request_catalogue.py` (rules 7 and 16, FR-040 to FR-045): one test per catalogue entry (`register`; `delegation` with `obo` and `shared_sp`; `settings` with `slug`, `display_name`, `source_path`, `memory_budget_mb`) with a valid and an invalid payload, the asker rule and the effect on `apps`; one pending request per (app, kind) or per proposed slug; 3 pending `register` requests per person; a reason of up to 500 characters; withdraw only by the asker and only while pending; a request that changes nothing, a `shared_sp` request while `shared_sp_delegation_allowed` is off and a `settings` payload with no field are refused.
- [X] T140 [P] [US8] Write `tests/unit/test_request_approval.py`: approval is one transaction that applies every field or none, needs a reason when it holds a gate field (`register`, `source_path`, `obo`, `shared_sp`), records both names when asker and approver are the same person; a slug taken at approval closes the request as `denied` with `slug '<slug>' is already taken (automatic)` under the admin who pressed Approve; a rename applied to a running app restarts it through the swap; an unknown kind in a row cannot be approved; a forged `approved` row changes nothing because `authorize()` and the reconciler read only `apps`; deleting an app, the owner and egress rules are not requests.
- [X] T141 [P] [US8] Write `tests/contract/test_requests_api.py`: `GET /requests` (own, an app's, the inbox), `POST /requests`, `POST …/withdraw`, `POST …/approve|deny`, `PUT|DELETE /apps/{app}/delegation/{kind}` (`delegation.grant` needs a `reason`).
- [X] T142 [P] [US8] Write `tests/unit/test_credentials.py` (FR-036, FR-039, [design §8.5](../../docs/product/v1-design.md#85-choosing-a-mode-and-delivering-credentials)): a child gets no Databricks credential unless one was delegated or attached; an own SP sets the three standard variables from its `secret` references; a shared-SP token is minted by appmux, written atomically to a file in the child's run directory and re-minted when less than 10 minutes of life remain, its `expiry` written as local time, and never put in a static variable; `shared_sp` is refused while `shared_sp_delegation_allowed` is off and for an `unverified` resource; a personal access token is not accepted anywhere; an OBO token is never stored.

### Implementation for User Story 8

- [X] T143 [US8] Implement `src/appmux/admin/request_catalogue.py`: the kinds, their payload schemas and gate fields, sharing the field validators of the direct edits in `src/appmux/admin/apps.py` so a request and an edit are checked by the same code. The module also owns the catalogues that `registry/models.py` and `registry/audit.py` deliberately do not check: the request kinds with their payload keys, the audit actions with their detail keys, the keys of a detected manifest, the setting keys and the tag rules, and the exactly-one-of rule of an `EnvEntry`.
- [X] T144 [US8] Implement `src/appmux/registry/requests.py`: create, withdraw, approve (lock the `apps` row, or for `register` check that no app has the slug; apply; close; audit before and after) and deny, all under the platform admin's own role where the data model says so.
- [X] T145 [US8] Implement `src/appmux/admin/requests.py` and the delegation routes in `src/appmux/admin/delegation.py`; call the rename cascade of `src/appmux/reconciler/rename.py` after an approved `slug`.
- [X] T146 [US8] Implement `src/appmux/secrets/shared_sp_token.py` (mint, atomic file write, re-mint before expiry; port from `specs/001-appmux-v1/spikes/scripts/spike-10/refresh_test.py`), `src/appmux/secrets/token_command.sh` (the small script that prints the token file, placed in the child's run directory) and `src/appmux/secrets/credentials.py` (mode choice per child and the own-SP variables); have `src/appmux/sandbox/environment.py` set `DATABRICKS_AUTH_TYPE=databricks-cli` and `DATABRICKS_CLI_PATH` for a shared-SP child; the values reach the child through `ChildIdentity.credentials`, whose `repr` hides them (do not format it with `dataclasses.asdict`, which would expose them).
- [ ] T147 [US8] Write `tests/integration/test_credential_modes.py`: an own-SP child and a shared-SP child each call the Databricks API through the unmodified SDK, the shared-SP child across a token expiry (use the short advertised lifetime of spike 10's `refresh_test.py`), and `secret_registry_snooper` with the shared-SP token still reaches neither other apps' secrets nor the registry beyond the accepted residual of [TD-002](../../docs/tech-debt.md#td-002-an-obo-childs-token-reaches-the-registry).
- [ ] T148 [US8] Write `tests/integration/test_requests_walkthrough.py`: Grace requests an app, a platform admin approves it (the app exists, owned by Grace, stopped, no release); Carol's OBO request; a taken slug at approval; a rename of a running app (restart under the new prefix, old URL `404`).
- [X] T149 [US8] Update `ARCHITECTURE.md` and add the new route names to `contracts/admin-api.md` only if a route changed during the work.

**Checkpoint**: every request rule of data model §7 has a passing test.

---

## Phase 11: User Story 9 - Platform admin supports any app, with two ways to open it (Priority: P2)

**Goal**: a platform admin holds implicit `CAN_MANAGE` on every app, can read a release's source, and chooses between **Open without token** and **Open as a user** for an OBO-delegated app.

**Independent Test**: the platform-admin rows of the `authorize()` table plus a check of the headers the child receives in each mode.

### Tests for User Story 9 (write first)

- [X] T150 [P] [US9] Write `tests/unit/test_open_mode.py` (rule 17, FR-028): without a grant on an OBO-delegated app the default removes `X-Forwarded-Access-Token` and the app still sees the email; **Open as a user** writes `app.view_as_user` and passes the token until switched back; the `__Host-appmux_open` cookie is removed before forwarding; a non-OBO app has one **Open** and no token; the cookie is set only by the edge.
- [X] T151 [P] [US9] Write `tests/contract/test_source_viewer.py` (FR-029): `GET …/source` and `…/source/{path}` return text files only (binary files are listed, not returned), each call writes `release.view_source`, `CAN_USE` alone is denied.
- [X] T152 [P] [US9] Write `tests/contract/test_platform_admin_support.py`: a platform admin edits another team's configuration (audited, no `app_grants` row appears); a change that would leave `platform_admins` without a `user` entry is refused with `PUT /platform/admins`. *Progress 2026-10-31:* `tests/contract/test_platform_admin_support.py` passes (55 tests) and proves the audited admin edit through `PUT /apps/{app}/grants` (no `app_grants` row for the admin) and the last-admin refusal; the same case through the configuration routes is `test_a_platform_admin_edits_another_teams_config_with_no_grant_of_their_own` in `tests/contract/test_activate_config_restart.py` (audit row under the admin's name, no `app_grants` row).

### Implementation for User Story 9

- [X] T153 [US9] Implement `src/appmux/edge/open_mode.py` (cookie read and write, the decision passed to `rewrite/request.py`) and the `POST /apps/{app}/open-mode` route in `src/appmux/admin/identity.py`; extend `GET /apps` with the two open modes.
- [X] T154 [US9] Implement `src/appmux/admin/source_viewer.py` and the platform-admin routes `PUT /platform/admins` in `src/appmux/admin/platform.py`, including the last-admin guard.
- [ ] T155 [US9] Write `tests/integration/test_open_modes.py`: an OBO fixture app opened by a platform admin receives no token by default and receives it after the switch, with its audit row (use `light_fastapi` with a `/headers` route).
- [ ] T156 [US9] Update `ARCHITECTURE.md` and the "Platform at a glance" row of `docs/reference/databricks-apps-platform.md` only if a run changed a fact.

**Checkpoint**: the platform-admin rows pass; the child sees the right headers in both modes.

---

## Phase 12: User Story 10 - Stay inside the slot's memory (Priority: P2)

**Goal**: refuse to start or update an app whose budget would not fit next to the running ones.

**Independent Test**: set the budget for 10 simulated apps and try to start an 11th (SC-003).

### Tests for User Story 10 (write first)

- [X] T157 [P] [US10] Write `tests/unit/test_admission.py` using the worked example of [design §5.7](../../docs/product/v1-design.md#57-memory-and-admission-control): 8192 MB limit, 1024 MB reserve, 10 apps of 512 + 13 MB fit with 1918 MB free, an update beside its running release adds 525 MB and is allowed, an app of 2048 MB is refused with `needs 2061 MB, 1393 MB free`; the second copy of a swap counts for at most `health_deadline_s`; the running release is untouched on refusal (FR-050).
- [X] T158 [P] [US10] Write `tests/unit/test_rss_watchdog.py`: a child over its budget (with headroom) is stopped and no other app is; `RLIMIT_AS` is set from the budget with the headroom factor.

### Fixtures for User Story 10

- [X] T159 [P] [US10] Create the fixtures `tests/fixtures/memory_fastapi/` (holds a configurable amount of memory), `tests/fixtures/memory_hog/` (allocates until stopped), `tests/fixtures/streamlit_app/` and `tests/fixtures/node_app/` (the last uses `index` mode; no dependency).

### Implementation for User Story 10

- [X] T160 [US10] Implement `src/appmux/reconciler/admission.py`: read the container's memory limit at start, apply the formula, return the `needs <n> MB, <m> MB free` reason; call it from `update.py`, `activate.py` and the start path.
- [X] T161 [US10] Implement `src/appmux/sandbox/watchdog.py`: best-effort RSS watchdog over the child's process tree; stop only the offender; set `RLIMIT_AS` in `sandbox/launcher.py` from the budget.
- [ ] T162 [US10] Write `tests/integration/test_capacity.py` (SC-003): register 10 fixtures (a mix of the four kinds), start and load them, report RSS per app idle and loaded, per-sandbox overhead, start time and CPU, the total against the slot limit and a linear 100-app estimate labelled as an estimate, then try an 11th that exceeds the budget and assert the refusal; write the numbers into `specs/001-appmux-v1/spikes/README.md`.
- [ ] T163 [US10] Update `ARCHITECTURE.md`, the memory figures in `docs/reference/databricks-free-edition.md` if they differ, and [TD-003](../../docs/tech-debt.md#td-003-cpu-and-memory-limits-are-best-effort) with the measured memory-hog result.

**Checkpoint**: SC-003 report written; the 11th app is refused with the amounts.

---

## Phase 13: User Story 11 - Run everything from the admin UI and watch per-minute metrics (Priority: P3)

**Goal**: every action is available in the admin UI and API; each app's traffic, latency, memory and egress drops show per minute.

**Independent Test**: the scripted walkthrough of SC-004.

### Tests for User Story 11 (write first)

- [X] T164 [P] [US11] Write `tests/unit/test_metrics_aggregate.py` (FR-052): one-minute aggregates per app with the columns of [design §10.2](../../docs/product/v1-design.md#102-metrics), the 11-bucket latency histogram, a percentile read as the upper bound of its bucket, a bounded buffer when Lakebase is down with a visible gap, platform rows (`app = _platform`), retention of 7 days as a job.
- [X] T165 [P] [US11] Write `tests/unit/test_csp.py`: the CSP carries `script-src 'self'` plus the SHA-256 of each inline script found in the export's HTML files; the headers `frame-ancestors 'none'` and `Cross-Origin-Opener-Policy: same-origin` are present and no CORS header is ([FR-021](spec.md#functional-requirements), spike 19). Use `specs/001-appmux-v1/spikes/scripts/spike-19/check_csp.py` as the reference check.
- [X] T166 [P] [US11] Write `tests/contract/test_platform_and_metrics_api.py`: `GET /platform`, `PUT /platform/settings/{key}` (an unknown key and a broken rule between keys answer `400`), `GET /audit`, `GET /metrics`.
- [ ] T167 [P] [US11] Write `tests/integration/test_sc004_walkthrough.py`: a script that does every step of SC-004 through the API (register directly; request and approve a new app; request and approve OBO and shared-SP delegation and a settings change; rename; edit an egress rule; view a source as a platform admin; share; attach resources and accept `databricks.yml` offers; the release actions; restart and stop) with no SQL typed, then compares the final rows with the expected ones.

### Implementation for User Story 11 - backend

- [ ] T168 [US11] Implement `src/appmux/metrics/aggregate.py` (in-memory minute buckets fed by the edge), `src/appmux/metrics/flush.py` (one batch per minute, bounded buffer) and `src/appmux/metrics/retention.py` (hourly delete by `minute`); feed the edge from `src/appmux/edge/pipeline.py` and the pump counters from `sandbox/pump.py`. *Progress 2026-10-31:* `metrics/aggregate.py`, `flush.py` and `retention.py` are written and tested (`tests/unit/test_metrics_*.py`); the feed from `edge/pipeline.py` (T085) is wired (`Service.metrics` goes to the edge, `Service.start_metrics` starts the writer and `Service.stop` flushes the last minute; tested in `tests/contract/test_service_wiring.py`), but the feed from `sandbox/pump.py` (T061), the container and per-child figures (no source) and the restart, admission and registry-call counters have no call site, so the task stays open.
- [X] T169 [US11] Extend `src/appmux/admin/platform.py` with `GET /platform` (settings, admins, global egress, the `self_check` result, memory against the slot limit) and `PUT /platform/settings/{key}`, and add `src/appmux/admin/metrics.py` (`GET /metrics`, `GET /audit`).
- [X] T170 [US11] Implement `src/appmux/edge/csp.py` and `src/appmux/edge/ui_static.py`: serve the export directory under `/_appmux/` and `/`, hash each page's inline scripts at start, add the headers of the edge contract. The directory is the optional setting `APPMUX_UI_DIR` (add it to `src/appmux/settings.py` with its test), default `web/out` relative to the working directory (the setting exists as `PlatformSettings.ui_dir` in `src/appmux/settings.py`; this task reads it, maps `/_appmux/<path>` to `<ui_dir>/<path>` and serves `index.html` at `/`), which is the same relative path in the repository and in `build/deploy/`; the container test below confirms the working directory on the platform. *Progress 2026-10-31:* `edge/csp.py` and `edge/ui_static.py` are written and tested (`tests/unit/test_csp.py`, `test_ui_static.py`, a fake export in `tmp_path`); mounted in `edge/app.py` the same day (`create_edge_app(ui_app=...)`, `tests/contract/test_edge_ui.py`); `__main__._ui_application` loads the export in the worker thread and, if it cannot be served, logs the cause and mounts `unavailable_ui_app()` (every UI path answers `503`, the admin API and proxy keep running). Only a container run confirms the working directory and that the platform proxy passes the headers.

### Implementation for User Story 11 - the UI

- [X] T171 [US11] Create the Next.js project in `web/` (`package.json`, `next.config.mjs` with `output: "export"`, strict `tsconfig.json`, kebab-case files, named exports) and `web/src/lib/api/` (a Zod-validated client of [contracts/admin-api.md](contracts/admin-api.md), the schema the single source of each type). Vet each npm package as in the Setup phase, set up the stack's standard formatter, linter and a `tsc --noEmit` gate before the first page, and add their commands to `AGENTS.md` (Commands).
- [X] T172 [P] [US11] Build `web/src/app/page.tsx` (launcher: the apps the visitor may open, **Request a new app**, own requests, the two open modes, a pending-request count for a platform admin).
- [X] T173 [P] [US11] Build `web/src/app/app/page.tsx`, which reads the slug from the query string (a static export cannot prerender slugs that are unknown at build time). The app page shows status, releases with name, tag and lock, Update, Activate, Lock, Unlock, delete, Restart, Stop/Start, the Requirements panel, resources, sharing, the source viewer and the request buttons.
- [X] T174 [P] [US11] Build `web/src/app/platform/page.tsx` (platform page: register an app, per-app fields, the inbox, delegations, global and per-app egress rules, platform admins, settings, the `self_check` result, memory against the slot limit).
- [X] T175 [P] [US11] Build `web/src/app/dashboard/page.tsx` (per-minute counts, latency histogram, memory peak, egress drops).
- [ ] T176 [US11] Write `web/build.sh` (`npm ci && npm run build`, output `web/out`), run it, run the SC-004 walkthrough and the CSP test against the built export, and have `scripts/sync-deploy.sh` call it (its header says `web/out` is only a warning until this script exists; remove that note). Re-run the strict-CSP check on the built export with a page that imports `web/src/lib/api/`, because the Zod `jitless` setting is what keeps a client-side schema off `unsafe-eval`. Port from `specs/001-appmux-v1/spikes/scripts/spike-19/build.sh`. *Progress 2026-10-31:* the browser CSP check of the export exists as `npm run test:e2e` (`web/e2e/`, Chromium, a stand-in edge with the reference policy and a mock API); its 69 tests pass on pages that parse API responses with the Zod schemas. Still open: `web/build.sh`, the `sync-deploy.sh` call, the SC-004 walkthrough, and the same check against the real `csp.py` (T170).
- [ ] T177 [US11] Write `tests/integration/test_ui_on_container.py`: deploy and check that every page loads with no CSP violation, no inline script outside the hash list, no cross-origin request, and polls the API every few seconds.
- [ ] T178 [US11] Update `ARCHITECTURE.md` (`admin`, `metrics`, `ui` implemented) and `docs/index.md` if it lists the UI.

**Checkpoint**: SC-004 passes; all four screens work through the API.

---

## Phase 14: Polish and cross-cutting concerns

**Purpose**: proofs and documents that span stories.

- [X] T179 [P] Create `tests/fixtures/README.md` listing the 10 well-behaved apps, about 10 hostile apps, 2 routing fixtures, 4 offline-install fixtures, 2 `app.yaml` fixtures and 1 `databricks.yml` fixture of design §14 against their directories, with the one-line purpose of each.
- [ ] T180 [P] Write `tests/integration/test_failure_table.py`: one test per row of the 27-row failure table in [design §11](../../docs/product/v1-design.md#11-failure-behaviour) that the earlier stories did not already cover; list the rows covered elsewhere in the module docstring with the test name.
- [ ] T181 Run the whole of [quickstart.md](quickstart.md) in order (local checks, deploy and `self_check`, real-edge capacity, SC-001 to SC-004, hostile fixtures) on the Free Edition workspace and write the dated results into `specs/001-appmux-v1/spikes/README.md` under "plan checks"; mark every figure "Free Edition only".
- [X] T182 Security pass: run the `security-audit` skill over `src/appmux/`, `app.yaml`, `.github/`, `scripts/` and `web/` (secret handling, path containment in the snapshot reader and source viewer, SSRF in the attach checks, response headers), fix findings, and record anything left open in `docs/tech-debt.md`.
- [ ] T183 Update the docs the finished work invalidates: `AGENTS.md` (Spec gate paragraph: tasks exist, code exists), `specs/001-appmux-v1/spec.md` Status line, `specs/001-appmux-v1/plan.md` Status line and Phase outputs row 2, `ARCHITECTURE.md` (remove every "planned" marker the tests now back), `docs/index.md`, and the ADRs whose "Must now be true" items were measured (status or result edits only; a reversal needs a new ADR).
- [ ] T184 Write the release workflow `.github/workflows/release.yml`: on a pushed tag `v*` (and by hand for a dry run) it runs `ruff`, `mypy` and `pytest tests/unit tests/contract`, `scripts/vendor-wheels.sh` (with `APPMUX_PYTHON` set to the runner's Python 3.11), `scripts/fetch-slirp4netns.sh`, `web/build.sh` and `scripts/assemble-deploy.sh --zip` (never with `APPMUX_VENDOR_ALLOW_LOOPBACK_HTTP_FOR_TESTS`, which exists for the tests only), then attaches `appmux-<version>.zip` and its `.sha256` to a GitHub Release. The zip carries `bin/slirp4netns` and `licenses/slirp4netns/`; the release notes name the pinned `slirp4netns` version and say the zip redistributes it under `GPL-2.0-or-later` with its source ([ADR 0015](../../docs/decisions/0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md)). The publishing step refuses to run (a dry run is unaffected) while that ADR's status line is not `accepted`. Pin every action by commit SHA, give the workflow `contents: write` only in the release job, and give it no Databricks credential: it builds, it never deploys. Dry-run it with `workflow_dispatch` and check the zip with `unzip -l`.
- [ ] T185 Run `/speckit-converge` once everything above is done; it appends any unbuilt work as new tasks to `specs/001-appmux-v1/tasks.md`.

---

## Phase 15: Admin UI overhaul and the backend it needs (added 2026-11-03)

**Purpose**: the owner's hand test as an app admin and an end user found eleven faults and asked for the Databricks Apps
console's layout and flows. Design: [admin-ui-design.md](../../docs/product/admin-ui-design.md); contract:
[the addendum](contracts/admin-api.md#addendum-routes-and-fields-for-the-admin-ui-overhaul-t187-onward);
decisions: ADR 0019 (identity, theme, CSP-safe primitives) and ADR 0020 (service-principal grants). A task is ticked only
when its tests pass; the lane that writes a route writes its tests first.

### Backend (disjoint lanes B1 to B4)

- [ ] T187 [B1] Write the tests first, then `GET /principals` (`src/appmux/admin/principals.py`, `src/appmux/identity/directory.py`): SCIM users, groups and service principals read with the person's token, then the platform token; the query allow-list; `everyone` row; per-directory `sources`; `principal.search` in `authz/actions.py` and the policy. Measure on the dev App which scopes the caller token needs and record them in `docs/reference/databricks-apps-platform.md`.
- [ ] T188 [B1] The `service_principal` grant type end to end ([ADR 0020](../../docs/decisions/0020-let-a-service-principal-hold-an-app-grant.md)): data model 4.x check constraint and migration, `Principal`/`GrantRequest`/`AppGrant`, the edge and authorizer matching a forwarded application id, `CAN_USE` only, `DELETE /apps/{app}/grants/service_principal/{id}`, the examples and both schema sets, data model and design text.
- [ ] T189 [B2] An output ring per app that survives restarts and failed starts, and `GET /apps/{app}/logs` with the masking of `build_log_tail`, the cursor and the throttled audit row; the `appmux`-stream lines for start, exit code and signal.
- [ ] T190 [B2] Start-failure diagnostics: the health wait ends when the process exits; `reason` names the exit code or signal; `build_log_tail` gets the child's last 50 lines; `failure_hint` for a budget below the runtime's need (Node: 768 MB). Reproduce first with a Node child at 64 MB on the container, then fix the cause of the empty log tail, not only the message.
- [ ] T191 [B2] `GET /apps/{app}/environment`: runtime versions, variables (config, resources by key, platform), packages (release, wheels, platform), secrets never returned.
- [ ] T192 [B3] Wire TD-016 (1): `Service` gives `MetricsService` a real platform sample (cgroup memory current and limit, the edge's RSS, apps running and registered) and real child probes (RSS, pump stats), calls the restart, admission-rejection and registry-call counters, and counts a request that matches no app. Observe on the dev App that the `_platform` row and the children's memory appear; update TD-016.
- [ ] T193 [B3] `step` on `GET /metrics`, windows up to 7 days at at most 1440 steps, and `GET /metrics/breakdown`; examples and both schema sets.
- [ ] T194 [B4] `GET /workspace/browse` with `markers`, `looks_like_app`, `runtime_hint` and `suggested_memory_mb`, tested against a `MockTransport` and once on the dev App.
- [ ] T195 [B4] `GET /resources/types` and `GET /resources/suggestions` for the types that have a safe list call; a type without one answers `not_supported`; tested against a `MockTransport` and once on the dev App.

### Foundation (lanes F0a, F0b, then F0c, before the page lanes)

- [ ] T196 [F0a] [ADR 0019](../../docs/decisions/0019-give-the-admin-ui-its-own-identity-and-a-theme-switch.md): tokens, radius scale, IBM Plex Sans and Mono self-hosted and measured under the reference CSP, the route strip and the lamp, `chrome-*` removed, the contrast test updated; every page still builds.
- [ ] T197 [F0c] The theme switch (Light, Dark, System): `data-theme` set by a same-origin script in the head, no inline script, no flash; the dark variant and the contrast test follow `data-theme` and the OS.
- [x] T198 [F0b] CSP-safe shadcn primitives: Popover, Select (on Popover and Command), DropdownMenu (`modal={false}`), Command and a combobox, Tabs, Tooltip, Collapsible, ToggleGroup; a sample of each in `tests/csp/rendered-markup.test.tsx` and each opened in Chromium under the reference CSP in `e2e/csp.spec.ts` with no violation; the native select kept only where its comment says why.
- [ ] T199 [F0a] Motion: the spinner, the starting lamp, the skeleton, menus and the bar between tabs behave as the table in the design doc for both values of `prefers-reduced-motion`; the blanket 0.01 ms rule is removed; an e2e test with each `reducedMotion` value asserts the running animations.
- [ ] T200 [F0a, F0c] The shells: top bar with breadcrumb and app switcher, the sidebar (collapse, phone menu, one geometry for every bar), the inset panel, page header, skeleton parts, the `RouteStrip`, `Lamp`, `SectionPanel` and `CopyButton` components.

### Pages (lanes F1 to F4, after F0, B1 to B4 for their routes)

- [ ] T201 [F1] Launcher, the app shell and its Overview, Deployments (releases, row menu, failure reason and output), Logs and Environment sections; the node-at-64-MB case shows the reason and the hint.
- [ ] T202 [F2] Authorization (the principal combobox, level select, service principals, identity delegation), Resources (the Databricks add flow with suggestions) and Requests sections.
- [ ] T203 [F3] The Platform sidebar areas, the register and edit forms with the folder browser, the app's Security and Settings sections.
- [ ] T204 [F4] Dashboard and Insights: the figures strip, the platform view with the same charts as an app, the breakdown by app, the ranges up to 7 days, skeletons.

### Integration

- [ ] T205 Whole-branch review by a fresh agent, the Python gate and the seven UI checks, the deploy to the dev App and a hand run of all eleven points with screenshots before and after, docs (`ARCHITECTURE.md`, `web/README.md`, design 10.1, data model, `docs/index.md`), ADRs 0019 and 0020 set to accepted or left proposed with the owner's choices named.

---

## Dependencies and execution order

### Phase dependencies

- **Setup (Phase 1)**: no dependencies; start at once.
- **Foundational (Phase 2)**: needs Setup; blocks every story.
- **US4 (Phase 3)**: needs Foundational only. Pure logic; fast.
- **US3 (Phase 4)**: needs Foundational. It does not need US4 to compile, but its `POST /platform/self-check` route is checked through `authorize` from US4, so do US4 first.
- **US1 (Phase 5)**: needs US4 (authorize in the pipeline), US3 (the sandbox is the only way to start a child, FR-011) and Foundational.
- **US2 (Phase 6)**: needs US1 (it extends the reconciler, the release routes and the edge).
- **US5 (Phase 7)**: needs US1 (releases exist); touches `update.py` from US1 and `activate.py` from US2.
- **US6 (Phase 8)**: needs US1 (`appyaml`, the release path) and US3 (`sandbox/environment.py`).
- **US7 (Phase 9)**: needs US1 and US3 (the overlay is built inside the sandbox); independent of US5 and US6.
- **US8 (Phase 10)**: needs US4 and US1; the rename cascade needs US2.
- **US9 (Phase 11)**: needs US4 and US1; the viewer needs releases from US1.
- **US10 (Phase 12)**: needs US1; the swap overlap check needs US2.
- **US11 (Phase 13)**: needs the routes of every story it shows; the backend half can start after US4 and US1, the UI after the API of the stories it renders.
- **Polish (Phase 14)**: needs every story that is in scope.

### Within each story

- Tests and fixtures first, and each test must fail for the right reason before its code is written.
- Pure modules before the modules that call them; registry functions before the admin routes that use them; integration tests last, because they need a deploy.
- A story's documentation task is the last task of its phase.

### Parallel opportunities

- Setup: the `.gitignore`, module-boundary test and licence test tasks.
- Foundational: settings tests, the two migration files, write-identity tests, snapshot tests, the actor, and the four deploy-script tasks touch different files.
- Inside a story: every `[P]` test task and every `[P]` fixture task; different fixtures are different directories.
- Across stories, after Foundational: US4 and US3 can run in parallel by two people (US3's gate test needs `authorize` only at the end). After US1: US5, US6, US7, US9 and US10 touch different modules and can run in parallel; US8 waits for US2's rename cascade; US2 itself can run beside them if `reconciler/` edits are coordinated (`update.py` and `activate.py` are shared by US2, US5, US6 and US10).

### Parallel example: User Story 3

```bash
# Tests for User Story 3, together (all different files):
Task: "Write tests/unit/test_egress_rules.py"
Task: "Write tests/unit/test_self_check_model.py"
Task: "Write tests/unit/test_child_environment.py"
Task: "Write tests/unit/test_sandbox_spec.py"

# Fixtures for User Story 3, together:
Task: "Create tests/fixtures/env_snooper/"
Task: "Create tests/fixtures/pkg_env_writer/"
Task: "Create tests/fixtures/network_scanner/"
Task: "Create tests/fixtures/fork_bomb/"
```

---

## Implementation strategy

### MVP first

1. Phase 1 Setup, then Phase 2 Foundational.
2. Phase 3 (US4): authorization, pure and fast.
3. Phase 4 (US3): the sandbox and `self_check` on the container.
4. Phase 5 (US1): the edge, the capacity run, then the release path.
5. **Stop and validate**: US1's independent test and SC-002 on the container. This is the smallest slice that hosts a child, opens its route only after health and refuses to run when the sandbox is weakened.

### Incremental delivery

1. MVP above, then US2 (SC-001), then US5 to US10 in any order that suits the owner (US7 if the package index is blocked, US6 if the first real app needs a secret, US10 before more than a few apps run).
2. US11 last: every earlier story is testable through the API, so the UI carries finished behaviour to people.
3. Polish, then `/speckit-converge`.

### Order of risk

The capacity run in Phase 5 and `self_check` in Phase 4 are the two places where a result could change the design ([research D15](research.md#d15-order-of-proof)). If either fails, stop and write the finding down before building on it.

---

## Notes

- `[P]` tasks touch different files; the shared files `src/appmux/reconciler/update.py`, `src/appmux/reconciler/activate.py`, `src/appmux/admin/releases.py`, `src/appmux/admin/apps.py` and `ARCHITECTURE.md` are edited by several stories, so those edits are never `[P]` with each other.
- Assumptions A1 to A5 of the spec are unanswered; their defaults stand. A change to one of them is a change to this file.
- Plan choices the owner may reverse: `404` for a denied child route, no Node offline cache in v1, CPU pin on by default, the caps (120 s, 100 MB, 64 requests), environment settings as a `BaseModel` instead of `pydantic-settings`. Each has a named test above, so a reversal is a small change to a task and one test.
- Commit after each task or logical group once the owner asks for the first commit (the repository is under git, with no commit yet).
- Stop at any checkpoint to validate the story on its own.
