# Feature Specification: appmux v1

**Feature Branch**: `001-appmux-v1` (the spec directory name; the repository was put under git on 2026-10-30 with the remote `timezlab/databricks-appmux`, and no feature branch exists yet)

**Created**: 2026-10-30

**Status**: The owner approved the [Spikes](#spikes-for-owner-approval) on 2026-10-30 and dropped spike 3 (no paid workspace to use); the spikes run on the Free Edition workspace only and their results are in the [spikes log](spikes/README.md). The owner answered question 5 (a) the same day, so ADR 0004 is `accepted`, and the [plan](plan.md) with its research, contracts and quickstart exists. The [tasks](tasks.md) were generated on 2026-10-30, and the owner asked for the implementation the same day; `tasks.md` records which tasks are done.

**Input**: User description: "appmux v1 — a reverse proxy that runs as one Databricks App and hosts many child Databricks Apps in that single slot (up to ~100 registered), adding or updating a child without interrupting the others, treating every child as potentially malicious." The behaviour was designed and approved by the owner on 2026-10-30; this spec links to it and does not repeat it.

## Where the behaviour is specified

This spec says **what must be true and how it is judged**. The full behaviour lives once, in these documents; when a requirement below cites one, the cited text is the detail.

| Need | Document |
|---|---|
| Behaviour, part by part | [v1-design.md](../../docs/product/v1-design.md) (cited below as *design §n*) |
| The nine registry tables | [v1-data-model.md](../../docs/product/v1-data-model.md) |
| Constraints, alternatives, success criteria, spikes | [v1-design-brief.md](../../docs/product/v1-design-brief.md) |
| Why each technical choice was made | ADRs [0003](../../docs/decisions/0003-isolate-children-with-unprivileged-namespaces-and-a-filtering-pump.md) to [0013](../../docs/decisions/0013-license-appmux-under-apache-2-0.md) |
| Standing principles | [constitution](../../.specify/memory/constitution.md) (I child apps stay standard, II health-gated routes, III non-disruptive updates) and the five design premises in the brief |

## Settled decisions and open questions

**Settled** (owner, 2026-10-30; recorded as ADRs). All are `accepted`: 0004 on 2026-10-30 when the owner answered question 5 (a), and 0007 the same day after spikes 18, 13 and 14. The plan adds [ADR 0014](../../docs/decisions/0014-build-the-edge-on-the-environments-http-stack.md).

| ADR | Decision | Status | Spikes that could change it (all run) |
|---|---|---|---|
| [0003](../../docs/decisions/0003-isolate-children-with-unprivileged-namespaces-and-a-filtering-pump.md) | Unprivileged namespaces, veth, a filtering frame pump, fail-closed `self_check()` | accepted | 2, 6, 7 |
| [0004](../../docs/decisions/0004-keep-decisions-in-a-nine-table-lakebase-registry-with-split-writers.md) | Nine-table Lakebase registry; platform tables written with the platform admin's own token | **accepted** (spike 9 passed; spike 1 failed as written and the owner accepted its residual, question 5) | 1, 9 |
| [0005](../../docs/decisions/0005-run-releases-from-immutable-snapshots-and-swap-after-health.md) | Immutable snapshots; swap only after the health check | accepted | 4, 15 |
| [0006](../../docs/decisions/0006-route-by-path-prefix-behind-a-routeselector.md) | Path-prefix routing behind a `RouteSelector` | accepted | 5, 8 |
| [0007](../../docs/decisions/0007-resolve-packages-environment-first-with-a-wheel-overlay.md) | Packages resolve environment first, with a hashed wheel overlay | accepted | 13, 14, 18 |
| [0008](../../docs/decisions/0008-follow-the-standalone-app-yaml-contract.md) | Follow the standalone `app.yaml` contract | accepted | 17 |
| [0009](../../docs/decisions/0009-attach-resources-with-the-attachers-own-token-and-reference-secrets.md) | Attach resources with the attacher's token; secrets are references | accepted (spikes 11 and 16, Free Edition) | 11, 16 |
| [0010](../../docs/decisions/0010-mirror-databricks-permissions-and-give-platform-admins-implicit-access.md) | `CAN_USE`/`CAN_MANAGE` on Databricks users and groups; platform admins hold `CAN_MANAGE` implicitly | accepted | 12 |
| [0011](../../docs/decisions/0011-gate-credentials-and-platform-fields-behind-a-request-catalogue.md) | Three request kinds; three gates | accepted | 9 (rename) |
| [0012](../../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md) | Python services; Next.js static-export admin UI | accepted | 7, 8, 19 |
| [0013](../../docs/decisions/0013-license-appmux-under-apache-2-0.md) | Apache-2.0 | accepted | 9 (`slirp4netns` licence) |
| [0015](../../docs/decisions/0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md) | The release zip carries `slirp4netns` and its source | accepted 2026-11-01 | 9 (`slirp4netns` licence) |

**Open** (not settled): the owner decisions still open in [Questions for the owner](#questions-for-the-owner) (questions 2 and 5 are answered; A1–A5 are confirmed); and what each spike entry lists as not covered. The [Spikes](#spikes-for-owner-approval) all ran on 2026-10-30 except spike 3, which was dropped.

## User Scenarios & Testing *(mandatory)*

Roles: a **platform admin** (listed in the `platform_admins` setting) runs the slot; an **app admin** (`CAN_MANAGE` on one app) updates and configures that app; a **visitor** reaches appmux through Databricks and may open the apps they hold `CAN_USE` on ([design §1.2](../../docs/product/v1-design.md#12-who-uses-it)).

### User Story 1 - Host a child safely and open it only after it is healthy (Priority: P1)

A platform admin registers an app, an app admin presses Update, and visitors open the child at `/<slug>/`. The route exists only once the new release has been fetched, built, started and has answered its health check ([design §5.2](../../docs/product/v1-design.md#52-register-update-roll-back-and-keep-releases), [§5.5](../../docs/product/v1-design.md#55-health-and-swap)).

**Why this priority**: it is the product. Without it nothing else has a subject, and the health gate is constitution principle II.

**Independent Test**: register one fixture app, press Update, and request `/<slug>/` before and after the health check passes.

**Acceptance Scenarios**:

1. **Given** an app `sales` registered with no release, **When** a visitor with `CAN_USE` requests `/sales/`, **Then** no child receives the request, because no route is open.
2. **Given** an app admin presses Update on a folder they can read, **When** the snapshot, the requirements check, the build and the start finish and the child answers a status below 500 within `health_deadline_s`, **Then** the route opens and the release becomes `active`.
3. **Given** a release whose child never answers a status below 500 within the deadline, **When** the deadline passes, **Then** the release is `failed` with the last error and a log tail, and no route was opened.
4. **Given** the folder cannot be read with the presser's own token, or the token expires during the snapshot, **When** Update runs, **Then** it fails with a message that says why, and nothing changes.
5. **Given** the folder's digest equals a live release's digest, **When** Update runs, **Then** appmux reports "already up to date" and creates no release.

---

### User Story 2 - Update with zero downtime and roll back to any release (Priority: P1)

An app admin updates a running app without interrupting it or any other app, and can return to any live release ([design §5.2](../../docs/product/v1-design.md#52-register-update-roll-back-and-keep-releases), [§5.5](../../docs/product/v1-design.md#55-health-and-swap)).

**Why this priority**: the workspace cap of 100 apps and the fear of breaking a running app are the pains the product exists for (constitution III).

**Independent Test**: hold steady load on app A and run the operations of success criterion SC-001.

**Acceptance Scenarios**:

1. **Given** app A is serving, **When** an app admin updates it with a good release, **Then** the new copy starts beside the old one, the route swaps atomically after the health check, in-flight requests finish within 30 s, then the old copy stops.
2. **Given** app A is serving, **When** an update's release fails its health check, **Then** A keeps serving the old release and the new one is `failed`.
3. **Given** an app has live releases 1 to 5, **When** an app admin activates release 3, **Then** it starts from its stored snapshot, wheels and config, reads no source folder and needs no token.
4. **Given** a rollback target needs a resource that is no longer attached, **When** it is activated, **Then** it fails before it starts and lists the missing keys.
5. **Given** an app admin changes an environment entry, **When** they save, **Then** a new release is created from the same snapshot and goes through the same health-gated swap.
6. **Given** an app crashes, **When** it exceeds the restart backoff (1, 2, 4, 8, 16 s), **Then** only that app's route answers 503 with a status page, and no older release is activated automatically.

---

### User Story 3 - Treat every child as potentially malicious (Priority: P1)

Every child runs in its own sandbox, and appmux refuses to open any route when the sandbox cannot prove itself ([design §6](../../docs/product/v1-design.md#6-isolation-and-network), [§12](../../docs/product/v1-design.md#12-threat-model)).

**Why this priority**: the owner's standing premise is "assume a child is malware"; measured on the platform, a flat supervisor exposes siblings' environment, platform secrets and localhost ports.

**Independent Test**: run the hostile fixtures on a healthy sandbox, then weaken the sandbox on purpose and start appmux again.

**Acceptance Scenarios**:

1. **Given** a healthy sandbox, **When** appmux starts, **Then** `self_check()` passes every check of [design §6.4](../../docs/product/v1-design.md#64-self-check-failures-and-gaps) and routes open one by one as health passes.
2. **Given** any one check fails (for example the `/proc` overmount is skipped), **When** appmux starts, **Then** no route opens and a status page names the failed check.
3. **Given** a hostile fixture, **When** it tries to read another app's environment, the platform's `runtime.cfg`, appmux's port, the container IP or the metadata address, **Then** each attempt fails.
4. **Given** a fixture that writes to the shared package environment or an overlay, **When** it tries, **Then** it receives a read-only filesystem error.
5. **Given** an app with egress rules, **When** its child connects to a denied destination, **Then** the connection is dropped and counted against the rule.

---

### User Story 4 - Give access through Databricks users and groups (Priority: P1)

Who may open or manage an app is decided from the Databricks users and groups that already exist, with two levels and no deny entries ([design §9.1](../../docs/product/v1-design.md#91-the-model), [§9.2](../../docs/product/v1-design.md#92-who-may-do-what)).

**Why this priority**: every request passes through authorization; a wrong default here exposes every app.

**Independent Test**: run the table in [design §9.2](../../docs/product/v1-design.md#92-who-may-do-what) against `authorize()` with a stubbed Databricks lookup.

**Acceptance Scenarios**:

1. **Given** Bob belongs to the Databricks group `sales-team`, which holds `CAN_USE` on `sales`, **When** Bob requests `/sales/`, **Then** it is allowed, and the launcher lists only the apps Bob may open.
2. **Given** Mallory has no grant, **When** Mallory requests `/sales/`, **Then** it is denied.
3. **Given** a person's group lookup fails and nothing is cached, **When** they open a group-shared app, **Then** it is denied.
4. **Given** a grant row is removed, **When** its holder next requests the app, **Then** it is denied within seconds; a group removal takes effect within the 300 s cache; open WebSockets of that person close on the same refresh.
5. **Given** user authorization is off for appmux, **When** a person requests an app, **Then** only `user` and `everyone` grants can match.
6. **Given** an app is not OBO-delegated, **When** a request is forwarded, **Then** `X-Forwarded-Access-Token` is removed and any client-supplied identity header is replaced.

---

### User Story 5 - Manage releases: name, tag, lock, delete, caps (Priority: P2)

An app admin keeps the versions that matter by naming, tagging, locking and deleting, within caps a platform admin sets ([design §5.2](../../docs/product/v1-design.md#52-register-update-roll-back-and-keep-releases)).

**Why this priority**: rollback to any release is only useful if the right ones survive the cap.

**Independent Test**: drive an app past `max_releases_per_app` with some releases locked.

**Acceptance Scenarios**:

1. **Given** a release is active, **When** an app admin tries to delete it, **Then** it is refused; the active release carries a lock with no action.
2. **Given** an app already holds `max_locked_releases` manual locks, **When** an app admin locks another, **Then** it is refused with `app 'sales' already has 3 locked releases; unlock one first`.
3. **Given** an app is at its release cap and an unprotected release exists, **When** an Update is accepted, **Then** the oldest unprotected release is deleted and `release.delete` is recorded by `appmux`.
4. **Given** every release is protected, **When** an Update is accepted, **Then** it is refused with a message naming how to free one.
5. **Given** a release has a tag and no lock, **When** it stops being active, **Then** it may be deleted; tags protect nothing. A deleted release's number is never reused and it cannot be activated.

---

### User Story 6 - Declare resources and secrets the way a standalone app does (Priority: P2)

A child brings its own `app.yaml` (and optionally a `databricks.yml`); an app admin attaches the resources it names, each checked with their own token; secrets are references, not typed values ([design §5.4](../../docs/product/v1-design.md#54-appyaml-requirements-and-start), [§8.2](../../docs/product/v1-design.md#82-resources-and-env-entries), [§8.3](../../docs/product/v1-design.md#83-secrets-where-the-value-comes-from)).

**Why this priority**: constitution I: the child needs no change to run on appmux.

**Independent Test**: use the two `app.yaml` fixtures and the `databricks.yml` fixture of [design §14](../../docs/product/v1-design.md#14-testing).

**Acceptance Scenarios**:

1. **Given** the snapshot's `app.yaml` uses `valueFrom` keys that are not resources of the app, **When** the release is reconciled, **Then** it fails before any build, listing every missing key at once; after the keys are attached and the release is activated, it starts with no source read.
2. **Given** the snapshot has a `databricks.yml`, **When** an Update completes, **Then** its resources are shown as offers and nothing is attached until the app admin confirms each one.
3. **Given** an app admin attaches a resource, **When** their own token lacks access to it, **Then** the attach is refused and nothing is written.
4. **Given** a `secret` resource is a reference `scope/key`, **When** the child starts, **Then** the value reaches only that child's environment and is never logged or stored; an unreadable secret fails the release naming the scope.
5. **Given** both `app.yaml` and `app.yml`, or an `env` entry with both or neither of `value` and `valueFrom`, **When** Update runs, **Then** it is refused, naming the problem.

---

### User Story 7 - Build with no package index (Priority: P2)

Packages come first from the environment the platform already provides, then from hashed wheel files in workspace folders, because the company index is blocked ([design §5.3](../../docs/product/v1-design.md#53-build-and-package-sources)).

**Why this priority**: where the index is blocked, an app that needs one extra package cannot start otherwise.

**Independent Test**: the four offline-install fixtures of [design §14](../../docs/product/v1-design.md#14-testing), run with the index unreachable.

**Acceptance Scenarios**:

1. **Given** an app fully covered by the environment, **When** it is built, **Then** no wheel is fetched.
2. **Given** an app needs a newer version of a package the environment has, **When** it is built with a hashed wheel, **Then** the overlay's version wins for that release only.
3. **Given** requirements the environment cannot satisfy and no wheel covers, **When** the Update runs in a wheel mode, **Then** it fails listing every unresolved requirement at once.
4. **Given** a wheel already in the content store by sha256, **When** a second app needs it, **Then** it is not fetched again.
5. **Given** a redeploy changed what the environment provides, **When** a release starts, **Then** it fails before starting if a requirement the environment used to satisfy no longer holds.

---

### User Story 8 - Ask a platform admin through a request inbox (Priority: P2)

A visitor asks for a new app; an app admin asks for OBO or shared-SP delegation, or for a new name, slug, source folder or memory budget; a platform admin answers in an inbox ([design §9.4](../../docs/product/v1-design.md#94-requests-to-a-platform-admin), [§8.1](../../docs/product/v1-design.md#81-the-three-modes-and-delegation-requests)).

**Why this priority**: the gates that keep a malicious or careless child from widening its reach need a record and a reviewer, and no hand-edited database.

**Independent Test**: the request walkthrough of SC-004 and the request rows of the `authorize()` table.

**Acceptance Scenarios**:

1. **Given** Grace asks for a new app with a slug, name, folder, budget and reason, **When** a platform admin approves with a reason, **Then** the app exists, owned by Grace, stopped, with no release.
2. **Given** a person already has 3 pending `register` requests, **When** they ask a fourth time, **Then** it is refused.
3. **Given** Carol asks for OBO delegation on `sales`, **When** a platform admin approves with a reason, **Then** `obo_delegated` is set; a forged `approved` row written by a child changes nothing, because authorization reads only `apps`.
4. **Given** a rename request names a slug that another app took, **When** a platform admin presses Approve, **Then** the request is closed `denied` at once with the note `slug '<slug>' is already taken (automatic)`, recorded under the admin who pressed the button.
5. **Given** a rename is approved for a running app, **When** it is applied, **Then** the app restarts through the normal swap under the new prefix and the old URL answers `404`.
6. **Given** `shared_sp_delegation_allowed` is off, **When** an app admin requests the shared SP, **Then** it is refused.
7. **Given** a request is pending, **When** anyone but its asker tries to withdraw it, **Then** it is refused; egress rules and the owner are a platform admin's direct edit, never a request.

---

### User Story 9 - Platform admin supports any app, with two ways to open it (Priority: P2)

A platform admin holds `CAN_MANAGE` on every app with no row in `app_grants`: they can open it, read the source of its releases in a read-only viewer and edit its configuration. For an OBO-delegated app they have no grant on, they choose between **Open without token** (default) and **Open as a user** ([ADR 0010](../../docs/decisions/0010-mirror-databricks-permissions-and-give-platform-admins-implicit-access.md), [design §9.1](../../docs/product/v1-design.md#91-the-model)).

**Why this priority**: supporting about 100 apps would otherwise need a grant per app or a hand-edited database.

**Independent Test**: the platform-admin rows of the `authorize()` table, plus a check of the headers the child receives in each mode.

**Acceptance Scenarios**:

1. **Given** Dana is a platform admin with no grant on the OBO-delegated `sales`, **When** Dana opens it, **Then** the default mode removes `X-Forwarded-Access-Token` and the app still sees Dana's email.
2. **Given** Dana chooses **Open as a user**, **When** the choice is saved, **Then** an `app.view_as_user` audit row is written and the token passes until Dana switches back; the cookie that holds the choice is removed before the request is forwarded.
3. **Given** an app is not OBO-delegated, **When** Dana opens it, **Then** there is one **Open** and the token is removed.
4. **Given** Dana opens a release's source, **When** the viewer shows the files (text only, read-only), **Then** a `release.view_source` audit row is written; Bob, with `CAN_USE` only, is denied.
5. **Given** Dana edits another team's configuration, **When** they save, **Then** the write is allowed and audited, and no row appears in the app's sharing list.
6. **Given** a change would leave `platform_admins` with no `user` entry, **When** it is submitted, **Then** it is refused.

---

### User Story 10 - Stay inside the slot's memory (Priority: P2)

Each app has a memory budget; appmux refuses to start or update an app that would not fit, instead of letting the container run out of memory ([design §5.7](../../docs/product/v1-design.md#57-memory-and-admission-control)).

**Why this priority**: there are no cgroups, so admission control is the main defence against one app taking the others down.

**Independent Test**: set the budget for 10 simulated apps and try to start an 11th (SC-003).

**Acceptance Scenarios**:

1. **Given** 10 apps whose budgets plus per-child overhead and appmux's reserve fit the limit, **When** an 11th is started, **Then** it is refused with `needs <n> MB, <m> MB free`.
2. **Given** an update needs memory for both copies during the swap, **When** the second copy does not fit, **Then** the Update is refused with the same kind of reason and the running release is untouched.
3. **Given** a child exceeds its budget, **When** the best-effort watchdog fires, **Then** only that app is stopped.

---

### User Story 11 - Run everything from the admin UI and watch per-minute metrics (Priority: P3)

Every action above is available in the admin UI and its JSON API, and each app's traffic, latency, memory and egress drops are shown per minute ([design §10](../../docs/product/v1-design.md#10-admin-ui-and-metrics)).

**Why this priority**: it carries the other stories to the people who use them, and success criterion SC-004 depends on it; the stories above stay testable through the API first.

**Independent Test**: the scripted walkthrough of SC-004.

**Acceptance Scenarios**:

1. **Given** a visitor opens `/`, **When** the launcher loads, **Then** it lists only the apps they may open, with a **Request a new app** button; a platform admin also sees a pending-request count.
2. **Given** an app has traffic, **When** an app admin opens its metrics, **Then** the per-minute counts, latency histogram and memory peak are shown; a percentile is reported as the upper bound of its bucket.
3. **Given** Lakebase is down while appmux runs, **When** requests arrive, **Then** they keep flowing from memory, admin writes fail with a clear error, and metrics show a gap.
4. **Given** the admin UI, **When** any page loads, **Then** it runs no inline script except those the page's CSP allows by SHA-256 hash, and sends no cross-origin request, and every action goes through the JSON API behind `authorize()`.

---

### Edge Cases

Every row of the failure table in [design §11](../../docs/product/v1-design.md#11-failure-behaviour) (27 rows) is an acceptance test of the requirement named there; it is the single source and is not copied here. The cases below are the ones whose outcome decides whether a requirement holds:

| Case | Required outcome | Requirement |
|---|---|---|
| A release fails at any step before the health check | the route table and the running release are unchanged | FR-002 |
| Two releases of one app are healthy at the same time | exactly one is active; the swap is one transaction | FR-004 |
| appmux restarts | a few minutes of full outage is accepted; each running app returns from its active release, routes open one by one | FR-008 |
| `/sales/../hr`, `//hr`, encoded dots, a slug lookalike | normalised and rejected before matching, never routed to another app | FR-017 |
| An app that hard-codes absolute links | its stray request gets `404`; it is documented as not hostable in v1, never misrouted | FR-017 |
| A client sends `X-Forwarded-Email` or another identity header | the edge discards it and sets its own | FR-018 |
| A child sets a cookie with `Path=/` and a `Domain` | the edge forces `Path` to `/<slug>/` and drops `Domain`; the body is untouched | FR-019 |
| A request exceeds the app's concurrency, body-size or timeout cap | `429` or `413` for that app only; other apps are unaffected | FR-020 |
| The source folder is edited after an Update | the running app is unaffected; only a new Update reads the folder | FR-003 |
| A request that grants nothing is forged by a child holding the shared SP | no effect on authorization or the reconciler | FR-045 |
| A renamed app's new copy fails its health check | the old copy and route keep serving; Restart retries | FR-010 |
| `self_check()` fails | no route opens for any app; the status page explains | FR-013 |
| The Lakebase role of a platform admin is missing | the write is refused with `your database role cannot write platform data; ask the Lakebase project owner` | FR-047 |
| The slot's memory is exhausted by a fast allocator | a known gap: container out-of-memory kill; stated, not hidden | FR-050 |
| A fork bomb or a CPU hog | a known gap: no cgroups; stated, not hidden. Spike 7 measured both: processes are bounded per sandbox by `RLIMIT_NPROC`; a CPU hog stalls every app for about 50 ms at a time unless the launcher pins it | FR-050 |

## Requirements *(mandatory)*

Each requirement is testable. The cited document gives the detail and the numbers; a number set "in the plan" is chosen there after the spikes, not left open here.

### Functional Requirements

**Lifecycle and routes**

- **FR-001**: A route MUST open for a release only after its health check passes: a GET on `health_path` (default `/`) that answers a status below 500 within `health_deadline_s` (default 60, 5 to 600). ([ADR 0005](../../docs/decisions/0005-run-releases-from-immutable-snapshots-and-swap-after-health.md), [§5.5](../../docs/product/v1-design.md#55-health-and-swap))
- **FR-002**: A failure at any step before the health check MUST leave the route table and the running release untouched and mark the new release `failed` with its reason and log tail.
- **FR-003**: Update MUST read `source_path` and wheel folders with the OBO token of the person who pressed it, store an immutable snapshot with a digest, and never follow the folder afterwards; a folder that lists no files MUST be refused as `empty, or you cannot read it`, because the workspace API answers a caller with no access with an empty listing or a 404 (spike 15). ([§5.2](../../docs/product/v1-design.md#52-register-update-roll-back-and-keep-releases))
- **FR-004**: The route swap MUST be atomic and, in one transaction, make the new release `active` and the old one `superseded`; in-flight requests MUST get 30 s to finish and WebSockets still open then MUST be closed before the old copy stops.
- **FR-005**: Releases MUST follow `pending → building → starting → healthy → active`, plus `failed` and `superseded`; a `failed` release MUST never be retried automatically and MUST be activatable by hand.
- **FR-006**: Rollback MUST activate any live release from its stored snapshot, wheels and config with no source read and no token, against the current resources.
- **FR-007**: A crashed child MUST restart with backoff (1, 2, 4, 8, 16 s) and then become `crashed`, answering 503 for its own route only, with no automatic fallback to an older release. ([§5.6](../../docs/product/v1-design.md#56-crash-restart-and-shutdown))
- **FR-008**: At startup appmux MUST bind its port first and answer `/` with a "starting" page and every other path `503` until startup finishes (the platform probes `/`); it MUST then run `self_check()`, read the registry, start each running app's active release and open routes one by one as health passes; on `SIGTERM` it MUST stop accepting, flush metrics and stop children in parallel within 15 s.
- **FR-009**: A config change MUST create a new release with the same snapshot and go through the same health-gated swap; resources, secrets and own-SP keys belong to the app and take effect at its next start.
- **FR-010**: Renaming an app MUST restart a running one through the normal swap under the new prefix; the old URL MUST answer `404`; if the new copy fails health, the old copy and route MUST keep serving. ([§5.5](../../docs/product/v1-design.md#55-health-and-swap))

**Isolation**

- **FR-011**: Every child MUST start in its own sandbox (user, mount, net and pid namespaces) and no other code path MAY start a child. ([ADR 0003](../../docs/decisions/0003-isolate-children-with-unprivileged-namespaces-and-a-filtering-pump.md))
- **FR-012**: `self_check()` MUST run at startup and after any sandbox change and MUST test every check of [design §6.4](../../docs/product/v1-design.md#64-self-check-failures-and-gaps) with a canary child.
- **FR-013**: If any check fails, appmux MUST open no route and MUST show a status page naming the failed check.
- **FR-014**: Egress MUST be enforced in the pump: the app's own rules, then the global rules, then allow; only a platform admin edits them, and the editor MUST refuse an `allow` in an app's list that overlaps `@container_ip`, `@appmux_port` or `169.254.0.0/16`. ([§6.3](../../docs/product/v1-design.md#63-egress-rules))
- **FR-015**: A child MUST receive only the environment of [design §5.4](../../docs/product/v1-design.md#54-appyaml-requirements-and-start) (the `app.yaml` values, the release `env`, then appmux's variables, which cannot be overridden) and never appmux's own environment or another app's.
- **FR-016**: The shared package environment and every overlay MUST be read-only inside children.

**Edge and routing**

- **FR-017**: Routing MUST be by path prefix behind a `RouteSelector`; `/_appmux/` is reserved; a path matching no slug MUST get `404`; the routing function MUST normalise and reject ambiguous paths before matching. ([ADR 0006](../../docs/decisions/0006-route-by-path-prefix-behind-a-routeselector.md), [§7](../../docs/product/v1-design.md#7-request-path-and-routing))
- **FR-018**: The edge MUST remove client-supplied identity headers, forward the visitor's email and username, add `X-Forwarded-Prefix`, and remove `X-Forwarded-Access-Token` unless the app is OBO-delegated and the visitor is its owner or named by a grant, or is a platform admin who chose **Open as a user**.
- **FR-019**: The edge MUST rewrite `Location` and `Set-Cookie` (`Path` forced to `/<slug>/`, `Domain` dropped) and MUST NOT rewrite response bodies.
- **FR-020**: Each app MUST have concurrency, body-size and timeout caps, answering 429 or 413 when exceeded (numbers set in the plan).
- **FR-021**: The admin UI MUST be served with `frame-ancestors 'none'`, `Cross-Origin-Opener-Policy: same-origin`, no CORS, `Sec-Fetch-Site` checks and a strict CSP (`script-src 'self'` plus the SHA-256 of each page's inline scripts; spike 19 showed a Next.js export does not start under `script-src 'self'` alone). ([§7.3](../../docs/product/v1-design.md#73-same-origin-limit-accepted-in-v1))

**Access**

- **FR-022**: Permissions MUST have two levels, `CAN_USE` and `CAN_MANAGE` (which includes `CAN_USE`), granted to a `user`, a `group` or `everyone` (`CAN_USE` only), with no deny entry and no inheritance between apps.
- **FR-023**: Every decision MUST go through one table-driven, pure `authorize(actor, action, app)`; the default MUST be deny and a failed group lookup MUST fail closed. ([ADR 0010](../../docs/decisions/0010-mirror-databricks-permissions-and-give-platform-admins-implicit-access.md))
- **FR-024**: A person's groups MUST come from SCIM `GET /Me` called with their own token, cached for 300 s (used for at most one hour after a failure); direct and indirect (nested) memberships both count (spike 12, 2026-10-30), and a change can take up to about 75 s beyond the TTL to show.
- **FR-025**: An app's owner MUST hold `CAN_MANAGE` on it and every platform admin MUST hold `CAN_MANAGE` on every app; a platform admin's access MUST be computed, never stored in `app_grants`.
- **FR-026**: The first platform admin MUST come from `APPMUX_BOOTSTRAP_ADMINS`, honoured only while `platform_admins` is unset; a change that would leave no `user` entry MUST be refused.
- **FR-027**: A revoked grant MUST take effect on new requests within seconds, a group removal within the cache TTL, and WebSockets of a person who lost access MUST close on the same refresh.
- **FR-028**: For an OBO-delegated app on which a platform admin has no grant, the launcher MUST offer **Open without token** (default) and **Open as a user**; the second MUST write an `app.view_as_user` audit row, and the choice MUST be kept in an edge cookie removed before forwarding.
- **FR-029**: A read-only viewer of a release's snapshot (text files only) MUST be available to `CAN_MANAGE` holders and each view MUST write `release.view_source`.

**Releases**

- **FR-030**: Live releases per app MUST be capped by `max_releases_per_app` (default 10); the active release is locked by default; manual locks are capped by `max_locked_releases` (default 3, at most the release cap minus 4); the target and in-progress releases are protected; tags and labels protect nothing; an Update at the cap MUST delete the oldest unprotected release or be refused; release numbers MUST never be reused. ([§5.2](../../docs/product/v1-design.md#52-register-update-roll-back-and-keep-releases))

**Packages and contract**

- **FR-031**: Package resolution MUST follow environment first, then a hashed wheel into the release's overlay (which wins), then the index only in an index mode; an Update MUST list every unresolved requirement at once; at every start appmux MUST re-check what the environment was providing. ([ADR 0007](../../docs/decisions/0007-resolve-packages-environment-first-with-a-wheel-overlay.md))
- **FR-032**: Builds MUST run inside the sandbox with the app's egress rules; `wheelhouse` mode MUST install wheels only and give the build an all-deny egress list.
- **FR-033**: Update MUST parse the snapshot's `app.yaml` (`command`, `env`) per [design §5.4](../../docs/product/v1-design.md#54-appyaml-requirements-and-start): refuse on both file names or an invalid `env` entry, warn on unknown keys, run `command` as an argument list without a shell. ([ADR 0008](../../docs/decisions/0008-follow-the-standalone-app-yaml-contract.md))
- **FR-034**: A `databricks.yml` MUST be read only to offer resources; reading it MUST attach nothing and an invalid file MUST never refuse an Update.
- **FR-035**: The requirements check MUST run before any build and MUST list all missing resource keys at once.

**Credentials and resources**

- **FR-036**: A child MUST have one of three credential modes: OBO and the shared SP only when a platform admin delegated them to that app, or an own SP attached by the app admin; personal access tokens are not supported. ([ADR 0011](../../docs/decisions/0011-gate-credentials-and-platform-fields-behind-a-request-catalogue.md))
- **FR-037**: Attaching a resource MUST run an attach check with the attacher's own token and record who attached it; a type with no verifiable check MUST stay `unverified` and MUST NOT be usable with `shared_sp`; the check proves the attacher can reach the object, and a permission level only where the response reports one. ([ADR 0009](../../docs/decisions/0009-attach-resources-with-the-attachers-own-token-and-reference-secrets.md))
- **FR-038**: A secret MUST be a reference `scope/key` read by the appmux SP at child start and never logged or stored; a start that cannot read it MUST fail naming the scope. The attach check runs as the attacher with the `sql` scope (spike 11).
- **FR-039**: `shared_sp` delegation MUST be refused while `shared_sp_delegation_allowed` is off.

**Requests**

- **FR-040**: `permission_requests` MUST carry exactly three kinds from a catalogue in code: `register` (`slug`, `display_name`, `source_path`; no memory budget: the new app starts with the platform setting `default_memory_budget_mb`), `delegation` (`obo`, `shared_sp`), `settings` (`slug`, `display_name`, `source_path`, `memory_budget_mb`); the gates are `register`/`source_path`, OBO and the shared SP.
- **FR-041**: There MUST be at most one pending request per (app, kind) (for `register`, per proposed slug) and at most 3 pending `register` per person; a reason of up to 500 characters is required; only the asker withdraws, and only while pending.
- **FR-042**: Approval MUST be one transaction under the platform admin's own role that applies every field or none, needs a reason when it holds a gate field, and records both names when asker and approver are the same person.
- **FR-043**: A slug taken at approval MUST close the request as `denied` at once with the automatic note, recorded under the admin who pressed Approve.
- **FR-044**: Deleting an app, the owner and egress rules MUST NOT be requests.
- **FR-045**: A request MUST grant nothing: `authorize()` and the reconciler read only `apps`, never `permission_requests`.

**Registry, audit, capacity**

- **FR-046**: The registry MUST hold decisions and history in the nine tables of the [data model](../../docs/product/v1-data-model.md) and no runtime state; the reconciler rebuilds run state at start.
- **FR-047**: Platform data MUST be written with the platform admin's own OBO identity under the role `appmux_platform_writers`, everything else with the appmux SP; if Lakebase is down, requests MUST keep flowing from memory, admin writes MUST fail with a clear error, and startup MUST fail closed. ([ADR 0004](../../docs/decisions/0004-keep-decisions-in-a-nine-table-lakebase-registry-with-split-writers.md))
- **FR-048**: Every write and every source view MUST produce an `audit_log` row with the actor; an automatic action is marked as such.
- **FR-049**: A write conflict MUST be reported as `changed by someone else, reload and retry`.
- **FR-050**: Admission control MUST refuse to start or update an app when budgets, update overlap, appmux's reserve and per-child overhead exceed the container's limit, stating the amount needed and free; `RLIMIT_AS` MUST be set from the budget and a best-effort watchdog MUST stop only the offending app. ([§5.7](../../docs/product/v1-design.md#57-memory-and-admission-control))

**UI and metrics**

- **FR-051**: The admin UI MUST be a static export served by the edge, polling a JSON API every few seconds, with the screens of [design §10.1](../../docs/product/v1-design.md#101-screens); no Node process runs in the slot. ([ADR 0012](../../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md))
- **FR-052**: Metrics MUST be collected as one-minute aggregates per app, flushed to the registry in one batch per minute and kept 7 days, with the columns of [design §10.2](../../docs/product/v1-design.md#102-metrics).
- **FR-053**: Every action in the SC-004 walkthrough MUST be available in the UI or API, so no SQL is ever typed to operate appmux.

**Licence**

- **FR-054**: The repository root MUST hold the Apache-2.0 text, and no `slirp4netns` binary or source MUST be committed. What the release zip carries is a distribution, not the repository, and is governed by [ADR 0015](../../docs/decisions/0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md) (`accepted`). ([ADR 0013](../../docs/decisions/0013-license-appmux-under-apache-2-0.md))

### Key Entities

- **App**: a registered child, with a unique slug, a display name, a source folder, a memory budget, an owner, two delegation flags and its egress rules. Platform-controlled fields change only by a platform admin or an approved request.
- **App config**: the app admin's settings for one app: the target release, the health check, environment entries and wheel sources.
- **Release**: an immutable snapshot plus a config copy; has a state, an optional label, tags, a lock and a detected manifest.
- **Resource**: a Databricks object an app declares (including a secret reference), with who attached it and the access it was checked with.
- **Grant**: `CAN_USE` or `CAN_MANAGE` for a user, a group or everyone on one app.
- **Permission request**: a `register`, `delegation` or `settings` ask, with a reason, a payload and a status; grants nothing by itself.
- **Platform setting**: a value that applies to all children (for example `platform_admins`, `max_releases_per_app`, `package_mode`, `egress_global`).
- **Metric row**: one app's counters for one minute, plus platform-wide rows.
- **Audit entry**: who did what, when, with the reason and the values before and after.
- **Actor**: the person making a request, with email and Databricks groups, built at the edge.

Columns, keys and sample rows are in the [data model](../../docs/product/v1-data-model.md).

## Success Criteria *(mandatory)*

These four come from the [brief](../../docs/product/v1-design-brief.md#success-criteria). The load rate and the request mix are fixed in the plan from spikes 7 and 8, which measure what the platform allows; the criteria do not change.

### Measurable Outcomes

- **SC-001 No disruption.** With steady load on app A, each of these operations causes no more failed requests than a no-op control run of the same length, and p95 latency before and during is reported: register app B; update B; update A with a good release; update A with a release that fails its health check (A keeps serving and the release is `failed`); a heavy build running in parallel; an egress rule edit.
- **SC-002 Fail-closed isolation.** `self_check()` passes on a healthy sandbox. For each deliberate weakening (skip the `/proc` overmount, skip the capability drop, disable the pump filter) it fails, appmux opens no route and shows the status page. The hostile fixtures read no secret or environment of another app.
- **SC-003 Capacity with 10 simulated apps.** The report gives RSS per app (idle and loaded), per-sandbox overhead, start time and CPU, with the total against the slot limit, and a linear estimate for 100 apps labelled as an estimate. With the budget set for 10 apps, the 11th is refused with a clear reason.
- **SC-004 No hand-edited database.** A scripted walkthrough through the UI or API runs with no SQL typed, and the final database state matches the expectation. It covers: register an app directly; request and approve a new app; request and approve an OBO delegation and a settings change (name and memory budget); rename an app by request, and a rename to a taken slug (which closes automatically); set an egress rule as a platform admin; open another team's app and read its snapshot source as a platform admin; share an app with a user and with a Databricks group; make someone an app admin; attach a resource, including a secret, and accept an offer from `databricks.yml`; update, tag, label, lock, unlock and delete a release; activate an older release; restart; stop.

## Assumptions

**Carried from the approved design** (details in the [brief](../../docs/product/v1-design-brief.md#constraints)):

- Up to about 100 registered apps (soft); how many run at once is not known. One team owns the children; most are FastAPI, plus Streamlit, Dash, Gradio and Node.
- Free Edition is the development environment: at most 3 apps, a 24-hour auto-stop, one Lakebase project ([reference](../../docs/reference/databricks-free-edition.md)). Container measurements exist only for Free Edition and stay labelled so.
- A few minutes of full outage when appmux itself restarts is acceptable.
- Source is a workspace folder; Git is a later addition. Children run on the parent's Python 3.11 and Node 22.
- appmux depends on user authorization being on, the `slirp4netns` binary fetched at install with a checksum, and the operator steps of [design §13.1](../../docs/product/v1-design.md#131-what-the-operator-sets-up).

**Assumptions added in round 8, confirmed by the owner on 2026-10-30** (they were the only design decisions in this spec the owner had not yet stated):

- **A1**: the default view mode for a platform admin is "without token", and the mode is kept in an edge cookie that the edge removes before forwarding ([design §9.1](../../docs/product/v1-design.md#91-the-model)).
- **A2**: the egress editor refuses an `allow` that overlaps `@container_ip`, `@appmux_port` or `169.254.0.0/16`, because `self_check()` assumes those stay closed ([design §6.3](../../docs/product/v1-design.md#63-egress-rules)).
- **A3**: an automatic denial records the admin who pressed Approve as `closed_by` ([design §9.4](../../docs/product/v1-design.md#94-requests-to-a-platform-admin)).
- **A4**: renaming restarts a running app through the normal swap and the old URL answers `404`, with no redirect ([design §5.5](../../docs/product/v1-design.md#55-health-and-swap)).
- **A5**: `ON UPDATE CASCADE` with deferrable composite keys works under the platform admin's role; spike 9 tests it, and if it fails a rename is removed from `settings` for v1.

## Non-Goals

Taken from the [brief's out-of-scope list](../../docs/product/v1-design-brief.md#out-of-scope--anti-goals) and [design §15](../../docs/product/v1-design.md#15-beyond-v1):

- Replacing Databricks Apps, or requiring any change in a child's `app.yaml` or source.
- Routing by anything but the path prefix (`prefix-rescue`, `root`, header, host), and hosting an app that hard-codes absolute links.
- Group managers who are not platform admins (nested groups work: spike 12).
- Requests for deleting an app, for a new owner, for an egress rule or a platform setting; access requests decided by an app admin; time-limited approvals; notifications outside the UI; re-checking an attach after the attacher's access changes; a redirect from a renamed app's old URL.
- Evaluating `${…}` and variables in `databricks.yml`; other Python versions per app; persisted runtime events and durable child logs; versioned secrets and resources per release.
- A pooled service principal; personal access tokens as a child credential.
- An npm offline source for Node children (spike 14 showed it works; the plan decides whether v1 ships it).
- Sleep and start-on-request; sharding across slots; hostname egress rules; IPv6.
- Response-body rewriting; Git as a source; automatic rollback; an audit-log UI; alerts; per-user usage analytics; editing source in the UI; release diff; preview before promotion; canary traffic; blue/green of appmux itself; admin step-up confirmation.
- Any promise to stop data leaving through allowed destinations or DNS; hard CPU or process limits; any guarantee beyond what `self_check()` measures.

## Questions for the owner

Each has a default; the spec proceeds on the default unless the owner says otherwise.

1. **Approve the spikes** below, or trim or reorder them. *Answered 2026-10-30: approved, run on the Free Edition workspace only.*
2. **Confirm or change A1–A5.** *Answered 2026-10-30: all five confirmed as written.*
3. **Spike 3 needs a paid workspace.** *Answered 2026-10-30: the paid workspace is the company's and is not used; spike 3 is dropped and every container measurement stays labelled "Free Edition only".*
4. **Where spike evidence is kept.** *The default was used.* Default: `specs/001-appmux-v1/spikes/README.md` holds one entry per spike (date, command, observation, pass or fail), and each spike's scripts are archived beside it as source only, with sha256 sums and no binaries. The existing probe sources (`appmux-probe`, `appmux-probe2`) are archived the same way as the first step, before the scratch directory is lost.
5. **A visitor's OBO token reaches the registry (spike 1).** Lakebase checks that a token is valid but not its OAuth scopes, so an OBO-delegated child holding a platform admin's token can log in to the registry and write platform data as that admin while the token is valid ([design §4.2](../../docs/product/v1-design.md#42-who-writes-what)). Options: (a) accept and state it, and keep *without token* as a platform admin's default view mode (A1); (b) give up the person-versus-SP separation and let the appmux SP write everything (the old fallback). Recommended: (a), because (b) removes the property that a child holding the SP cannot edit an `apps` row. **Answered 2026-10-30: (a).** [ADR 0004](../../docs/decisions/0004-keep-decisions-in-a-nine-table-lakebase-registry-with-split-writers.md) is `accepted`; the residual is stated in design 4.2 and logged as [TD-002](../../docs/tech-debt.md#td-002-an-obo-childs-token-reaches-the-registry); *without token* stays a platform admin's default view mode (A1).

## Spikes for owner approval

> **Nothing in this section runs until the owner approves it.** Approving means the owner accepts the list, the order, the method, the places and the timeboxes below, or says what to change. After approval each spike runs in the order below, and no application code is written before the gate spikes are recorded.
>
> **Status, 2026-10-30:** the owner approved the section and dropped spike 3 (the paid workspace is the company's and is not used). The other spikes ran on the Free Edition workspace only; the results and the cleanup are in the [spikes log](spikes/README.md). The text below is the approved plan, kept as written.

A **spike** is a bounded experiment that answers one question the docs cannot. It measures; it builds nothing that ships.

### Rules that apply to every spike

- **Where.** Only the owner's Free Edition workspace, where the owner authorised anything on 2026-10-30: the deployed `appmux-probe` App, the workspace's one Lakebase project, a throwaway secret scope, throwaway groups and roles. Free Edition allows 3 apps and stops each after 24 hours, so at most two other throwaway apps exist at once and a stopped app is restarted before a run. **Any other workspace needs the owner's explicit yes first.** "Local" means this development machine; AppArmor blocks `unshare` here, so no sandbox test runs locally.
- **Names and cleanup.** Throwaway objects are named `appmux-spike-<n>-…` and removed when the spike ends; the result entry lists what was created and removed.
- **Secrets.** No credential is typed on a command line, written to a file in the repository or printed. Tokens are read from the environment inside the process that needs them, and logs record only that a call succeeded or failed. A secret value generated for a test is piped, never displayed.
- **Packages.** Any package installed for a spike is checked against OSV.dev and the exact name verified first ([security rules](../../.agents/rules/common/security.md)); installs happen in a throwaway virtual environment.
- **Pass and fail.** A spike **passes** when its pass condition is observed. A fact-finding spike passes when every listed question has a recorded observation *and* the design's assumption holds. If the timebox ends before the pass condition, the partial result is recorded and the spike counts as failed, so its fallback applies unless the owner extends the timebox.
- **Result.** When a spike ends: (1) an entry in `spikes/README.md` as in question 4; (2) a paragraph in `plan.md` under Decisions (decision, why, alternatives) once the plan exists; (3) the status update of the named ADR. A `proposed` ADR becomes `accepted` when its spikes pass. A failed spike that reverses an `accepted` ADR is a new ADR with supersession links, never an edit; the design docs change in the same change.
- **Classes.** **Gate**: the plan cannot fix its design until the result is in. **Adjusts**: the plan has a stated default and the result may change it.

### Order, dependencies and timeboxes

Spikes with no shared dependency may run in parallel, subject to the 3-app limit. The numbers are those of the brief, so ADRs and design links stay valid.

| Run | Spike | Class | Needs first (spike numbers) | Where | Timebox |
|---|---|---|---|---|---|
| 0 | Archive the probe sources | prerequisite | — | local | ½ day |
| 1 | 8 Databricks proxy behaviour | Gate | run 0 | Free Edition app | 1 day |
| 2 | 17 `app.yaml` on the real platform | Adjusts | 8 | Free Edition app | ½ day |
| 3 | 5 Base path per framework | Gate | — | local | 1 day |
| 4 | 2 Filesystem exposure | Gate | run 0 | Free Edition app | 1 day |
| 5 | 18 Package overlay on the container | Gate | 2 | Free Edition app | 1 day |
| 6 | 13 Parent-app wheel bootstrap | Adjusts | — | Free Edition app | ½ day |
| 7 | 14 Node offline source and wheel coverage | Adjusts | 18 | local, Free Edition app | 1 day |
| 8 | 1 OBO login to Lakebase | Gate | — | Free Edition app and Lakebase | 1 day |
| 9 | 9 Cascade privileges and the `slirp4netns` licence | Gate | 1 | Lakebase, local | 1 day |
| 10 | 12 Nested groups in SCIM `Me` | Adjusts | 1 | Free Edition app | ½ day |
| 11 | 4 Content-store location | Gate | — | Free Edition app | ½ day |
| 12 | 15 Workspace file read with the OBO token | Gate | 1 | Free Edition app | 1 day |
| 13 | 11 Secrets API from an App | Gate | — | Free Edition app, secret scope | 1 day |
| 14 | 10 Shared-SP token refresh | Adjusts | 11, 2 | Free Edition app | 1 day (includes a 70-minute wait) |
| 15 | 16 Per-type attach checks | Adjusts | 1, 15 | Free Edition app | 2 days |
| 16 | 6 Egress mechanics | Adjusts | 2 | Free Edition app | 1 day |
| 17 | 7 Throughput, hogs and compiler builds | Adjusts | 2, 6, 8 | Free Edition app | 2 days |
| 18 | 19 Next.js admin UI | Adjusts | 8 | local, Free Edition app | 1 day |
| 19 | 3 Paid workspace | Adjusts | 2, 6, 8 | a paid workspace, **owner's yes first** | 1 day, only if question 3 is answered yes |

**Blocks the plan** (Gate spikes): 1, 2, 4, 5, 8, 9, 11, 15, 18. The plan is not written until these are recorded. **Adjusts** spikes (3, 6, 7, 10, 12, 13, 14, 16, 17, 19) may finish while the plan is written; a result that changes a plan section reopens that section only. Total about 19½ days of work with spike 3 and 18½ without, if run one after another; the parallel runs allowed above shorten the calendar time, not the work.

### Spike 8 — Behaviour of the Databricks proxy (Gate)

- **Question.** Does the proxy forward arbitrary sub-paths; carry WebSocket and SSE; apply idle and total timeouts; limit body and header sizes; pass `Set-Cookie` unchanged; overwrite client-supplied `X-Forwarded-*` headers; speak HTTP/1.1 or H2C to the app; and accept the CLI's OAuth token as a bearer (how load is generated)?
- **Why, and what it gates.** [ADR 0006](../../docs/decisions/0006-route-by-path-prefix-behind-a-routeselector.md) (path routing), [ADR 0012](../../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md) (H2C), FR-017 to FR-020, the trust in identity headers ([design §7.1](../../docs/product/v1-design.md#71-flow) step 2), and how SC-001 load is generated.
- **Method.** On Free Edition, redeploy `appmux-probe` as a standard-library echo server that logs method, raw path, headers, HTTP version and body length, with an SSE endpoint, a WebSocket endpoint, a sleep endpoint and large-body sinks. Drive it with `curl` using the CLI's OAuth bearer, and from a browser for cookies. Cases: `/a/b/../c`, `//x`, trailing slash; a client-sent `X-Forwarded-Email`; idle 30, 60, 120 and 300 s; responses of 1, 10 and 100 MB; request headers of 8, 16 and 64 KB; `Set-Cookie` with `Domain` and `Path`; an H2C prior-knowledge request.
- **Pass.** Every question has a recorded answer with the observed number, and sub-paths are forwarded with the path intact and a client-supplied `X-Forwarded-Email` is overwritten.
- **If not.** Sub-paths not forwarded: path-prefix routing cannot work, [ADR 0006](../../docs/decisions/0006-route-by-path-prefix-behind-a-routeselector.md) is superseded and the plan returns to the owner. Identity header not overwritten: the edge validates the visitor's token (`GET /Me`) instead of trusting the header. No bearer accepted: load is generated from a browser-driven script or from inside the container.
- **Record.** ADR 0006 and ADR 0012 `Revisit if` sections confirmed or changed.

### Spike 17 — `app.yaml` on the real platform (Adjusts)

- **Question.** Is `$DATABRICKS_APP_PORT` substituted inside a `command` argument, and with which syntax; what does Databricks do for a `valueFrom` key with no resource; which variables does a real App receive?
- **Why.** [ADR 0008](../../docs/decisions/0008-follow-the-standalone-app-yaml-contract.md), FR-015, FR-033; the "differs from a standalone App" table in [design §5.4](../../docs/product/v1-design.md#54-appyaml-requirements-and-start).
- **Method.** On Free Edition, deploy `appmux-probe` variants: (a) `command` with `$DATABRICKS_APP_PORT`, (b) the same with `${DATABRICKS_APP_PORT}`, (c) an `env` entry with `valueFrom` naming no resource. In each, print the received variable names (values of names that look secret are redacted).
- **Pass.** The substitution syntax, the missing-key behaviour and the variable list are recorded and compared with the design's list.
- **If not.** Nothing blocks: appmux keeps its own rule (replace `$NAME` and `${NAME}` per argument, no shell) and the "differs" table is corrected.
- **Record.** ADR 0008 `Must now be true` and the design table updated if they differ.

### Spike 5 — Base path per framework (Gate)

- **Question.** For FastAPI (uvicorn `root_path`), Streamlit (`server.baseUrlPath`), Dash (`requests_pathname_prefix`), Gradio (`root_path`) and a Node/Express app, which environment variable or flag makes unmodified code emit `/<slug>/…` links, and does a page, an asset, an API call and a WebSocket work through the prefix?
- **Why, and what it gates.** It decides which apps v1 can host at all ([design §7.2](../../docs/product/v1-design.md#72-the-selector-interface-and-what-v1-ships)); gates [ADR 0006](../../docs/decisions/0006-route-by-path-prefix-behind-a-routeselector.md) and FR-017.
- **Method.** Local. Put each framework's smallest app behind a 50-line standard-library proxy that serves `/sales/x` as `/x`, adds `X-Forwarded-Prefix`, and counts requests that arrive outside `/sales/`. Load the front page in a headless browser (a vetted package in a throwaway environment) and follow its assets, one API call and one WebSocket.
- **Pass.** For each framework, a recorded row: the setting that works, or "not hostable in v1".
- **If not.** The list of hostable frameworks in the README shrinks; if FastAPI itself fails, the plan returns to the owner. `prefix-rescue` moves from beyond-v1 to a candidate.
- **Record.** ADR 0006 Consequences gain the hostable list.

### Spike 2 — Filesystem exposure beyond `/proc`, `/tmp` and runtime-secrets (Gate)

- **Question.** Which paths can a sandboxed child still read or write: the app directory, home, `/dev/shm`, `/mnt`, `/databricks`, `/sys`, another sandbox's files or sockets?
- **Why, and what it gates.** The mount allow-list; [ADR 0003](../../docs/decisions/0003-isolate-children-with-unprivileged-namespaces-and-a-filtering-pump.md), FR-011, FR-012, FR-016.
- **Method.** On Free Edition, run the probe's sandbox prototype from inside `appmux-probe`. From the child, walk `/`, and attempt a read and a write on each candidate path and on a sibling's socket and files; record what each attempt returns.
- **Pass.** A written mount table exists, and with it applied a canary reads nothing outside it except named, harmless paths (system libraries).
- **If not.** A path that cannot be hidden and holds a secret blocks the sandbox design and returns to the owner; otherwise it is listed as a known gap in [design §6.4](../../docs/product/v1-design.md#64-self-check-failures-and-gaps).
- **Record.** ADR 0003 gains the mount table; `self_check()` gains a check per path that mattered.

### Spike 18 — Package overlay on the real container (Gate)

- **Question.** Does `pip install --no-index --no-deps --require-hashes --target` into an overlay work; does `PYTHONPATH` ahead of the environment let an overlay version win; do entry-point scripts run from the overlay's `bin`; does the read-only remount of the environment and overlays hold in the sandbox?
- **Why, and what it gates.** [ADR 0007](../../docs/decisions/0007-resolve-packages-environment-first-with-a-wheel-overlay.md), FR-016, FR-031, FR-032.
- **Method.** On Free Edition, in the spike 2 sandbox: read the App's Environment tab and record its packages; `pip download` the wheels of a small FastAPI app; run the install from [design §5.3](../../docs/product/v1-design.md#53-build-and-package-sources) into an overlay; start `uvicorn` from the overlay's `bin` with the `PYTHONPATH`; use a wheel of a package the environment also has at another version; then try to create a file in the environment and in the overlay.
- **Pass.** The app serves, the overlay's version is the one imported, and both writes return a read-only filesystem error.
- **If not.** A per-release virtual environment built from the system interpreter, or vendoring every package; [ADR 0007](../../docs/decisions/0007-resolve-packages-environment-first-with-a-wheel-overlay.md) is revised.
- **Record.** ADR 0007 `proposed` → `accepted` when spikes 18, 13 and 14 do not reverse it; the Environment tab's package list is stored with the evidence.

### Spike 13 — Parent-app wheel bootstrap (Adjusts)

- **Question.** Does the platform honour `--no-index` and `--find-links` lines in an App's `requirements.txt`?
- **Why.** Whether appmux needs the standard-library bootstrap of [design §13.1](../../docs/product/v1-design.md#131-what-the-operator-sets-up) step 5; [ADR 0007](../../docs/decisions/0007-resolve-packages-environment-first-with-a-wheel-overlay.md).
- **Method.** On Free Edition, deploy a throwaway app whose `requirements.txt` starts with `--no-index` and `--find-links ./wheels`, with the wheels in the deployment folder; read the deployment log. Repeat with the bootstrap start script that installs from a workspace folder into `/tmp`.
- **Pass.** The result is recorded; either the platform honours the lines or the bootstrap installs and starts the app.
- **If not.** The bootstrap is the designed fallback; if neither works, appmux's dependencies must be vendored into the source and the plan returns to the owner.
- **Record.** A note in ADR 0007; [design §13.1](../../docs/product/v1-design.md#131-what-the-operator-sets-up) keeps the working variant only.

### Spike 14 — Node offline source and wheel coverage (Adjusts)

- **Question.** Can a Node child be built from an npm cache or tarballs with no index; and for the well-behaved fixtures, which packages have no `manylinux` Python 3.11 wheel and which does the environment already provide?
- **Why.** [ADR 0007](../../docs/decisions/0007-resolve-packages-environment-first-with-a-wheel-overlay.md); the non-goal "an npm offline source".
- **Method.** Local and Free Edition. Populate an npm cache on a machine with the index, then run `npm ci --offline` for a sample Express app inside the sandbox. For each fixture (FastAPI, Streamlit, Dash, Gradio), compile hashed requirements and run `pip download --only-binary=:all: --python-version 3.11 --platform manylinux2014_x86_64`; subtract the environment's packages from spike 18.
- **Pass.** A table: per fixture, how many requirements the environment covers, how many need wheels, which have none; and a yes or no for Node offline.
- **If not.** Node children stay out of scope for wheelhouse mode and need an index mode; the non-goal list says so.
- **Record.** The coverage table goes to the plan; ADR 0007 consequences updated.

### Spike 1 — OBO login to Lakebase (Gate)

- **Question.** Does a Postgres login with a user's OBO token carrying the `postgres` scope succeed, and fail without the scope, and can that role then write a table granted to `appmux_platform_writers`?
- **Why, and what it gates.** The platform-admin write path of [ADR 0004](../../docs/decisions/0004-keep-decisions-in-a-nine-table-lakebase-registry-with-split-writers.md), FR-042, FR-047.
- **Method.** On the Free Edition Lakebase project, create a throwaway OAuth role for the owner's user and the group role. Enable user authorization with the `postgres` scope on `appmux-probe`. A small endpoint takes `X-Forwarded-Access-Token`, connects with the token as the password (a vetted PostgreSQL client), and runs `select current_user` and one `insert` into a throwaway table. Repeat without the scope.
- **Pass.** Login succeeds as the user's role with the scope, fails without it, and the insert succeeds only through the granted group role.
- **If not.** The registry's assumption 1 applies: the shared-SP write rule is dropped and the appmux SP writes everything, so the platform gate loses its person-versus-SP separation; this returns to the owner as an ADR 0004 supersession.
- **Record.** ADR 0004 is `accepted` (spike 9 passed; the owner accepted the residual on 2026-10-30).

### Spike 9 — Cascade privileges on Lakebase and the `slirp4netns` licence (Gate)

- **Question.** Can a platform admin's role `DELETE` from `apps` and have the delete cascade into tables that role cannot write; does `UPDATE apps SET slug` cascade with `ON UPDATE CASCADE` through the deferrable composite keys of `app_config`; does `SET LOCAL ROLE` work; and what licence does `slirp4netns` carry?
- **Why, and what it gates.** [ADR 0004](../../docs/decisions/0004-keep-decisions-in-a-nine-table-lakebase-registry-with-split-writers.md), [ADR 0011](../../docs/decisions/0011-gate-credentials-and-platform-fields-behind-a-request-catalogue.md) (rename), [ADR 0013](../../docs/decisions/0013-license-appmux-under-apache-2-0.md), assumption A5, FR-043, FR-054.
- **Method.** On the Lakebase project from spike 1, in a scratch database, load the DDL of the [data model](../../docs/product/v1-data-model.md) as written (it has not yet been run against a Postgres). As a member of the group role, delete an app and update a slug inside one transaction; as the same role, try a direct write to `app_config` (it must be refused); try `SET LOCAL ROLE`. Read the `LICENSE` file shipped with the `slirp4netns` release and record its SPDX identifier.
- **Pass.** The delete and the rename cascade in one transaction under the admin role, the direct write is refused, and the licence is recorded.
- **If not.** Cascade fails: the platform role gets the minimum extra grants, or deletes and renames update the child tables explicitly in the same transaction; if neither works, a rename is removed from v1's `settings` request (A5). A licence that requires a source offer adds a `NOTICE` file.
- **Record.** ADR 0004 → `accepted` when spikes 1 and 9 pass; ADR 0011 and 0013 gain a note; the DDL in the data model is corrected where the run showed errors.

### Spike 12 — Nested groups in SCIM `Me` (Adjusts)

- **Question.** Does `GET /Me`, called with the visitor's token, list groups reached through nesting or direct memberships only?
- **Why.** [ADR 0010](../../docs/decisions/0010-mirror-databricks-permissions-and-give-platform-admins-implicit-access.md), FR-024.
- **Method.** On Free Edition, create `appmux-spike-12-parent` containing `appmux-spike-12-child` containing the owner's user, then call `/Me` through `appmux-probe` with the user's token. If the workspace cannot create nested groups, record that.
- **Pass.** The response is recorded with the parent present or absent.
- **If not.** Nothing changes: direct memberships only is the default.
- **Record.** ADR 0010 `Revisit if` closed or confirmed.

### Spike 4 — Content-store location (Gate)

- **Question.** Can the appmux service principal write and read snapshots and wheels in its own workspace folder, and up to what file size?
- **Why, and what it gates.** Where the durable content store lives; [ADR 0005](../../docs/decisions/0005-run-releases-from-immutable-snapshots-and-swap-after-health.md), FR-003, FR-006, FR-031.
- **Method.** From `appmux-probe`, with its SP, write, read and delete files of 10, 100 and 500 MB under `/Workspace/Users/<sp id>/appmux-store/` through the workspace API, and in a Unity Catalog volume if the workspace has one. Record latency and the size at which a call is refused.
- **Pass.** Read and write work at the sizes the largest expected wheel needs, and the size limit is recorded.
- **If not.** A Unity Catalog volume, or `/tmp` with re-fetch from the source on demand (which fails the "rollback needs no source read" rule and so returns to the owner).
- **Record.** ADR 0005 Consequences gain the store location and limit.

### Spike 15 — Workspace file read with the presser's OBO token (Gate)

- **Question.** Which OAuth scope lets appmux list and export a workspace folder as the visitor, and does the workspace's scope allowlist include it?
- **Why, and what it gates.** [ADR 0005](../../docs/decisions/0005-run-releases-from-immutable-snapshots-and-swap-after-health.md) and FR-003: the confused-deputy rule that Update reads with the presser's token.
- **Method.** On `appmux-probe` with user authorization on, request `workspace.workspace:read` (the candidate) and list and export a folder the owner can read, then one the owner cannot (a folder owned by the SP); try a scope-less token.
- **Pass.** The first call succeeds, the second returns an access error, and the scope name is recorded.
- **If not.** Assumption 11 applies: the folder owner grants the appmux SP read access to the registered folder; the Update flow and ADR 0005 are revised and the confused-deputy trade-off is stated.
- **Record.** The scope is added to the operator steps of [design §13.1](../../docs/product/v1-design.md#131-what-the-operator-sets-up).

### Spike 11 — Secrets API from an App (Gate)

- **Question.** Can the appmux SP, or a token minted for it, read a secret value from inside an App (`get-secret` is documented as `DBUtils`-only), and does the attach-time key listing work with an OBO token? Can the same SP token read a second scope it holds `READ` on?
- **Why, and what it gates.** "Yes" ships secret references; "no" ships the encrypted store. [ADR 0009](../../docs/decisions/0009-attach-resources-with-the-attachers-own-token-and-reference-secrets.md), FR-038, and the exposure of [design §8.4](../../docs/product/v1-design.md#84-shared-sp-what-it-reaches-and-what-can-be-done) O1.
- **Method.** Create the scope `appmux-spike-11-secrets` with key `k1` (a random value piped in, never printed) and give the app's SP `READ`. From `appmux-probe`, try the REST route and the SDK with the SP token, and list the scope's keys with the owner's OBO token. Create a second scope and test whether the same token reads it.
- **Pass.** The outcome of each call is recorded: if the value is read, the call, the scope needed and the permission model are written down.
- **If not.** The fallback of ADR 0009: typed values in an encrypted store in the registry, a master key attached to the appmux App, and the references dropped.
- **Record.** ADR 0009 stays `proposed` until spike 16 is also recorded; the secret scope is deleted.

### Spike 10 — Shared-SP token refresh (Adjusts)

- **Question.** How does a child that never holds the client secret keep a short-lived (1 hour) SP token fresh with unmodified SDK code, and does the token request accept a scope narrower than `all-apis`?
- **Why.** Constitution I; [design §8.4](../../docs/product/v1-design.md#84-shared-sp-what-it-reaches-and-what-can-be-done) O3; FR-036.
- **Method.** On `appmux-probe`, in a sandbox child: the supervisor mints the token with the SP's client credentials; test (a) a local endpoint the Databricks SDK is pointed at, (b) a token file the child re-reads; run a call every 5 minutes for 70 minutes with an unmodified SDK. Separately, request a token with a narrower `scope` and call a secret read (spike 11).
- **Pass.** The unmodified SDK keeps working across the expiry with (a) or (b), and a scope narrower than `all-apis` is accepted or refused (recorded either way).
- **If not.** The shared SP ships with a static token that expires hourly and that limit is stated, or it is not shipped in v1 and own-SP covers the need; the owner decides.
- **Record.** [Design §8.4](../../docs/product/v1-design.md#84-shared-sp-what-it-reaches-and-what-can-be-done) updated; ADR 0011 consequences noted.

### Spike 16 — Per-type attach checks (Adjusts)

- **Question.** For each of the 14 resource types of the [data model](../../docs/product/v1-data-model.md), which read-only call shows the attacher's access, and which OAuth scope does it need?
- **Why.** [ADR 0009](../../docs/decisions/0009-attach-resources-with-the-attachers-own-token-and-reference-secrets.md), FR-037.
- **Method.** For each type, create the object on Free Edition where the workspace allows it, call the candidate read with the owner's OBO token and expect success, then with the app SP's token (a principal with no access) and expect refusal. Types the workspace cannot create stay unverified.
- **Pass.** A table of type, call, scope and verified or unverified, with no type marked verified without an observed refusal.
- **If not.** An unverified type can be attached with OBO or own SP but not with `shared_sp`, as designed.
- **Record.** The table goes to the data model (resource types) and to [design §8.2](../../docs/product/v1-design.md#82-resources-and-env-entries); ADR 0009 → `accepted` when spikes 11 and 16 are recorded.

### Spike 6 — Egress mechanics (Adjusts)

- **Question.** Do rules reload live for open and new connections; do port rules work; are IP fragments dropped when port rules exist; what happens to IPv6; can a child reach neighbouring containers on the same subnet?
- **Why.** [ADR 0003](../../docs/decisions/0003-isolate-children-with-unprivileged-namespaces-and-a-filtering-pump.md), FR-014, [design §6.3](../../docs/product/v1-design.md#63-egress-rules).
- **Method.** On Free Edition, run the pump prototype with a rule engine stub. Change a rule while a connection is open and for a new connection; send port-filtered TCP and UDP; send fragmented UDP from the sandbox before the capability drop; try IPv6; scan the container's /24 for responders.
- **Pass.** Each behaviour is recorded as designed or as a gap.
- **If not.** Gaps are listed in [design §6.4](../../docs/product/v1-design.md#64-self-check-failures-and-gaps); reachable neighbours add the subnet to the global default deny list.
- **Record.** The design's egress section and the threat-model residuals updated.

### Spike 7 — Throughput, hogs and compiler builds (Adjusts)

- **Question.** What throughput do the Python pump and edge sustain; what do a CPU hog and a bounded fork test do to other apps; do source builds that need a compiler work?
- **Why.** [ADR 0012](../../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md) (revisit if Python cannot carry the pump), the accepted gaps of [design §12](../../docs/product/v1-design.md#12-threat-model), the load rate for SC-001.
- **Method.** On Free Edition, with the spike 6 prototype: a bulk download through the sandbox; 1, 10 and 100 concurrent connections through a trivial child, recording CPU of the 4 visible CPUs; a CPU-hog child; a fork test stopped at a fixed count of processes and ended by tearing down the pid namespace; an `sdist` with a C extension built in the sandbox.
- **Pass.** Numbers are recorded and compared with the load chosen for SC-001.
- **If not.** A faster, non-Python pump ([ADR 0012](../../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md) revisited) or the wheels-only rule for builds.
- **Record.** The plan's load figure for SC-001 is derived from the measured edge capacity.

### Spike 19 — Next.js admin UI on the platform (Adjusts)

- **Question.** Does a Next.js static export work with a strict CSP (no inline script) and a base path behind appmux's prefix; does the platform build a `package.json` in a Python App?
- **Why.** [ADR 0012](../../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md), FR-021, FR-051.
- **Method.** Local: a minimal Next.js app with `output: 'export'` and a build-time base path, one page polling a JSON endpoint; serve the export from a standard-library server with `script-src 'self'` and look for CSP violations in a headless browser. Free Edition: deploy the export in `appmux-probe` behind the platform proxy; deploy a Python app with a `package.json` and read the build log.
- **Pass.** The page renders and polls with zero CSP violations behind the base path, and the platform's build behaviour is recorded.
- **If not.** Hash-based `script-src` computed at build; failing that, a server-rendered UI from the Python edge (the alternative recorded in ADR 0012), which the owner decides.
- **Record.** ADR 0012 `Must now be true` updated with the working CSP.

### Spike 3 — Do these measurements hold on a paid workspace? (Adjusts)

- **Question.** Do the container facts (uid, `ptrace_scope`, `/dev/net/tun`, memory limit, cgroup and `oom_score_adj` writes, proxy behaviour, egress) differ on a paid workspace?
- **Why.** Every container measurement is Free Edition only ([design §6.4](../../docs/product/v1-design.md#64-self-check-failures-and-gaps)).
- **Method.** Only if the owner answers question 3 yes and names the workspace: re-run the scripts of spikes 2, 6 and 8 in a throwaway app there.
- **Pass.** Differences, if any, are listed beside the Free Edition values.
- **If not.** If there is no access, the spike is dropped and every measured claim stays labelled "Free Edition only"; `self_check()` remains the guard if the platform differs.
- **Record.** [reference/databricks-free-edition.md](../../docs/reference/databricks-free-edition.md) gains the paid-workspace column.
