# appmux v1 — product and system design

**Status:** design approved; the [plan](../../specs/001-appmux-v1/plan.md) fixes the stack and layout. **Nothing described here is implemented**; [ARCHITECTURE.md](../../ARCHITECTURE.md) marks what exists. **Date:** 2026-10-30.

This is the single description of what appmux v1 is and how it behaves. The reasoning behind it — constraints, alternatives that lost, success criteria, unresolved spikes — is in the [design brief](v1-design-brief.md), the input to `/speckit-specify`. Platform facts are in [reference/databricks-apps-platform.md](../reference/databricks-apps-platform.md) and [reference/databricks-free-edition.md](../reference/databricks-free-edition.md). Every table, with columns, sample rows and writer, is in the [data model](v1-data-model.md). Table, column, header and file names below are final for v1; the [plan](../../specs/001-appmux-v1/plan.md) adopts them. Every example uses made-up names (`example.com` emails, an app called `sales`).

Contents: [1 Overview](#1-overview) · [2 Principles](#2-principles) · [3 Architecture](#3-architecture) · [4 Registry](#4-registry) · [5 Child lifecycle](#5-child-lifecycle) · [6 Isolation and network](#6-isolation-and-network) · [7 Request path and routing](#7-request-path-and-routing) · [8 Identities, secrets and resources](#8-identities-secrets-and-resources) · [9 Roles, permissions and config levels](#9-roles-permissions-and-config-levels) · [10 Admin UI and metrics](#10-admin-ui-and-metrics) · [11 Failure behaviour](#11-failure-behaviour) · [12 Threat model](#12-threat-model) · [13 Deployment and operations](#13-deployment-and-operations) · [14 Testing](#14-testing) · [15 Beyond v1](#15-beyond-v1)

## 1. Overview

### 1.1 What appmux is

A Databricks workspace allows at most 100 apps, and each app keeps its own compute running while deployed, so a team runs out of slots. appmux is an open-source reverse proxy that runs as **one** Databricks App and hosts many **child apps** in that slot. A child stays an ordinary Databricks App: it keeps its own `app.yaml`, can still be deployed on its own and is never required to change its source. Joining appmux is a registry entry, and deploying a child follows the rules of a standalone deployment ([5.4](#54-appyaml-requirements-and-start)).

appmux treats every child as **potentially malicious**: children share one container and one OS user id, so each gets its own sandbox, its reach is controlled and its crashes are contained. Safety is balanced against convenience: children keep their normal features (outbound network, dependencies, WebSocket) unless an admin narrows them.

v1 delivers: the edge with path-prefix routing, a registry, a launcher and admin UI, dashboard metrics, RBAC with Databricks users and groups, versioned releases that can be named, tagged, locked, deleted and re-activated at any version, the `app.yaml` contract (and the resources a `databricks.yml` declares) with each app's own resources table and attach check, installs that use the app environment's packages first and add wheels from a content store, in-app requests to a platform admin, an audit log, and a sandbox with a fail-closed self-check. What v1 leaves out is in [15](#15-beyond-v1).

### 1.2 Who uses it

| Role | Who | Does |
|---|---|---|
| **Platform admin** | a person or Databricks group listed in the `platform_admins` setting; not necessarily a workspace admin | registers apps (or approves a visitor's request for one), sets egress rules, answers requests from visitors and app admins, sets platform settings; holds `CAN_MANAGE` on every app, so can open it, read its snapshot source and change its configuration ([9.1](#91-the-model)) |
| **App admin** | a person or Databricks group holding `CAN_MANAGE` on one app; the **owner** is one undeletable app admin | updates and activates releases, names, tags, locks and deletes them, restarts, declares the app's own resources and secrets, manages sharing, sets the health check, asks for delegations and for a new name, slug, source folder or memory budget |
| **User** | anyone holding `CAN_USE` on an app | opens the launcher and the apps they may use; can ask for a new app |

The model copies Databricks Apps: two levels, `CAN_USE` and `CAN_MANAGE`, granted by email or by Databricks group ([9](#9-roles-permissions-and-config-levels)). A Databricks workspace admin appears in no permission path; the one deploy-time touchpoint is the OAuth scope allowlist ([13.1](#131-what-the-operator-sets-up)).

### 1.3 Glossary

| Term | Meaning |
|---|---|
| **Slot** / **Child** | one Databricks App, which appmux occupies / an app hosted by appmux |
| **Snapshot** | an immutable, content-addressed copy of a child's source folder, taken when an app admin presses Update |
| **Release** | a snapshot plus its config; the unit that is built, health-checked, activated and rolled back. The **active** release is the one that last passed health and went live; a restart runs it |
| **Lock** | a mark an app admin puts on a release so that neither a person nor the automatic clean-up can delete it; the active release is protected without one ([5.2](#52-register-update-roll-back-and-keep-releases)) |
| **Content store** | appmux's storage of snapshots and wheel files, named by digest, one copy per distinct file ([5.3](#53-build-and-package-sources)) |
| **Edge** / **Reconciler** | the component that receives and routes every request / the one that drives children from desired to observed state and is the only one that opens routes |
| **Sandbox** / **Pump** | the namespaces and restrictions one child runs in ([6.1](#61-sandbox)) / the per-child process that filters its network frames |
| **Registry** | the Lakebase database of decisions and history ([data model](v1-data-model.md)) |
| **Resource** | something an app declares for itself and names by key in `valueFrom` (a secret, a SQL warehouse, …), with who attached it ([8.2](#82-resources-and-env-entries)) |
| **Delegation** | a platform admin's decision that a credential (OBO or the shared SP) may be passed down to one app; an app admin can **request** it ([8.1](#81-the-three-modes-and-delegation-requests)) |
| **Request** | a person's ask that a platform admin do what only a platform admin may do: register a new app, pass a credential down to an app, or change an app's name, slug, source folder or memory budget ([9.4](#94-requests-to-a-platform-admin)); it grants nothing until approved |
| **OBO** / **Shared SP** / **Own SP** | the visitor's own token / the appmux app's service principal, lent as a short-lived token / a service principal that already exists, attached to one child |

### 1.4 Worked example: the life of one app

The rest of the document uses this app. `sales` is a FastAPI dashboard owned by `alice@example.com`; `dana@example.com` is a platform admin; `bob@example.com` is in the group `sales-team`.

| # | Who | Does | What changes |
|---|---|---|---|
| 1 | Dana | registers `sales`: folder `/Workspace/Shared/apps/sales`, owner Alice, budget 512 MB | rows in `apps` and `app_config`; audit `app.register`. Alice needs read access to the folder: Alice's own token reads it on Update |
| 2 | Alice, Dana | Alice presses **Request** next to `OBO: not allowed`, reason "dashboard queries the SQL warehouse as the viewer". Dana sees `1 pending`, reads it and approves with a reason | a row in `permission_requests` (kind `delegation`, payload `{"obo": true}`); on approval `apps.obo_delegated` is set; audit `request.create`, `request.approve` ([8.1](#81-the-three-modes-and-delegation-requests)) |
| 3 | Alice | presses **Update** | appmux reads the folder with Alice's token, snapshots it, reads its `app.yaml` and creates release 1. The reconciler finds that `openai-api-key`, `sql-warehouse` and `experiment` are not resources of `sales`: release 1 is `failed`, listing all three ([5.4](#54-appyaml-requirements-and-start)) |
| 4 | Alice | attaches the three from the Requirements panel, which offers them pre-filled from the app's `databricks.yml`: the secret as a reference to a Databricks secret, the warehouse as the visitor (OBO) and the experiment as the app's own SP | rows in `app_resources`; for each, appmux first checks with Alice's own token that Alice can use it ([8.2](#82-resources-and-env-entries)) |
| 5 | Alice | shares the app with the group `sales-team` (`CAN_USE`), then presses **Activate** on release 1 | row in `app_grants`; the stored snapshot is built and started in a sandbox, the health check passes, the route `/sales/` opens, release 1 is `active` |
| 6 | Bob | opens `/sales/` | the edge reads Bob's email, asks Databricks (with Bob's token, cached) for Bob's groups, finds `CAN_USE` through `sales-team` and forwards to the sandbox |
| 7 | Alice | edits the source and presses **Update**; later a broken change | release 2 starts beside release 1 on a private route, passes health, the route swaps, release 1 drains. The broken release 3 fails health and is `failed`; release 2 keeps serving |
| 8 | Alice | tags release 2 `stable`, names release 1 "first version" and locks it, deletes release 3 | `releases.tags`, `releases.label`, `releases.locked_by` and `locked_at`; release 3 soft-deleted (it is neither active nor locked); audit `release.tag`, `release.label`, `release.lock`, `release.delete` |
| 9 | Alice | presses **Activate** on release 1 | the same path as an update with release 1's stored snapshot, wheels and config; any live release can be chosen; no source folder or token is read |

Step 1 can also start from Alice: Alice presses **Request a new app** on the launcher with the same values and a reason, and Dana approves ([9.4](#94-requests-to-a-platform-admin)).

## 2. Principles

Constitution principles I–III are design constraints: children stay standard apps; a route opens only after fetch → build env → start → health; adding or updating never interrupts running apps. IV (docs ship with code) and V (observable verification) govern how the work is done.

Five design premises, set by the project owner, are checked on every decision:

1. **No permission path may need a Databricks workspace admin.** Where a service principal would need an admin's grant, use OBO. A personal access token is the worst case and is not in v1.
2. **Malware test.** For every design: if the child is malware, can this harm? State the trade-off.
3. **Many apps in one slot** is the reason the repo exists.
4. **Level of effect.** Every config says whether it affects all children, one child, or one visitor.
5. **Review gate trade-off.** Where a risk cannot be removed by mechanism, gate it with a per-app approval by a platform admin and a recorded reason; state the gate's cost and keep gates few.

appmux is the **trust root**: anyone who can write the registry can cause code to run (through `source_path`, grants, env), so registry writes follow [4.2](#42-who-writes-what).

## 3. Architecture

### 3.1 Components

| Component | Responsibility |
|---|---|
| **Edge** | receives every request from the Databricks proxy; identity, ACL, routing, header/cookie/`Location` rewriting, WebSocket, per-app limits; serves the launcher and admin UI |
| **Reconciler** | compares desired with observed state and runs the lifecycle ([5](#5-child-lifecycle)); owns the in-memory route table; the only component that opens or closes a route |
| **Sandbox launcher** | creates a child's namespaces, network pair and lockdown, then starts the child; also runs builds |
| **Egress pump** (per child) | rule engine plus frame pump between the child's network pair and a `slirp4netns` process |
| **`self_check()`** | at startup, proves the sandbox holds with a canary child; failure means no routes |
| **Registry client** | Lakebase access with the right identity per write ([4.2](#42-who-writes-what)) |
| **Metrics collector, admin UI and API** | per-minute aggregates flushed to the registry; the Next.js admin pages (a static export) and the JSON API with the actions behind `authorize()` |

```
Databricks proxy ─► edge (identity, routing, ACL, rewriting, WebSocket)
                    reconciler (the only component that opens routes)
                    sandbox launcher + self_check() ─► child (own namespaces)
                    registry client (Lakebase) · admin UI · metrics
child ◄─ unix-socket relay ─ edge        child ─► veth ─► pump (rules) ─► slirp4netns ─► Internet
```

The services are Python 3.11 ([ADR 0012](../decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md)); [ADR 0014](../decisions/0014-build-the-edge-on-the-environments-http-stack.md) builds the edge on uvicorn, Starlette and httpx, which the environment already ships. The platform docs ask an app to support H2C, but spike 8 measured only HTTP/1.1 from the proxy, even toward a server that supports H2C, so the edge must accept HTTP/1.1 and may accept H2C ([reference](../reference/databricks-apps-platform.md)).

### 3.2 Trust boundaries

- **Trusted:** the appmux process tree, the registry, the deploy-time config of the appmux App. **Untrusted:** every child, its source, build scripts and everything it writes or sends. **Semi-trusted:** app admins (run their own app's code, cannot widen its reach) and users (permissions only).
- **The edge is reachable only by the platform proxy.** The proxy connects from loopback, so the source address proves nothing; instead children are kept from reaching the edge's port, and `self_check()` proves it.
- **Two permission layers.** Databricks decides who may reach the slot (`CAN USE` on the appmux App); appmux's tables decide which children that person may open. "Everyone" means everyone Databricks lets in.

### 3.3 Where state lives

| State | Where | Notes |
|---|---|---|
| Decisions and history: apps, grants, releases, resources (secrets as references), requests, settings, metrics, audit log | Lakebase | durable; the registry holds secret references, never values ([8.3](#83-secrets-where-the-value-comes-from)) |
| Snapshots and wheel files | the content store: the appmux SP's workspace folder | durable, named by digest; verified on Free Edition (spike 4): 500 MB per file in the folder, a Unity Catalog volume takes larger files |
| Environment cache, local copies of snapshots and wheels | `/tmp/appmux/` | rebuildable; survives a redeploy, not a stop and start (spike 1) |
| Run state: app states, restart counts, route table, in-flight requests, current-minute counters, recent events, child log tails, RSS | memory | lost on restart by design; the reconciler rebuilds it from the registry |

Only the build-log tail of a failed release is stored; child logs are not durable.

### 3.4 Module boundaries

The code keeps these modules separate, with dependencies pointing only the way shown, so the parts most likely to change (how an app is selected, where packages come from, who is allowed) can be replaced alone.

| Module | Does | Must not |
|---|---|---|
| `identity` | forwarded headers → `Actor` (email, plus the Databricks groups looked up with the person's own token and cached, [9.1](#91-the-model)) | know routing or apps |
| `authz` | `authorize(actor, action, app)`; a pure function over the permission data | do I/O or look at requests |
| `routing` | selectors mapping a request to an app and upstream path ([7.2](#72-the-selector-interface-and-what-v1-ships)); pure | check permissions, touch the network or read the registry |
| `rewrite` | request and response header, cookie and `Location` rewriting | decide routing or permissions |
| `upstream` | the child's unix socket: timeouts, size and concurrency caps, WebSocket | read the registry |
| `reconciler` | lifecycle state machines; owns the route table | serve requests |
| `sandbox` | namespaces, lockdown, the pump, `self_check()` | know releases or apps beyond a run directory and a rule list |
| `packages`, `appyaml` | package sources and overlay cache ([5.3](#53-build-and-package-sources)); the `app.yaml` parser and requirements check ([5.4](#54-appyaml-requirements-and-start)) | start processes outside the launcher |
| `secrets` | reads the value of a `secret` resource from its Databricks scope when a child starts ([8.3](#83-secrets-where-the-value-comes-from)) | log or return plaintext to anything but the launcher's environment builder |
| `registry`, `admin` | Lakebase access and the write-identity rule; UI and API under `/_appmux/` | contain business rules; bypass `authz` |

The edge pipeline runs in a fixed order: `identity → routing → authz → rewrite(request) → upstream → rewrite(response)`. `routing` and `authz` are pure and tested as tables.

## 4. Registry

Lakebase (Autoscaling) is the system database. **The database holds decisions and history, not the runtime's state**: what is running now, recent events and the route table are in memory, and snapshots and wheels are files in the content store. That keeps it to nine tables, each with one writer class. Columns, constraints, sample rows and walkthroughs are in the [data model](v1-data-model.md).

### 4.1 Tables at a glance

| Table | Holds | Written by |
|---|---|---|
| `platform_settings` | platform-wide values, including who the platform admins are | platform admin |
| `apps` | an app's platform facts: owner, name, source folder, budget, delegation flags, its egress rules | platform admin |
| `permission_requests` | a person's request to register an app, for a delegation, or to change an app's name, slug, source folder or memory budget, and its answer; **grants nothing** | the asker (ask, withdraw); platform admin (answer) |
| `app_config` | run or stop, target release, health check, own-SP keys | app admin (a platform admin acts as one) |
| `app_grants` | who may use or manage each app: users and Databricks groups; appmux keeps **no table of people or groups** | app admin |
| `releases` | snapshot reference, config, detected manifest, wheel list, status, name, tags, manual lock | app admin, and the reconciler for status |
| `app_resources` | the app's own resources, including secret references | app admin |
| `metrics`, `audit_log` | per-minute aggregates; every administrative action (append-only) | appmux; any writer |

Why `apps` and `app_config` are two tables: platform admins write one and app admins the other, which turns the write rule into plain `GRANT`s ([4.2](#42-who-writes-what)). Why no `app_type`: the runtime and framework are detected from the files ([5.4](#54-appyaml-requirements-and-start)). Where the data of the earlier 20-table draft went is listed in [data model 3](v1-data-model.md#3-the-nine-tables).

### 4.2 Who writes what

Platform data is written with the **platform admin's own OBO token and Postgres role**; everything else with the appmux SP. A child holding the shared-SP identity must not be able to overwrite platform data. Because every table has one writer class (the one exception, `permission_requests`, grants nothing), the rule is plain table-level `GRANT`s ([data model 6](v1-data-model.md#6-who-writes-what)).

| Written by | Identity in the database | Tables |
|---|---|---|
| a platform admin | that person's own Postgres role (OBO) | `apps`, `platform_settings`, answers in `permission_requests`; the first `app_config` row at registration |
| an app admin, or a platform admin acting on an app, through appmux | the appmux SP | `app_grants`, `app_config` (updates), `releases`, `app_resources`, requests and withdrawals in `permission_requests` |
| appmux itself | the appmux SP | `metrics`, release status |
| every admin action | whichever identity made the change | `audit_log` (insert only) |

- **Two layers.** The permission data decides what `authorize()` allows in the UI. The Postgres roles decide what a database connection may write; this layer matters only if the first is bypassed (a bug, or a child holding the SP token), and it is why platform data needs a human's OBO identity.
- **Adding a platform admin touches both:** an entry in `platform_admins` (or membership of a Databricks group listed there) and membership of the group role `appmux_platform_writers` for that person's own OAuth role, which the Lakebase project owner grants (no workspace admin). Without the second, the UI works but writes fail with "your database role cannot write platform data; ask the Lakebase project owner". This cost is accepted: it is the only way the shared-SP identity cannot edit platform data.
- **Residual (OBO).** Lakebase ignores a token's OAuth scopes (spike 1), so any OBO-delegated child receives a token that logs in to the registry as the visitor. When the visitor is a platform admin, the child can write platform data with that person's identity while the token is valid, and a connection it opened keeps working after it. The minimal scope set does not limit this; the platform admin's choice of *without token* does, and the owner accepted the rest on 2026-10-30 (spec question 5, option a; [TD-002](../tech-debt.md#td-002-an-obo-childs-token-reaches-the-registry)).
- **Residual.** A child with the shared SP can write `app_grants`, `app_config`, `releases`, `app_resources` (secret references) and `permission_requests` of any app, and so can bypass the attach check ([8.2](#82-resources-and-env-entries)) or fake a request. It **cannot** write an `apps` row (a delegation flag, name, slug, budget, source folder, egress rule) or a setting. Hence shared SP is a gated delegation ([8.4](#84-shared-sp-what-it-reaches-and-what-can-be-done)). Row-level security keyed on a session variable would not help, because a holder of the SP token can set the variable.
- **Fallback:** if logging in to Lakebase with an OBO token could not be made to work, shared SP would be dropped and the SP would write everything. Spike 1 showed the login works, so the fallback does not apply; it also showed that Lakebase checks only that the token is valid, not its OAuth scopes, and that a connection stays usable after the token expires (the login is the only check).

### 4.3 Change propagation and failure

- The reconciler re-reads the small tables every few seconds; there is no change counter. (Polling is simple and survives the 1-hour token lifetime, enforced only at login.)
- Writes use optimistic concurrency through `apps.version` and `app_config.version`; a conflicting write fails with "changed by someone else, reload and retry".
- Lakebase down while running: appmux serves from its in-memory copy, admin writes fail with a clear error, metrics buffer with a bound (fail open on purpose, with a visible gap). Down at startup: fail closed with a status page. New connections fetch a fresh OAuth token.

## 5. Child lifecycle

### 5.1 States

- **App:** `stopped`, `starting`, `running`, `crashed` (run state, in memory). The model leaves room for a later `sleeping` state.
- **Release:** `pending → building → starting → healthy → active`, plus `failed` and `superseded`. A release interrupted mid-build returns to `pending`; a `failed` release is never retried automatically but can be activated by hand.
- `app_config.target_release_no` is the release the app admin chose to go live by pressing Update or Activate (nobody approves it); the **active** release is what last passed health. They differ during an update or after a failed one, so a restart runs the last good release.

### 5.2 Register, update, roll back and keep releases

1. **Register** (a platform admin, directly or by approving a visitor's request for a new app, [9.4](#94-requests-to-a-platform-admin)): slug, display name, `source_path`, owner, memory budget. The app starts stopped with no release.
2. **Update** (an app admin presses the button):
   1. `authorize()` allows it.
   2. appmux reads the folder at `source_path`, and any wheel folders the install needs ([5.3](#53-build-and-package-sources)), **with the token of the person who pressed Update** (OBO). That person needs read access; appmux's service principal needs none. The workspace API does not refuse a caller with no access: it returns an empty listing or a 404 (spike 15), so a folder that lists no files is refused as `empty, or you cannot read it`. The App declares the scope `workspace.workspace:read` for this ([13.1](#131-what-the-operator-sets-up)). It computes a digest and stores an immutable snapshot; the same digest as an existing live release means "already up to date". The token is held in memory for this step only; an Update that outlasts it fails with `session expired during the snapshot; press Update again`. Snapshots are never followed live, which keeps a running app from breaking when someone edits the source.
   3. appmux reads the snapshot's `app.yaml` and stores the manifest; if the snapshot has a `databricks.yml` it also stores the resources that file declares, as an offer ([5.4](#54-appyaml-requirements-and-start)). An invalid `app.yaml` refuses the Update.
   4. If the app already has its maximum number of live releases, the oldest unprotected one (not active, target, in progress or locked) is deleted (below). Admission control checks that the budget fits **next to** the running release; if not, the Update is refused with a reason (fail closed).
   5. A release is created from the snapshot plus a config copied from the target release (editable), and becomes the target.
   6. The reconciler checks the requirements ([5.4](#54-appyaml-requirements-and-start)), builds the environment, starts the release beside the old one on a private route and health-checks it.
   7. On success the route table swaps atomically and, in one transaction, the release becomes `active` and the old one `superseded`; the old one drains, then stops. On failure the release becomes `failed` with its reason and log tail, and the running release is untouched. A child that ends before its first healthy answer fails the release at once, and the reason names its exit code or signal and how long it ran (`the app was stopped by signal SIGTRAP after 1.2 s`); the tail is the last 50 lines of that start's output, standard output and standard error both.
3. **Config change** (env entries, wheel sources) creates a new release with the same snapshot, so config is versioned, reversible and goes through the same health-gated swap, without reading any source folder. **Resources, secrets and own-SP keys belong to the app, not to a release**: a change takes effect when the app next starts.
4. **Roll back** = activate **any** live release, not only the previous one: steps 4 (admission only) and 6–7 with its stored snapshot, wheels and config. It reads no source folder and needs no token. It runs against the *current* resources; a release whose requirements are no longer met fails before it starts. A release that never passed health can be activated; this is also how a release that failed on missing resources is started once they are attached.

**Keeping releases.** The platform setting `max_releases_per_app` (default 10; a platform admin might choose 20) caps live releases per app; no number is fixed in code. An app admin may **name** a release (`label`) and **tag** it (`stable`); tags are free labels, not unique, and protect nothing. What protects a release is a **lock**:
- The **active** release is locked by default, with no action and no stored mark.
- An app admin may **lock** any live release by hand, for example a known good version, up to `max_locked_releases` per app (a platform setting, default 3, at most the release cap minus 4). A fourth lock is refused: `app 'sales' already has 3 locked releases; unlock one first`. Only manual locks count. Any app admin may unlock.
- The **target** and a release being built or started are protected until they finish.

A release none of these protects is free to go: when it stops being active and has no manual lock, an app admin may **delete** it, and the automatic clean-up may too. When an Update would exceed the cap, appmux deletes the **oldest unprotected release** and records `release.delete` by `appmux`; the rule between the two settings guarantees one exists, and if none does the Update is refused: `app 'sales' has 10 releases and all are protected (active, target or locked); unlock one`. To delete a locked release, unlock it first. Deletion drops the snapshot link, wheel links and tags; the row stays as history, so numbers are never reused. A daily job removes content-store files that no live release references ([data model 4.6](v1-data-model.md#46-releases)).

`source_path` stays fixed by the platform admin: it decides what code runs in the slot, so it belongs to the trust root.

### 5.3 Build and package sources

The build (preparing the packages a release needs) runs **inside the sandbox** with the app's egress rules, because it executes untrusted code. A child runs on the parent's Python, so the **app environment** — the packages already installed in the appmux App's own Python environment — is the **base layer**: the packages the platform provides and appmux's own dependencies. Each release adds a thin **overlay** on top for what the environment lacks or has at another version, and the overlay wins. The environment is never modified: it is mounted read-only into the sandbox, and `self_check()` proves a child cannot write to it ([6.4](#64-self-check-failures-and-gaps)).

**Resolution order.** For each requirement of the app (its `requirements.txt`), appmux decides in this order:

1. **The environment satisfies it** (the package is installed and its version meets the requirement, read with `importlib.metadata` and `packaging`) → use the environment's copy. Nothing is fetched or installed, and neither the index nor a wheel is needed.
2. **Otherwise a wheel is needed**, and it **overwrites** whatever the environment has under that name: a pin the environment cannot satisfy is installed into the release's overlay from a wheel file (see Wheels, below) and found before the environment's copy. To force an own build of a version the environment already has, give it a local version (`1.2.3+acme1`).
3. **If no wheel has it**, an index mode takes the package from the index. With `wheelhouse`, the Update fails and lists **every** requirement it could not resolve at once: `no wheel for 'foo==2.0' (the environment has foo 1.0)`.

Which packages the environment holds is shown on the Environment tab of the appmux App (the platform docs list none); the Update's build summary says how many requirements it covered (`41 of 58 from the environment, 17 from wheels`).

Spike 14 measured the coverage for FastAPI, Streamlit, Dash and Gradio against the Free Edition environment (132 packages). A top-level requirement **left unpinned** is satisfied by the environment in all four (it has fastapi 0.115.0, streamlit 1.38.0, dash 2.18.1 and gradio 4.44.0). **Pinned** to the newest version at least 14 days old, the same four need 5, 2, 3 and 16 wheels (0.6 to 38.6 MB), of which 1, 0, 0 and 2 are compiled; pinning every package of the lock instead needs 14, 26, 22 and 41. Every one of these wheels exists for Python 3.11 and `manylinux2014_x86_64`, so none of the four lacked a wheel. These are four samples, and the Free Edition environment may differ from the one on another workspace.

Where the packages a build still needs come from is the **package mode**, a platform setting for all children:

| Mode | Behaviour for what the environment cannot satisfy | Typical use |
|---|---|---|
| `index` | install from the package index in the workspace's pip configuration | the index is reachable |
| `wheelhouse` | install **only** from wheel files; `pip --no-index` | the index (for example a company Nexus) is blocked from the workspace |
| `wheelhouse+index` | wheels first, the index for what is missing | partial mirror |

A source distribution is built only in `index` mode, in the sandbox, with the compiler the platform's container provides. Spike 7 built a C extension that way in 2.7 s with build isolation (the build's `setuptools` fetched through the pump, which needs the launcher's `resolv.conf`, [6.1](#61-sandbox)) and in 0.8 s without it, using the environment's `setuptools`. A build whose index is unreachable fails closed but slowly (128 s, and more than 300 s when names resolve and connections are dropped), and `pip`'s own `--retries` and `--timeout` did not bound it; the platform setting `build_timeout_s` (default 600 s) bounds a build.

**Wheels: declared per child, stored once.**
- **Sources are declared by the child.** At Update, wheels are looked for in this order: (1) `wheels/` inside the app's own source folder, snapshotted with it; (2) the folders in the release config's `wheel_sources`, chosen by the app admin; (3) the platform's `default_wheel_sources` (optional, a home for big common packages). All are read with the Update presser's token, like `source_path`.
- **A wheel lock names the wheels.** In the wheel modes, a requirement the environment cannot satisfy must be pinned (`==`) **with a hash** (`pip-compile --generate-hashes` or `uv pip compile --generate-hashes`); requirements the environment satisfies need neither a pin nor a hash. For each such pin appmux first looks in the content store by sha256 and fetches from the sources only what is missing, matched by filename and verified against the hash. A hash found nowhere fails the Update: `no wheel for 'foo' (sha256 3b1e…) in the wheel sources`. A wheel carries its own dependencies only if the wheel lock lists them, so compile the wheel lock for the whole closure.
- **The store is content-addressed.** One file per distinct sha256, so a heavy wheel used by thirty apps is stored once. Each release records the wheels it installed (its **wheel list**), so restarts, rebuilds and rollbacks need neither the source folders nor anyone's token.
- **No cross-app effect.** A release's overlay is assembled from that release's own wheel list, so a wheel in one app's source never gets installed by another; a file in `default_wheel_sources` reaches an app only if that app's wheel lock names its hash. Only platform admins should be able to write that folder.

**Lazy install.** The overlay is built on the first Update or start and reused; the cache key is the digest of the wheel list and the Python minor version. A release that needs no wheel has no overlay and runs on the environment alone. `/tmp` survives a redeploy but is emptied by a stop and start (spike 1), so the cache usually survives an upgrade and is rebuilt after every stop, including the daily auto-stop on Free Edition. For an overlay folder `O` and a directory `W` holding exactly the release's wheels:

```
python -m pip install --no-index --no-deps --require-hashes --find-links W --target O -r wheels-needed.txt
PYTHONPATH=O  PATH=O/bin:$PATH  <the child's command>
```

`PYTHONPATH` is what makes the overlay win. An entry-point script in `O/bin` has the environment's interpreter as its shebang and, started without `PYTHONPATH`, imports the environment's packages (spike 18).

There is no per-release virtual environment: a virtual environment cannot see another's packages unless it is built from the parent's interpreter with `--system-site-packages`, and a child must not be able to modify the parent. `--no-deps` is deliberate: the wheel lock already lists the closure, so pip never fetches a dependency behind its back. In `index` mode, appmux lets pip resolve the missing requirements with their dependencies into `O`. After the install, a `pip check` against the overlay and the environment runs and its output is shown as a warning on the release (for example, an overlay `pydantic` that the environment's `fastapi` does not accept); it never fails the build. Spike 18 measured why it matters: overriding `opentelemetry-api` broke the environment's `opentelemetry-sdk` and `opentelemetry-semantic-conventions`, which pin it with `==`, while the app still answered; `pip check` named each gap, and the lock was complete once it listed all three. So the wheel lock covers the closure of everything it overrides, including the environment packages that depend on it. Spike 14 saw the same in three of four frameworks when the top-level package was pinned to its newest version (FastAPI, Streamlit and Gradio: `starlette`, `opentelemetry-api` or `websockets` replaced, and the environment's `fastapi`, `opentelemetry-sdk` or `gradio-client` then failed `pip check`; Dash was clean). A conflict that touches an environment package only warns and never fails the Update; the health check is the gate ([plan](../../specs/001-appmux-v1/research.md#d6-a-conflicting-overlay)).

**The environment can change.** A redeploy of appmux can change what the environment provides. At every start appmux re-checks the requirements the environment was satisfying; if one no longer holds, the release is `failed` before it starts: `the environment no longer provides 'uvicorn==0.30.6' (it has 0.31.0); add a wheel and Update`. A release is therefore reproducible when its wheels are in the store and the environment still satisfies the rest; otherwise it fails closed with a reason.

**Preparing wheels** happens on any machine that reaches an index: compile a hashed wheel lock, `pip download --only-binary=:all:` for Python 3.11 and `manylinux2014_x86_64` **only the packages the environment lacks** (the build summary names them), then `databricks workspace import-dir` into a wheel folder. A package with no wheel for this platform fails with `no wheel for 'foo'`; the app admin builds one elsewhere and adds it. Source distributions are not built in the wheel modes (`--only-binary`), which removes the largest install-time code-execution path, and `wheelhouse` mode gives the build an all-deny egress list. The overlay is written by the builder and mounted **read-only** into children. **Node children** have an offline source that spike 14 measured: a lock file (`package-lock.json`) and an npm cache, installed in the build sandbox with `npm ci --offline --ignore-scripts` from a **read-only** cache. On the Free Edition container (Node 22.23.4, npm 10.9.9, no network) it installed 66 packages (Express 5.3.0 and `ws`) in under a second, and the project then ran from a read-only tree; a cache that lacks a package, or a lock with an altered integrity, fails with `ENOTCACHED` and installs nothing. Install scripts stay off, so a package that needs one (a native addon) does not install. The package modes above are defined for Python wheels; v1 does not ship the npm cache ([15](#15-beyond-v1)), so a Node app with dependencies needs `index` mode. appmux's own dependencies have the same blocked-index problem: see [13.1](#131-what-the-operator-sets-up). Spike 18 ran it on the real container: the offline hashed install, the overlay winning over the environment, an entry-point script from `O/bin` and read-only mounts all work (`pip` 24.0 is in the environment, so the sandbox needs no other tool); a framework other than FastAPI on an overlay was not run.

### 5.4 app.yaml, requirements and start

A user deploying on appmux gets what a standalone deployment gets: appmux reads the child's own `app.yaml` and follows the same contract, so no appmux-specific file exists. What a standalone deployment gets from the platform is a port, a base path, environment variables and declared resources; appmux provides each.

**Runtime and command.** appmux detects the runtime from the snapshot as Databricks does: `requirements.txt` (or `pyproject.toml` with `uv.lock`) means Python, `package.json` means Node. The start command is `command` in `app.yaml`; without one Databricks' defaults apply (`npm run start` for Node, `python <app>.py` for Python). It is run as an **argument list without a shell**.

**The parse.** At Update appmux reads `app.yaml` (or `app.yml`) from the snapshot and keeps only `command` and `env`:

| Input | Result |
|---|---|
| both `app.yaml` and `app.yml` present | the Update is refused |
| an `env` entry with both or neither of `value` and `valueFrom` | the Update is refused, naming the entry |
| any other key | a warning shown on the app page; the key is ignored |

The result is stored with the release as its **detected manifest** (runtime, framework, command, env entries, warnings; sample in [data model 4.6](v1-data-model.md#46-releases)).

**The bundle file.** A standalone App is often deployed from a Databricks Asset Bundle, whose `databricks.yml` declares the app's resources. If the snapshot has a `databricks.yml` (or `databricks.yaml`), appmux reads it for **one** purpose: the list `resources.apps.<key>.resources[]` of the app, to pre-fill the Requirements panel and the resources screen. If the bundle declares several apps, the app admin picks which is this one, and the choice is kept in the release config.

| In the file | Result |
|---|---|
| a resource entry (`name`, one type key such as `sql_warehouse`, `experiment`, `secret`, `uc_securable`, and its `permission`) | an **offer**: `name` becomes the key, the type key maps to an appmux type, the id or name field becomes the target, `permission` the permission (mapping in [data model 4.7](v1-data-model.md#47-app_resources)) |
| a value that is a `${…}` interpolation (a variable, another resource) | shown as unresolved; the app admin types the value |
| a permission that is not a level of the type (for example `IS_OWNER`) | a warning; the app admin picks a level |
| `config` (`command`, `env`), `permissions`, `user_api_scopes`, compute, budget, git, targets, variables | ignored, with a warning: `app.yaml` is the contract for `command` and `env`, and appmux never applies a grant a file declares |
| not valid YAML, or no `resources.apps` | a warning; an invalid bundle file never refuses an Update |

The offers are stored in the manifest as `bundle_resources`. **Reading the file attaches nothing:** the app admin confirms or edits each offer and presses Attach, which runs the same attach check as a hand-typed resource, with the admin's own token ([8.2](#82-resources-and-env-entries)). This is the standalone rule as well: declaring a resource in a bundle needs the deployer to hold the permission, and the check proves the admin holds it.

**Requirements.** The keys a release needs are every `valueFrom` in the manifest plus every `value_from` the app admin added to the release `env`. They are checked **first** in the reconcile, before any build: if some key is not a resource of the app, the release becomes `failed`, listing **all** missing keys at once (`missing resources: openai-api-key, sql-warehouse`). The app page shows a **Requirements** panel: each key with attached or missing, and for a missing key an **Attach** form pre-filled from the bundle offer of that key, or, when the file does not declare it, with the key and a suggested type (a hint from the key name, never a gate). The panel also lists the resources the bundle declares that no `valueFrom` mentions, which can be attached too. After attaching, the app admin presses **Activate** on that release, which starts the stored snapshot with no source read and no token.

**The environment** the child receives, later wins:

1. the `app.yaml` values, with each `valueFrom` resolved from the app's resources ([8.2](#82-resources-and-env-entries));
2. the release's own `env` entries;
3. appmux's variables, which cannot be overridden: the child's port as `DATABRICKS_APP_PORT`; `DATABRICKS_APP_NAME` (the slug), `DATABRICKS_HOST` and `DATABRICKS_WORKSPACE_ID`; the base path as `APPMUX_BASE_PATH=/sales` (plus the `X-Forwarded-Prefix` header on every request); credentials for the app's mode ([8.5](#85-choosing-a-mode-and-delivering-credentials)); and the framework variables Databricks sets, all pointing at the same port: `GRADIO_SERVER_PORT` and `GRADIO_SERVER_NAME=0.0.0.0`, `STREAMLIT_SERVER_PORT` and `STREAMLIT_SERVER_ADDRESS=0.0.0.0`, `UVICORN_PORT` and `UVICORN_HOST=0.0.0.0`, `FLASK_RUN_PORT` and `FLASK_RUN_HOST=0.0.0.0`, `PORT`, plus `STREAMLIT_SERVER_HEADLESS=true` and `STREAMLIT_BROWSER_GATHER_USAGE_STATS=false`.

The child never receives appmux's own environment. It listens on a port in its own network namespace and a relay connects it to a unix socket the edge uses. The framework variables follow the list measured on a real App in spike 17: the ones above plus `GRADIO_ANALYTICS_ENABLED=false`, `STREAMLIT_SERVER_ENABLE_CORS=false` and `STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=false`; the platform also sets `HOME`, `LC_CTYPE`, `PYTHONUNBUFFERED`, `UV_PYTHON_INSTALL_DIR` and `DATABRICKS_APP_URL`, which appmux handles one by one: `LC_CTYPE=C.UTF-8` and `PYTHONUNBUFFERED=1` as the platform sets them; `HOME` is the child's private writable temp directory; `UV_PYTHON_INSTALL_DIR` is not set (v1 supports only the parent's Python); `DATABRICKS_APP_URL` is the parent's URL plus `/<slug>`. For the base path, appmux sets the variables below for every child, so unmodified code generates `/sales/...` links while the prefix is stripped from the request (measured per framework in spike 5; a variable a framework does not read is inert): `UVICORN_ROOT_PATH=/sales` (FastAPI, read by the `uvicorn` command), `GRADIO_ROOT_PATH=/sales`, and for Dash both `DASH_REQUESTS_PATHNAME_PREFIX=/sales/` and `DASH_ROUTES_PATHNAME_PREFIX=/`. appmux sets **no** Streamlit variable: Streamlit works with the prefix stripped when left alone, and `STREAMLIT_SERVER_BASE_URL_PATH` breaks it. A Node app needs no setting if its links are relative. The framework name is a hint, never a gate: an app that hard-codes absolute links hits the limit of [7.2](#72-the-selector-interface-and-what-v1-ships). v1 targets FastAPI first, then Streamlit, Dash, Gradio and Node; all five were hostable in spike 5.

**Where appmux differs from a standalone App, on purpose:**

| Difference | Why |
|---|---|
| no service principal of its own, and no automatic grant when a resource is attached | a child gets a credential only by delegation or own SP ([8.1](#81-the-three-modes-and-delegation-requests)); premises 1 and 2 |
| runs on the parent's runtime: Python 3.11, Node 22 | one container; a standalone App that uses `uv` may pick another Python version, which v1 does not support |
| no telemetry: `OTEL_*` variables are not set | not provided in v1 |
| `databricks.yml` is read only to pre-fill resources; nothing in it is applied, and each resource is confirmed and checked with the admin's own token | a file must never widen an app's reach; the check mirrors the permission a standalone deploy needs |
| a `valueFrom` key with no resource fails the release with a clear list | a standalone deploy only logs an error and starts the app without the variable (spike 17); a child that would start with a missing secret is worse than one refused |

Measured in spike 17: Databricks substitutes `$DATABRICKS_APP_PORT` inside a `command` argument, but not `${DATABRICKS_APP_PORT}` and not inside an `env` `value`. appmux does the same: it replaces `$NAME` in each `command` argument from the child's environment, leaves `${NAME}` and `env` values as written, and runs no shell, so a child behaves as it does on a standalone App.

### 5.5 Health and swap

The child cannot be asked for a health endpoint, so **health is: a GET on `health_path` (default `/`) that answers with any status below 500**. A 200, a 302 to a login page, a 401 and a 404 all mean "up and answering"; a 500, a refused connection or a timeout do not. `health_deadline_s` (default 60) is how long a *new release* has, from the moment its process starts, to give a first healthy answer; appmux polls once a second. A slow starter gets a longer deadline from its app admin (5 to 600 s); a typo fails fast. Both settings live in `app_config` because they affect no other app: admission control counts the second copy's memory during a swap for at most the deadline.

```
t=0 s   process started        poll → connection refused
t=23 s  poll → 200             healthy → route opens (or swaps)

t=0 s   process started        poll → connection refused
t=60 s  deadline reached       release failed: "health check did not pass within 60 s
                                                (last error: connection refused)"
```

Once running, an app is judged by crashes ([5.6](#56-crash-restart-and-shutdown)), not by repeated probes. The swap replaces the route table atomically. Requests in flight on the old release finish within 30 s (a constant); WebSockets still open then are closed; only then is the old release stopped.

**Renaming an app.** A new `slug` ([9.4](#94-requests-to-a-platform-admin)) changes the URL prefix and two variables the child receives (`APPMUX_BASE_PATH` and `DATABRICKS_APP_NAME`), so a running app is restarted through this same swap: a new copy starts under the new prefix and passes health, the route table swaps atomically to the new route, and the old copy drains. The old URL answers `404` from then on, and cookies the child set for the old path are no longer sent, so its own sessions end. A stopped app only has its row changed. If the new copy fails its health check, the old copy and its route keep serving and the app page shows the failure; the row already holds the new slug, so pressing Restart retries, and a platform admin can set the old slug back. No other app is touched.

### 5.6 Crash, restart and shutdown

- **Crash:** the same release restarts with backoff (1, 2, 4, 8, 16 s). After repeated failures the app becomes `crashed` and **only its route** answers 503 with a short status page. There is no automatic fallback to an older release; an admin activates one. A crash is logged with the exit code and the last log lines.
- **Startup:** bind the port first, with a small gate in front of everything: while the process is *starting*, `GET` and `HEAD` on `/` answer `200` with a static "appmux is starting" page and every other path answers `503`, so the platform's probe of `/` is answered while the slow steps run (a sibling project measured a container restart when nothing answered during its start; not measured on Free Edition). Then connect to the registry, run `self_check()`, read the registry, start each running app's active release, open routes one by one as health passes. The gate opens to the real application only after `self_check()` has passed; if the registry is unreachable or `self_check()` fails it stays shut and answers `503` on every path, `/` included, with a page naming the reason. The startup pages carry no identity, registry or environment data. A few minutes of full outage is acceptable; appmux cannot be blue/green inside its own slot.
- **`SIGTERM`:** the platform allows 15 seconds. Stop accepting, flush metrics, stop children in parallel. A redeploy keeps `/tmp`, so the environment cache usually survives; a stop and start empties it.

### 5.7 Memory and admission control

Each app has a memory budget set by the platform admin. At startup appmux reads the container's memory limit. It refuses to start or update an app when the running budgets, the update overlap, appmux's reserve and per-child overhead (pump ≈ 10 MB, slirp 2–3 MB, measured) would exceed it. Worked example, the 8 GiB container measured on Free Edition:

```
limit                    8192 MB
appmux reserve          -1024 MB   (platform setting memory_reserve_mb)
available                7168 MB
10 apps × (512 + 13)     5250 MB   → fits, 1918 MB free   (13 MB = pump 10 + slirp 3)
update of `sales` next to its running release: +525 MB → 5775 MB → allowed
a new `ml` app, budget 2048: +2061 MB → 7836 MB > 7168 MB → refused: "needs 2061 MB, 1393 MB free"
```

A best-effort RSS watchdog stops only the offending app (cgroup and `oom_score_adj` writes are denied, so there is no hard cap); `RLIMIT_AS` is set from the budget with a headroom factor. Known gap: a fast allocator can still push the container into an out-of-memory kill. Spike 7 measured ten idle stand-in sandboxes by PSS: the pump 5.6 MB, slirp 0.3 MB and the stand-in's in-sandbox Python process 15.9 MB (21.8 MB each); SC-003 measures the real per-child overhead.

## 6. Isolation and network

### 6.1 Sandbox

All children run as the **same OS user** (uid 1000) as appmux inside **one container**. appmux has no root and no `useradd`, and separate uids would not exist here anyway. Separation comes from Linux **namespaces**: each gives a process its own private view of one kind of system resource.

| Namespace | What it makes private | What it blocks for a hostile child |
|---|---|---|
| **user** | the mapping of user ids and the set of capabilities | acts as "root" *only inside* its other namespaces; outside it is still uid 1000 with no extra rights |
| **mount** | the list of mounted filesystems | the child gets a root built from an allow-list ([step 5](#61-sandbox)), so the platform's files, appmux's source, home and shared temp directories do not exist for it |
| **pid** | the process ids it can see | other children, appmux and PID 1 do not exist for it: no `/proc/<pid>/environ`, no signals |
| **ipc** | SysV shared memory and message queues | another process's segments and queues are invisible (without it both stay visible) |
| **net** | interfaces, addresses, routes, ports | no access to the container's loopback or IP, so it cannot reach appmux's port or other children's sockets |

An unprivileged process may create a *user* namespace and then holds every capability over the namespaces it owns, so an unprivileged appmux can create a network namespace, mount `tmpfs` and build an interface in it, with those powers confined to the sandbox. The probes on the Databricks container (Free Edition) used util-linux `unshare`; the first probe had no `--ipc`, which spike 2 showed is needed:

```
unshare --user --map-root-user --net --mount --pid --ipc --fork --kill-child -- <stage-1 program>
```

`--map-root-user` maps inside uid 0 to outside uid 1000 (measured `/proc/self/uid_map`: `0 1000 1`; not real root; `--map-current-user` would keep uid 1000 and was not tried, so v1 keeps `--map-root-user` as measured); `--ipc` hides SysV shared memory and message queues; `--fork` is required for the pid namespace; `--kill-child` kills the sandbox if the launcher dies.

**Stage 1 — what the launcher program does inside the new namespaces**, in order (the measured sequence; step 5 follows spike 2 and step 7 differs):

1. Bring up the child's own loopback `lo`.
2. Create the network pair, give it an address and a route ([6.2](#62-network-path)).
3. Hand a socket for the pair's far end to appmux, then wait until the NAT is attached.
4. Make all mounts private (`MS_REC|MS_PRIVATE`) so nothing propagates out.
5. Build the child's root from an **allow-list**, never by hiding paths one by one (spike 2): a fresh `tmpfs` root, then `pivot_root` into it and detach the old root, then `chroot` into a subdirectory of it (this is what makes a nested `unshare(CLONE_NEWUSER)` fail). In it, **read-only**: `/usr` (with `/bin`, `/sbin`, `/lib`, `/lib64` as symlinks), the app environment (the parent's Python packages, [5.3](#53-build-and-package-sources)), the release at `/srv/app`, and ten `/etc` files (`ld.so.cache`, `ld.so.conf`, `ld.so.conf.d`, `ssl`, `ca-certificates`, `nsswitch.conf`, `passwd`, `group`, `localtime`, `os-release`) and an `/etc/resolv.conf` that the launcher writes with the one line `nameserver 10.0.2.3` (slirp's resolver; none of the ten is one, and without it a name lookup fails, spike 7); a read-only bind is remounted with `MS_RDONLY` **and the source mount's locked flags** (`nosuid`, `nodev`, `noexec`, `relatime`) or the remount fails. **Read-write**: the per-child run directory at `/run/appmux` (host-backed, so not capped: [6.4](#64-self-check-failures-and-gaps)), and a private `tmpfs` each for `/tmp` (64 MB), `/var/tmp` (16 MB), `/dev/shm` (64 MB) and `/home/app` (64 MB). `/dev` holds `null`, `zero`, `full`, `random` and `urandom`; `/proc` is a `tmpfs` with only `cpuinfo`, `meminfo` and `/proc/self`, and `/proc/self/{cgroup,mountinfo,mounts,mountstats}` overmounted by `/dev/null` because they name the pod and the host's paths. Nothing else exists: no `/databricks`, `/sys`, `/mnt`, `/etc/hostname`, `/etc/hosts`, no app directory and no sibling's run directory or release.
6. Drop every capability (`PR_CAPBSET_DROP`, clear the ambient set, `capset` to empty) and set `no_new_privs`. After this it can no longer mount, create interfaces or open `AF_PACKET` (all measured as denied).
7. `exec` the child's command (the probe served a test app from the same program; appmux runs the real command from `app.yaml`).

Measured on the Databricks container (Free Edition): launch takes 0.1–0.2 s; the sandbox sees no other pids and cannot read PID 1's or siblings' `environ`; 0 of 89 localhost ports answer (a plain sibling process reaches 77 of 89). Spike 2 measured the filesystem: without the allow-list a child reads `runtime.cfg` (which names a client secret) and the platform's `dynamic-secrets`, reads and writes the customer log and `/dev/termination-log`, and writes `/home/app`, `/dev/shm` and the app directory; with it, 18 of 18 canaries (those files, a sibling's release, run directory and socket, the shared temp directories) answered `ENOENT`, exactly five mounts are writable, and a nested user namespace fails with `EPERM`. Stage 1 also closes every descriptor above 2 before `exec`; the checker still found three inherited descriptors, so this is required, not yet shown to work.

**Inbound.** The child's server listens on loopback inside its own net namespace, which nothing outside can reach. A relay inside the namespace listens on a unix socket in a per-child run directory (the only host path the sandbox shares with appmux) and forwards each connection to `127.0.0.1:<port>`; the edge connects to that socket. The relay starts listening only once the child's port accepts a connection, so until then a connect to the socket is refused, which is the `connection refused` the health check reports (5.5).

### 6.2 Network path

The child's net namespace starts with no network. This container has no `/dev/net/tun` and no root, so the usual user-mode NAT (a TAP device) cannot start; the chain below replaces the TAP device with a virtual Ethernet cable and a small program that holds its other end.

```
 child process ── ordinary sockets
 eth0 10.0.2.100 ──── veth pair ──── vpump0        (both ends inside the child's net namespace)
                                       │ AF_PACKET socket (raw Ethernet frames), its fd passed to appmux over SCM_RIGHTS
                                  pump (appmux, one per child)  ◄── egress rules decide here
                                       │ SOCK_SEQPACKET socketpair (one frame per message)
                                  slirp4netns  (in appmux's own network namespace)
                                       │ ordinary sockets from appmux's namespace
                                    Internet / workspace
```

1. **`eth0`.** The child sees a normal interface `10.0.2.100/24` with a default route via `10.0.2.2` (slirp's conventional addresses; its DNS is `10.0.2.3`).
2. **veth pair.** Two virtual interfaces joined like the ends of a cable. The launcher creates it by speaking rtnetlink directly (there is no `ip` command) and turns off checksum and segmentation offloads.
3. **`AF_PACKET` socket.** A raw socket on `vpump0`, opened inside the namespace and passed to appmux with `SCM_RIGHTS`; appmux reads and injects whole frames without joining the namespace, and the child never receives the descriptor.
4. **The pump.** One appmux-owned process per child. Child → world: parse the Ethernet and IPv4 headers, apply the egress rules to the destination, forward or drop. World → child: forward. The child's only way out is `eth0`, so it cannot route around the pump. Frames it cannot parse are dropped (fail closed), and so is every non-IPv4 frame, IPv6 included, except ARP, which is forwarded so that the child can find its router.
5. **`slirp4netns`.** A user-space TCP/IP stack playing router, DHCP and DNS: it terminates each TCP and UDP exchange and re-opens an ordinary socket from appmux's namespace to the destination, so children inherit the workspace's DNS and network path. It runs in `--netns-type=tapfd` mode with `--disable-host-loopback`, so `10.0.2.2` does not lead to appmux's loopback.

| Child does | What happens | Result |
|---|---|---|
| `pip download six` | DNS `pypi.org`, then TCP to the resolved address on 443: not matched by a deny rule, slirp connects out | works at the speed of a plain container (measured 11–12 MB/s) |
| `connect(169.254.169.254, 80)` | the pump matches `deny 169.254.0.0/16`, drops the frame and counts it | the child sees a timeout, not a refusal |

Measured: relay p50 ≈ 0.5 ms; pump ≈ 10 MB RSS (Python prototype), 5.6 MB PSS; slirp 2–3 MB RSS, 0.3 MB PSS. Under load (spike 7, Free Edition, Python prototype) the pump carried 100 MB/s down and 59 MB/s up, spending about 12 ms of CPU per MB (124 % of one CPU at 100 MB/s; `slirp4netns` adds about 3.6 ms), and 1 KiB exchanges at 8,000, 25,000 and 28,000 per second at 1, 10 and 100 connections (p99 0.26, 0.75 and 37 ms). The pump is not the limit; the container's CPU quota is ([6.4](#64-self-check-failures-and-gaps)), so a child's egress costs CPU that every app shares. An idle pump's only cost is its 10 ms rules-reload poll (3 % of one CPU for ten sandboxes), which a file-change wake-up would remove. The platform may change user namespaces, veth or `AF_PACKET`; `self_check()` ([6.4](#64-self-check-failures-and-gaps)) is the guard.

### 6.3 Egress rules

- Enforced in the pump, ordered, first match wins, **no match = allow**. v1 rules are CIDR and port; hostname rules come later. Evaluation order: the app's own rules (`apps.egress`), then the global rules (`egress_global`), then default allow. The virtual subnet `10.0.2.0/24` is always allowed (it is the cable's far end, and slirp's DNS at `10.0.2.3` lives in it: without the exception an allow-list also stops name resolution, spike 6).
- The **global default list** (editable) blocks the container IP, appmux's port and `169.254.0.0/16` (the first two as the placeholders `@container_ip` and `@appmux_port`, resolved at start); loopback is the slirp flag. The container IP is the one that matters most: on Free Edition it listens on 28 TCP ports (the App port where the edge listens, fluent-bit on 7270 and the platform's own services), a sandbox with no rule connected to all 28 and with the deny to none (spike 6). An allow-list for an app is allow rules followed by a trailing `deny 0.0.0.0/0`. A blanket private-range deny is not used: the workspace host resolves to a private address. Per-app rules are set by a platform admin only (the app page shows them read-only), and the editor refuses an `allow` in an app's list that overlaps `@container_ip`, `@appmux_port` or `169.254.0.0/16`, because `self_check()` ([6.4](#64-self-check-failures-and-gaps)) assumes those stay closed.
- IPv6 is not supported in v1: the pump drops every IPv6 frame and slirp runs without `--enable-ipv6`, so a child has only link-local addresses and its connects fail at once with `ENETUNREACH` (spike 6). IP fragments are dropped when port rules exist. That is conservative, not required: with fragments passed slirp reassembles them and a first fragment to a denied port is still dropped by the rule. The cost is that a UDP datagram larger than the MTU stops working for an app that has port rules.
- Rules reload live (spike 6): the pump loads a changed file within 10 ms. A new connection follows the new rule within a fraction of a second. A new deny silences an open connection without resetting it (the frames are dropped; neither side sees an error), and after the allow the connection resumes only when the child's TCP retransmit timer fires: about 25 s after a 30 s deny, with the longest echo delayed 55 s. A client therefore sees a stall, not an error. "Block host loopback" is a slirp start-up flag, so changing it restarts that child's NAT. The pump counts drops per rule and keeps sample destinations for the dashboard.
- New apps default to pass-through. Residual risks: default-allow egress and DNS tunnelling.

### 6.4 Self-check, failures and gaps

`self_check()` runs at startup and after any sandbox change, with the default rules. A canary child (a real sandbox running a short test program) must **not**:

| Check | Pass condition |
|---|---|
| read other processes' `environ` | `EACCES` or `ENOENT` for appmux, PID 1 and a sibling sandbox |
| see other pids | `/proc` lists only the canary's own |
| reach loopback ports, the container IP and appmux's port | connection to a decoy listener on appmux's loopback fails; a connect to **every** port the container IP listens on times out (28 on Free Edition, read from `/proc/net/tcp`: appmux's port is one of them) |
| reach `169.254.169.254:80` | TCP connect fails |
| read the platform's files | `runtime.cfg`, `/databricks/dynamic-secrets/…`, the customer log, `/dev/termination-log`, `/sys/class/dmi/id/*`, `/etc/hostname` and `/proc/self/cgroup`'s pod line are all absent or empty |
| regain a capability | `mount`, `ip link add` and opening `AF_PACKET` all fail |
| write to the app environment or an overlay | creating a file in the parent's `site-packages` and in an overlay fails (`EROFS`) |
| reach a sibling's unix socket, run directory or release | fails (`ENOENT`) |
| reach appmux's app directory, `/home/app` or another sandbox's `/tmp`, `/var/tmp`, `/dev/shm` | absent; a marker file written there by appmux is not visible |
| see SysV shared memory or message queues of another process | `shmget` and `msgget` on a planted key fail |
| create a nested user namespace | `unshare(CLONE_NEWUSER)` returns `EPERM` |
| inherit descriptors | only 0, 1 and 2 are open |

Any success makes appmux refuse to run children: no route opens and a status page explains. The result is shown on the platform page and logged; it also guards against the platform changing underneath appmux.

**When a layer fails.** If a child's pump or slirp dies, that child has no egress and it is treated as that child's crash. If the launcher cannot build a layer, the release fails and the reason is stored. appmux and other children are unaffected: each has its own pair and pump.

**Known gaps.** There are no cgroups. Spike 7 measured what that costs on Free Edition:

- **CPU.** The container has a quota of about 2 CPUs that it cannot see (4 are visible, no cgroup file shows it). A child with busy loops on every CPU exhausts it, and then every app, the edge included, stalls for about 50 ms at a time; no request fails. `nice` does nothing for that. Pinning the child's sandbox to one CPU (nice 19) keeps the hog from exhausting the quota, so what runs outside the pinned CPU (edge, pump, reconciler) is unaffected: with the pin and a seccomp filter that refuses `sched_setaffinity` the paced p95 stayed at 1.35 ms (50 ms without), and without the filter the child widened its own pin and was back at 50 ms. Children that share the pinned CPU compete by scheduler share (not measured), and the pin caps all children together at one CPU of two, so v1 does it: the platform setting `child_cpu_pin` is on by default and a canary in `self_check()` proves it. The quota of a paid workspace is not known.
- **Processes.** `RLIMIT_NPROC` is counted per sandbox (it stopped a fork test at 298 of 300 in 0.11 s while 34 other processes of the same user ran), so the launcher sets it; without it 2,000 forks took 1.5 s, 2 GB and 11 % of a neighbour's throughput. Killing the sandbox's init ends all of them in 0.6 s, but the container's PID 1 does not reap orphans, so the launcher must be a subreaper or each teardown leaves a zombie.
- **Disk.** The `tmpfs` mounts are capped (a 64 MiB one gave `ENOSPC` at 64 MiB). The run directory is host-backed and uncapped: a child wrote 1 GiB there in 1.5 s, and `RLIMIT_FSIZE` bounds only each file. Not tested: handing the relay a socket that appmux bound outside, so the run directory need not be writable, and a size watchdog like the memory one.
- **Untried and untested.** The cgroup and UTS namespaces are untried; measurements exist only for Free Edition; pods of other Apps on the same subnet are unreachable on Free Edition (the platform drops the traffic in both directions, spike 6), but that is untested on a paid workspace, where an admin can add the pod subnet to the global list.

## 7. Request path and routing

### 7.1 Flow

A worked request: Bob opens `GET /sales/api/orders?limit=5`.

1. The Databricks proxy authenticates Bob and forwards to the edge with headers such as `X-Forwarded-Email: bob@example.com`, `X-Forwarded-User`, `X-Forwarded-Preferred-Username`, `X-Real-Ip`, `X-Request-Id` and, only if user authorization is on for the appmux App, `X-Forwarded-Access-Token`.
2. `identity` builds the actor from the email and adds Bob's Databricks groups, looked up with Bob's own token and cached ([9.1](#91-the-model)). The edge trusts the headers because only the proxy can reach its port ([3.2](#32-trust-boundaries)) and, as spike 8 measured, the proxy overwrites client-supplied `X-Forwarded-Email`, `-User`, `-Access-Token`, `-Host` and `X-Real-Ip`. `X-Forwarded-For` is the exception: the proxy appends its hops to a client-supplied value, so neither end of it is the client, and the edge reads the client address from `X-Real-Ip`, which the proxy overwrites (spike 8).
3. `routing` picks the app: the first path segment `sales` is a registered slug; the upstream path is `/api/orders?limit=5`.
4. `authz` checks `CAN_USE` on `sales` (Bob holds it through `sales-team`). Default is deny.
5. `rewrite` removes any client-supplied header with an identity name, forwards email and username, removes `X-Forwarded-Access-Token` unless `sales` has `obo_delegated` and the visitor is its owner or named by a grant, or is a platform admin who chose **Open as a user** ([9.1](#91-the-model)), and adds `X-Forwarded-Prefix: /sales`.
6. `upstream` sends it over `sales`'s unix-socket relay (WebSocket upgrades included) under per-app concurrency, body-size and timeout caps (platform settings `max_concurrent_requests` 64, `max_request_body_mb` 100 and `request_timeout_s` 120, at most 290; [data model 4.1](v1-data-model.md#41-platform_settings)). The platform's own limits sit above them: 300 s for a request or a stream, 32 KB for one header; WebSocket is not subject to the 300 s limit (spike 8).
7. The response is rewritten for `Location` and `Set-Cookie`: a redirect to `/login` becomes `/sales/login`; a cookie's `Path` is forced to `/sales/` and its `Domain` dropped. **Response bodies are never rewritten.**

### 7.2 The selector interface and what v1 ships

One hostname and one port reach appmux, so an app can only be chosen by something in the request: the path, a cookie, a header. **v1 supports one way: the path prefix.** The code keeps a clean place for the others, so adding one later means adding a module, not changing the edge.

```
RouteSelector
    name            "prefix"
    select(request, registry_snapshot) → Selection | NoMatch
        Selection = { app_slug, upstream_path, extra_request_headers }
    rewrite_response(selection, response_headers) → response_headers

SELECTORS = { "prefix": PrefixSelector }   # the only entry in v1; a second one adds a routing-mode column with it
```

Selectors are pure: they receive an in-memory snapshot of the registry, do no I/O and check no permissions ([3.4](#34-module-boundaries)). `/_appmux/` is the launcher and admin UI, `_appmux` is a reserved slug, and `/` shows the launcher.

**`prefix`.** `/<slug>/x` goes to the app `slug` as `/x`; a path that matches no slug gets `404`. This fits apps whose framework supports a base path. Spike 5 measured it: FastAPI, Streamlit, Dash, Gradio and Node apps with relative links all work with the settings in [5.4](#54-appyaml-requirements-and-start). An app that hard-codes absolute links (`<script src="/static/app.js">`) is **not hostable in v1**: its page loads, the stray request gets a `404` and the page breaks, whatever the framework. This is the main cost of the narrow scope.

**Designed but deferred** (the interface leaves room):

| Selector | Idea | Cost it carries |
|---|---|---|
| `prefix-rescue` | an unmatched path goes to the app named by the `Referer`'s path, else by an edge-set cookie | per-browser, not per-tab: two apps open at once can cross-route |
| `root` | an edge-set cookie names the app that gets `/` and every unreserved path | one app per browser; the child cannot link back to the launcher |
| `header` | `X-Appmux-App` picks the app (service clients) | needs a rule for who may set it |
| `host` | a hostname per app | not available: one hostname per Databricks App |

If cookie-based selectors are added, the cookie is set only by the edge (`__Host-`, `HttpOnly`, `Secure`, `SameSite=Lax`), a same-named `Set-Cookie` from a child is dropped, and the ACL is always checked for the app the selector names. Response bodies are never rewritten in any mode (nginx `sub_filter` style): URLs built in JavaScript or JSON are missed, streaming breaks and every response would be buffered in the shared edge.

### 7.3 Same-origin limit (accepted in v1)

All children and the admin UI share one browser origin. A malicious child's script in an admin's browser can call the admin API. v1 hardens the admin UI: `frame-ancestors 'none'`, `Cross-Origin-Opener-Policy: same-origin`, no CORS, `Sec-Fetch-Site` checks, and a strict CSP: `script-src 'self'` plus the SHA-256 of each page's inline scripts (spike 19), because a Next.js export cannot start without its inline bootstrap scripts. An optional per-app `Content-Security-Policy: sandbox` (without `allow-same-origin`) is not adopted in v1 and stays untried. Admin step-up confirmation is later.

## 8. Identities, secrets and resources

A child has **no Databricks identity and no secret unless one is configured**. Preference order (premise 1): OBO, then a service principal; personal access tokens are not in v1.

### 8.1 The three modes and delegation requests

| Mode | What the child receives | Allowed by | Level of effect | If the child is malware |
|---|---|---|---|---|
| **OBO** | the visitor's token in `X-Forwarded-Access-Token`, with the appmux app's scope set; stripped for other apps | `apps.obo_delegated`, set by a platform admin | per app allow or deny; the scope set is shared by every OBO app; the token is per visitor | acts as the visitor, within those scopes, while the visitor is on the page |
| **Shared SP** | a short-lived token of the appmux app's own service principal (never its client secret), in a file in the child's run directory | `apps.shared_sp_delegated`, set by a platform admin, and only while the master switch `shared_sp_delegation_allowed` is on | per app, but one identity shared by every app that has it and used by appmux for Lakebase | can use everything the appmux SP reaches ([8.4](#84-shared-sp-what-it-reaches-and-what-can-be-done)); the review is the control |
| **Own SP** | `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET` of a service principal that already exists | an app admin attaches its client id and secret as two `secret` resources and names them in `app_config` | per app | exactly what that SP was granted |

- **A delegation means "the platform admin allows this credential to be passed down to this app".** The app admin decides whether to *use* a credential the platform allows, never whether it is allowed. It is two flags on `apps`; who, when and why are in the audit log.
- **OBO works only inside a request**: a background thread has no visitor and no token. Work without a visitor needs an SP. **OBO scopes** are set per Databricks App, so every OBO child gets the same set and adding one changes all of them; the workspace's OAuth scope allowlist may block it. Keep the set minimal, but note that Lakebase does not check it (spike 1). This is the one deploy-time touchpoint with a workspace admin.
- **appmux never creates SPs** (Databricks lists that as a workspace or account admin action), so own SP depends on someone with that right, once per SP, outside appmux. A Databricks App's SP is created with the app and deleted with it, so only appmux has one; one Databricks App per child would spend the slots appmux saves.

**Asking for a delegation, inside appmux.** An app admin who needs OBO or the shared SP asks a platform admin on the app page; there is no side channel. A delegation request is a request of kind `delegation`, carrying `obo`, `shared_sp` or both, and the flow (the inbox, approve as one transaction, deny, withdraw) is the one of [9.4](#94-requests-to-a-platform-admin). What is particular to delegations:

1. The app page shows `OBO: not allowed` with a **Request** button. After approval OBO starts for the next request.
2. A request is refused for a flag the app already has, and a `shared_sp` request is refused while the master switch is off. The approval screen of a shared-SP request lists the exposure ([8.4](#84-shared-sp-what-it-reaches-and-what-can-be-done)).
3. A platform admin may also set a flag directly with no request (`delegation.grant`). Only a platform admin removes one (`delegation.revoke`, with a reason): OBO stops at the next request, a lent shared-SP token expires within its hour, and the app's resources stay, flagged on the app page.
4. Both flags are review gates, so approving a request that carries either needs a reason; a request carrying both is answered as a whole, and a platform admin who would allow only one denies it with a note and the app admin asks again. The gate's cost (premise 5) is unchanged — latency, one person's judgment, rubber-stamping — but the request puts the reason in front of the reviewer and in the log.

### 8.2 Resources and env entries

In a standalone App, `app.yaml` writes `valueFrom: <resource key>`; the key names a *resource* the app declares for itself (a secret, a SQL warehouse, an MLflow experiment, a Lakebase database, …), the platform fills in the value, and whoever adds it needs `CAN MANAGE` on both the resource and the app. A child inside appmux works the same way: **each app declares its own resources**. They are not a subset of a platform list and no platform admin chooses them. A child has no Databricks resource list, so appmux keeps one per app: `app_resources` ([data model 4.7](v1-data-model.md#47-app_resources)). Its rows have the shape of a real app's — **key**, **type**, target, permission — plus **who attached it** and **which identity the child uses it as**. Types follow Databricks' list, with `secret` holding a reference to a Databricks secret ([8.3](#83-secrets-where-the-value-comes-from)). The catalogue with permission levels is in the data model.

A resource gets in by one of two paths, which end at the same attach check:
- **From the child's `databricks.yml`**, as a standalone deploy would declare it. Update reads the file ([5.4](#54-appyaml-requirements-and-start)) and the Requirements panel and the resources screen offer each resource filled in; the app admin confirms or edits it.
- **By hand**, on the app page.

A `databricks.yml` fragment of `sales`, and the offers appmux builds from it (nothing is attached until Alice confirms):

```yaml
resources:
  apps:
    sales:
      resources:
        - name: sql-warehouse
          sql_warehouse: {id: a1b2c3d4e5f67890, permission: CAN_USE}
        - name: experiment
          experiment: {experiment_id: "456789012", permission: CAN_EDIT}
        - name: openai-api-key
          secret: {scope: sales-secrets, key: openai-api-key, permission: READ}
        - name: forecast-model
          serving_endpoint: {name: "${var.endpoint}", permission: CAN_QUERY}   # unresolved: Alice types the name
```

| Offer | Type | Target | Permission |
|---|---|---|---|
| `sql-warehouse` | `sql_warehouse` | `a1b2c3d4e5f67890` | `CAN_USE` |
| `experiment` | `mlflow_experiment` | `456789012` | `CAN_EDIT` |
| `openai-api-key` | `secret` | `sales-secrets/openai-api-key` | `READ` |
| `forecast-model` | `serving_endpoint` | *(to fill in)* | `CAN_QUERY` |

The file does not say *how the child reaches* each resource, so `access_as` below is chosen at attach.

The `sales` `app.yaml` (unchanged). A `secret` resolves to the secret's value; any other type resolves to its target string (the value Databricks gives `valueFrom` for that type), never to access:

```yaml
command: ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
env:
  - name: LOG_LEVEL
    value: info
  - name: OPENAI_API_KEY
    valueFrom: openai-api-key        # a secret: the child gets the value read from its Databricks scope
  - name: WAREHOUSE_ID
    valueFrom: sql-warehouse         # a SQL warehouse: the child gets the id; the visitor must hold CAN USE (obo)
  - name: MLFLOW_EXPERIMENT_ID
    valueFrom: experiment            # an MLflow experiment: the id; the app's own SP must hold the permission (own_sp)
```

Each resource except a secret says which identity the child uses it as (`access_as`):

| `access_as` | The child reaches it as | What must be true |
|---|---|---|
| `obo` | the visitor | the app has `obo_delegated`; each visitor has the rights |
| `own_sp` | its own existing service principal | the app has own-SP keys; someone with rights granted that SP the permission **outside appmux** |
| `shared_sp` | the appmux service principal | the app has `shared_sp_delegated`, and the appmux SP already has the rights |

**appmux never grants access to a resource.** In Databricks, adding a resource sets the app SP's permission; a child has no SP to set. appmux checks instead that **the person attaching it could use it themselves**. The **attach check** runs in that person's request with their own token, so no workspace admin and no SP grant is involved. It is the standalone rule too: declaring a resource, in a bundle or in the UI, needs the declarer to hold the permission.

1. `authorize(resource.attach)`: `CAN_MANAGE` on the app. The delegation or own-SP keys for the chosen `access_as` exist.
2. With the attacher's token, one read-only call per type shows they can reach the target (for a Lakebase database, a login with their OBO token; for a `secret`, `SELECT length(secret('<scope>', '<key>'))` on a SQL warehouse, which succeeds only with `READ` on the scope and returns the length, never the value). The calls, the OAuth scope each needs and the result when it fails are in [data model 4.7](v1-data-model.md#47-app_resources) (spike 16: 8 of 14 types verified). Where the response carries the caller's own permission level (the serving endpoint), appmux compares it with the requested one; elsewhere the call proves the attacher can reach the object, not which level they hold. Result `ok`; if no such call exists for the type (`mlflow_experiment`, `job`, `databricks_app` and the types with nothing to test) or the App's scopes do not cover it, `unverified`. The checks of `secret`, `sql_warehouse` and `uc_function` run a statement, so they need a SQL warehouse the attacher can use; the platform setting `attach_check_warehouse_id` names it.
3. A "no access" answer refuses the attach and writes nothing. `ok` and `unverified` are stored — **except that `shared_sp` accepts only `ok`**: the shared identity is the one case where a resource could be borrowed from someone with no right to it (a confused deputy), so it fails closed.

The check proves what the attacher could reach **at attach time** and is not repeated; at use, `obo` is decided by the visitor's rights and `own_sp` by that SP's grants. **When changes apply:** resources belong to the app, not to a release; an attached, changed or detached resource takes effect at the next start, and the app page shows "restart to apply".

**Env entries in a release.** `app.yaml` stays the source of names. A release's `env` can add or override a name without touching the child's source: a plain `value` (stored and shown in the UI, so never a secret; the UI warns when the name contains `KEY`, `TOKEN`, `SECRET` or `PASSWORD`) or a `value_from` naming a resource key. Secret values never appear in release config, the audit log or logs.

### 8.3 Secrets: where the value comes from

Databricks resolves a `valueFrom` secret by injecting it into the environment of *the App that owns the resource*. A child is not an App, so appmux gets the value another way: the `secret` resource names `scope/key`, and the appmux SP reads the value from Databricks when the child starts. The app admin attaches an existing secret like any other resource and types no value; the value stays in Databricks under its owner's access control and rotation, and nothing secret enters the registry.

Spike 11 (Free Edition) verified that an App's service principal, which is not a workspace admin, reads a value with only `READ` on the scope (REST `secrets/get`, and the SDK's `dbutils.secrets.get` and `secrets.get_secret`; the documentation's "`DBUtils` only" did not hold). The alternatives were a secret store inside appmux (the app admin types the value, appmux keeps it encrypted under a master key) and attaching every child secret to the appmux App; the first would have held every child's secrets and a key to protect, the second edits and restarts the appmux App for every new secret (constitution III) and an app admin cannot do it. Neither is planned.

**References.**
1. The app admin attaches a `secret` resource: the scope and the key, from the bundle offer or typed. Only the name is stored.
2. The attach check runs as the attacher: `SELECT length(secret('<scope>', '<key>'))` on a SQL warehouse with the app scope `sql`. It succeeds only if the attacher can read the secret and returns the length, never the value; a missing key fails like a missing `READ`. A list of the scope's keys would be the obvious check but fails: the Secrets API needs an OAuth scope (`secrets`) that no App accepts. The statement's HTTP status is 200 whether it succeeds or not, so appmux reads `status.state`.
3. When the reconciler starts a release, the appmux SP reads each `secret` named by `valueFrom` and passes it through that child's environment. Plaintext exists only in appmux's memory and that child's environment, which only that child can read; it is never logged or stored.
4. The appmux SP needs `READ` on the scope. Only the scope's manager can give it and appmux never does; without it the start fails: `cannot read secret 'sales-secrets/openai-api-key': ask the scope's manager to give the appmux service principal READ on scope 'sales-secrets'`. Use **one scope per app**, so each grant is visible and can be revoked alone.

The cost is a human step per scope (the grant) and a wider reach for the appmux SP, which [8.4](#84-shared-sp-what-it-reaches-and-what-can-be-done) accounts for. Secrets are **not versioned by release**: a rollback runs with the current values.

**Permission changes are not immediate.** A grant took effect on the first call after it. A revocation did not: a scope kept answering for 163 s after `READ` was removed, and another alternated between allowed and denied for about six minutes. So for a few minutes after a manager removes the appmux SP's `READ`, a child may still start with the secret, or a start may fail and then succeed. The grant to revoke first is the one the attach check relied on; to stop a leaked secret at once, rotate the secret itself.

### 8.4 Shared SP: what it reaches and what can be done

**The concern.** Lending the appmux SP's token hands a child *everything that SP can reach*. That includes every scope a manager has given the appmux SP `READ` on, so any shared-SP child can read every other child's secrets in those scopes: spike 11 confirmed that an App's SP reads values with its own credentials.

| # | Measure | Effect | Status |
|---|---|---|---|
| O1 | **Secret references:** the appmux SP reads only scopes whose managers chose to give it `READ`, one scope per app; the approval screen of a shared-SP delegation lists every scope a `secret` resource points to | the reach is visible, bounded by scope managers and revocable per scope (a revocation takes minutes to bite, [8.3](#83-secrets-where-the-value-comes-from)) | in v1 |
| O2 | ~~Fallback store~~ | would have kept child secrets inside appmux; dropped because spike 11 showed references work | not planned |
| O3 | Narrow the lent token's OAuth scopes per app (for example `sql` only) | shrinks the token to what the app needs: a `sql` token is refused the Secrets API, apps and the rest | **possible** (spike 10: the token request accepts a narrower scope and enforces it); **not in v1** (the plan left it out) (it needs a scope list per delegation, which the data model has no column for), and it does **not** stop a `sql` child reading every scope the appmux SP can read, through the SQL `secret()` function |
| O4 | Own SP | the child's reach is exactly what its admin granted | in v1; the answer for an app that cannot accept the shared exposure |
| O5 | **Pooled SP**: a platform admin attaches an existing SP that is not appmux's to a group of apps of equal trust | keeps "shared" convenience without appmux's own rights | **not in v1; open decision for the project owner** (someone must create the SP; every app using it shares its reach) |

**There is no complete fix while the lent identity is appmux's own SP**, because it must reach appmux's own resources. The remaining exposure: (1) the registry write rights of [4.2](#42-who-writes-what) — any app's grants, config, releases, resources (secret references) and requests, but not an `apps` row or a setting; the content store (tampering is caught by the digest check at start); (2) a shared-SP child can read every scope the appmux SP has been given (spike 11 showed an App's SP reads values); (3) everything else the appmux SP was ever granted.

So shared SP is **gated** (premise 5): `shared_sp_delegation_allowed` (default `false`, all children) must be on, and then a platform admin approves one app with a reason. The approval screen lists the three items above and, with references, the scopes the appmux SP can read. The cost: an app waits for an admin, one person's judgment decides, and a rubber-stamped approval gives the exposure to a child that did not need it. The mitigation is to make the safer alternatives cheap: OBO exposes only the visitor's own rights, and own SP needs no approval at all.

### 8.5 Choosing a mode and delivering credentials

| The app… | Use | Why |
|---|---|---|
| needs no Databricks API | no credential | nothing to leak |
| needs only an external API key | a `secret` resource | no identity needed |
| queries data **as the visitor** | OBO | reaches only what that visitor may |
| runs without a visitor (background work, MLflow tracking) | own SP | OBO has no token outside a request |
| must not share an identity with other apps or appmux | own SP | exact, auditable reach |
| is internal, low-risk, and the platform admin accepts the exposure | shared SP | no SP to create; costs the review and the exposure in 8.4 |

- **Own SP:** the three standard variables are set at start, the SDK refreshes its own tokens, so unmodified code works.
- **Shared SP:** the token lives one hour and the child never gets the client secret, so a static variable would expire (spike 10: it failed from the first call after expiry). appmux mints the token and writes it atomically to a file in the child's run directory, and sets `DATABRICKS_AUTH_TYPE=databricks-cli` and `DATABRICKS_CLI_PATH` to a small script that prints that file. The unmodified SDK then keeps working across expiry (spike 10: 15 of 15 calls). appmux re-mints when less than 10 minutes of life remain, because the SDK keeps a token until it expires and the replacement must already be in the file; the file's `expiry` is read as local time, not UTC. A local metadata endpoint (`DATABRICKS_METADATA_SERVICE_URL`) also worked but needs a network path from the child's own namespace to the supervisor, which was not tried, so the token command is the default.
- **OBO:** the token arrives per request in the header and is never stored.

## 9. Roles, permissions and config levels

### 9.1 The model

The model copies Databricks Apps ([reference](../reference/databricks-apps-platform.md)) and uses the users and groups Databricks already has.

| Concept | In appmux |
|---|---|
| **Permission levels** | `CAN_USE` (open the app) and `CAN_MANAGE` (manage settings, releases and permissions; includes `CAN_USE`) |
| **Principals** | a `user` (an email), a `group` (a **Databricks group**), `everyone` (all users Databricks lets into the slot; app grants only) or a `service_principal` (another Databricks App's service principal, named by its application id; app grants only, `CAN_USE` only, [ADR 0020](../decisions/0020-let-a-service-principal-hold-an-app-grant.md)) |
| **Resources** | each app, and the **platform** itself |
| **Who can grant** | only a `CAN_MANAGE` holder of that resource |
| **Owner** | each app's one `owner` is an undeletable `CAN_MANAGE` holder; only a platform admin changes it |

A **platform admin** is anyone listed in the `platform_admins` setting; an **app admin** is anyone with `CAN_MANAGE` on an app. There is no deny entry and no inheritance between apps, but **a platform admin holds `CAN_MANAGE` on every app implicitly**, for oversight and support: they can open it, read the source of its releases in a read-only viewer and change its configuration (environment entries, resources, sharing, releases, Update, Activate, Stop and Start). The access is not a row in `app_grants`, so it is not in the sharing list and cannot be removed from an app. It has two limits. Every write is in the audit log as always, and every source view writes `release.view_source`. And the platform admin's token reaches an app only by their choice, because that token can write platform data ([4.2](#42-who-writes-what)). When a platform admin who is neither the owner nor named by a grant opens an OBO-delegated app, the launcher offers two modes: **Open without token** (the default: the edge removes `X-Forwarded-Access-Token`, so the app still sees who is there from the email but anything it does as the visitor fails) and **Open as a user** (the token passes exactly as for a visitor with `CAN_USE`, and the app acts as that admin within the scopes). Switching to the second mode writes the audit row `app.view_as_user`; the choice is kept for that app in an edge cookie that the edge removes before forwarding, and the admin can switch back at any time. No `CAN_USE` grant is needed in either mode. For an app that is not OBO-delegated the token is stripped anyway, so there is one **Open**.

**Where a person's groups come from.** `identity` calls Databricks' SCIM endpoint for the current user (`GET /api/2.0/preview/scim/v2/Me`) with the visitor's own token; the response lists their groups. It needs only the identity scopes a user-authorized App has by default, so **no workspace admin and no SP grant is involved**.
- The result is cached per person for 300 s (a constant). If the call fails the last result is used for at most one hour; with none the person has no groups, which can only deny. A group change takes effect within the TTL plus the platform's own lag after the change (about 50 s after an addition and about 75 s after a removal, measured on Free Edition; below), a grant removal within seconds.
- The edge needs the token. If user authorization is off there is no token header, so group grants cannot match and only `user` and `everyone` grants work: fail closed. The token is stripped before a request reaches a child unless the app is OBO-delegated.
- `Me` lists groups reached through nesting too, typed `indirect` (spike 12, Free Edition: four levels deep, the user in the lowest), so a group grant matches **direct and indirect** memberships alike; appmux matches on the group's display name and ignores the type. Right after a membership change the platform's answers alternate between the old and the new state: about 50 s after an addition and, after a removal, stale answers kept coming for about 75 s. A fetch made inside that window can be cached for the whole TTL, so a missed addition denies for up to the TTL plus that lag and a missed removal still allows for as long. A group name no one's token reports has no effect; the admin screen says "no one has matched this group in the last 7 days" when so.

**Effective permission**, computed on the in-memory data and the actor's cached groups:

```
principals(actor)      = { user:<email> } ∪ { group:g for every g in actor.groups } ∪ { everyone:* }
                         ∪ { service_principal:<email> }                      (the forwarded id, ADR 0020)
platform(actor)        = CAN_MANAGE if any entry of platform_admins ∈ principals(actor), else none
permission(actor, app) = CAN_MANAGE if actor is the owner of app
                         else CAN_MANAGE if platform(actor) = CAN_MANAGE      (implicit: no row)
                         else the highest permission among app_grants(app) rows whose principal ∈ principals(actor)
                              (an everyone or service_principal row counts as CAN_USE whatever it stores)
                         else none
```

**A service principal as a visitor.** When another Databricks App calls a child through appmux with its own service principal's token, the Apps proxy puts that service principal's application id (a UUID) in `X-Forwarded-Email` ([reference](../reference/mcp-and-a2a.md)). A `service_principal` grant names that id in lower case and matches it in any case; it needs no groups, so it works with user authorization off. It is `CAN_USE` only: the schema and `authorize` refuse `CAN_MANAGE`, and a stored `CAN_MANAGE` counts as `CAN_USE`, as for `everyone`. An id that no grant names gets the same `404` as an unknown app. A service principal cannot be a platform admin.

For example Bob resolves to `CAN_USE` on `sales` because Databricks reports `sales-team` among Bob's groups; Carol to `CAN_MANAGE` through a user grant that outranks the group's `CAN_USE`; Dana, a platform admin, to `CAN_MANAGE` on `sales` although no row names Dana, because every platform admin holds it implicitly ([9.2](#92-who-may-do-what)).

**Revocation** applies to new requests within seconds for grant rows and within the cache TTL for group membership; WebSockets of a user who lost access are closed on the same refresh.

**Bootstrap.** The first platform admin cannot be created through the UI. The deploy-time variable `APPMUX_BOOTSTRAP_ADMINS` (a list of emails) is honoured **only while the `platform_admins` setting is unset**. Whoever can redeploy the appmux App can set it, which is accepted: that person already controls the slot. **Last-admin guard:** a change that would leave no `user` entry in `platform_admins` is refused, because appmux cannot count the members of a Databricks group. **Break-glass:** if the guard was bypassed, the Lakebase project owner edits `platform_settings` with SQL and writes the `audit_log` row by hand, with `db:<role>` as the actor so the intervention is visible.

### 9.2 Who may do what

All checks go through `authorize(actor, action, app)`, driven by a table and unit-tested as a table.

| Action | Needs | Notes |
|---|---|---|
| `app.open` | `CAN_USE` on the app | the launcher lists only apps the actor can open; a platform admin can open every app (implicit `CAN_MANAGE`, [9.1](#91-the-model)), without the visitor's token by default or as a user by choice |
| `app.view_as_user` | platform admin, with no grant on an OBO-delegated app | switches the admin's way of opening that app to "as a user"; writes an audit row; the token reaches the app until the admin switches back |
| view an app's page: status, releases, Requirements, build-log tail, metrics | `CAN_MANAGE` on the app | a platform admin included |
| `release.view_source` | `CAN_MANAGE` on the app | the read-only viewer of a release's snapshot files (text files only); each view writes an audit row; this is how a platform admin reads any app's source |
| `app.view_logs` | `CAN_MANAGE` on the app | the app's recent output from an in-memory ring, masked as the build-log tail; the first read by an actor in 10 minutes writes an audit row |
| `app.view_environment` | `CAN_MANAGE` on the app | the runtime versions, variables and packages the app is started with; a secret's value is never shown; no audit row |
| `app.update`, `release.activate`, `app.restart`, `app.stop`, `app.start` | `CAN_MANAGE` on the app | `app.update` and the wheel fetch use the presser's own token |
| `release.label`, `release.tag`, `release.untag` | `CAN_MANAGE` on the app | labels only; they protect nothing |
| `release.lock`, `release.unlock` | `CAN_MANAGE` on the app | `release.lock` is refused once the app already holds `max_locked_releases` locks |
| `release.delete` | `CAN_MANAGE` on the app | the active, target, in-progress and locked releases cannot be deleted |
| `config.edit`, `resource.attach`, `resource.detach` | `CAN_MANAGE` on the app | `resource.attach` runs the attach check with the person's own token; a `secret` is attached as a reference, so no value passes through appmux |
| `grant.app` | `CAN_MANAGE` on the app | the owner cannot be removed |
| `request.create`, `request.withdraw` (every kind but `register`) | `CAN_MANAGE` on the app | a reason is required; the kind must be in the catalogue; refused if the app already has every value asked, a request of that kind is pending, a slug is taken, or `shared_sp` is off ([9.4](#94-requests-to-a-platform-admin)) |
| `request.create`, `request.withdraw` of kind `register` | any visitor of appmux | no app exists yet, so no app permission is checked; a reason is required; refused if an app or a pending request has the slug, or the asker already has 3 pending; only the asker withdraws |
| `request.decide` (approve, deny) | `CAN_MANAGE` on the platform | approving a request that holds a gate field (`register`; `source_path` in `settings`; `obo` or `shared_sp` in `delegation`) needs a reason; a taken slug closes the request as denied |
| `delegation.grant`, `delegation.revoke` | `CAN_MANAGE` on the platform | grant is a review gate and needs a reason; revoke records one |
| `app.register`, `app.delete`, `app.set_owner`, `app.edit_platform_fields` | `CAN_MANAGE` on the platform | the fields are `slug` (a change also writes `app.rename`), `display_name`, `source_path` and the budget; `app.register` is the direct path, and the same registration by request goes through `request.decide` |
| `platform_admin.add`, `platform_admin.remove`, `settings.edit`, `egress.edit`; the platform page and all metrics | `CAN_MANAGE` on the platform | `egress.edit` is not a gate; it covers an app's own list and the global list |
| `workspace.browse` | a platform admin or `CAN_MANAGE` on at least one app | lists one workspace folder with the visitor's own token (the folder browser beside the source folder field); writes nothing and records nothing; the route needs the token, so it answers `403` `user_token_missing` without one. Not open to every visitor, because a child's page script runs with the visitor's identity ([7.3](#73-same-origin-limit-accepted-in-v1)); a person who manages no app types the folder path by hand |
| `resource.suggest` | a platform admin, or `CAN_MANAGE` on at least one app | the catalogue of resource types and the objects of a type the person can see, read with their own token (the Add resource form); it reads, writes and records nothing |

| Actor | Action | App | Expected |
|---|---|---|---|
| bob | `app.open` | `sales` | allow |
| bob | `release.activate` | `sales` | deny |
| carol | `release.activate` | `sales` | allow |
| carol | `release.delete` of the active release | `sales` | deny (protected) |
| carol | `release.delete` of a locked release | `sales` | deny until unlocked |
| carol | `release.lock` when `sales` already holds 3 locks | `sales` | deny (cap) |
| alice | `request.create` (`delegation`: `obo`) | `sales` | allow, reason required |
| carol | `request.create` (`settings`: `source_path`) | `sales` | allow, reason required |
| mallory (no grants) | `request.create` (`settings`: `display_name`) | `sales` | deny (needs `CAN_MANAGE`) |
| alice | `request.decide` | `sales` | deny (platform) |
| alice | `grant.app` removing the owner (alice) | `sales` | deny (owner) |
| dana | `request.decide` (`delegation`: `obo`) | `sales` | allow, reason required |
| dana | `request.decide` (`settings`: `display_name`, `memory_budget_mb`) | `docs` | allow, a note is optional |
| dana | `request.decide` (`settings`: `slug`, taken by another app) | `docs` | closes the request as denied (automatic) |
| grace | `request.create` (`register`) | none yet | allow, reason required |
| grace | `request.create` (`register`) with 3 already pending | none yet | deny (cap) |
| alice | `request.withdraw` of grace's `register` request | none yet | deny (only the asker) |
| dana | `request.decide` (`register`) | none yet | allow, reason required |
| dana | `app.open` of an OBO app, named by no grant | `sales` | allow (implicit `CAN_MANAGE`), token header removed |
| dana | `app.view_as_user` of an OBO app, named by no grant | `sales` | allow, audit row, token header passes |
| dana | `app.view_as_user` of an app that is not OBO-delegated | `docs` | no such mode: one **Open**, token header removed |
| dana | `release.view_source` | `sales` | allow, audit row |
| bob | `release.view_source` | `sales` | deny |
| mallory (no grants) | `app.open` | `sales` | deny (default) |
| a user whose group lookup failed, no cache | `app.open` on a group-shared app | `sales` | deny (fail closed) |

### 9.3 Config levels and review gates

Rule: anything that widens a child's reach, affects other apps or changes what every visitor sees (an app's name and URL) is a platform admin's; an app admin brings credentials they already hold and chooses within what the platform allows.

| Level of effect | Config | Who changes it |
|---|---|---|
| **All children** | the `platform_settings` keys (`platform_admins`, `memory_reserve_mb`, `package_mode`, `default_wheel_sources`, `shared_sp_delegation_allowed`, `max_releases_per_app`, `max_locked_releases`, `egress_global`), the appmux App's OBO scope set and the workspace allowlist behind it | platform admin, with an audit entry |
| **One child, platform-controlled** | `slug`, `display_name`, `source_path`, `memory_budget_mb`, its **delegations** (`obo_delegated`, `shared_sp_delegated`), that app's egress rules, `owner` | platform admin; the delegations and a new `source_path` are review gates; an app admin can ask for any of them except the egress rules and `owner` ([9.4](#94-requests-to-a-platform-admin)) |
| **One child, app-controlled** | env entries, wheel sources, the health check (`health_path`, `health_deadline_s`), the app's own resources and secret references, own-SP keys, `app_grants`, release names, tags, locks and deletion, requests to a platform admin, Update, Activate, restart, stop and start | `CAN_MANAGE` holder of that app, and any platform admin (implicit, [9.1](#91-the-model)) |
| **One visitor** | what an OBO child can read is that visitor's own permission | nobody; the data owner's grants decide |

The appmux SP's own Databricks grants (a warehouse, a table) sit outside this table because they reach every shared-SP child at once; granting one to the appmux SP is a decision about all of them.

Three **review gates**, kept few on purpose because every extra gate makes the real ones easier to rubber-stamp:

| Gate | Reviewer judges | Cost |
|---|---|---|
| Register an app (directly, or by approving a `register` request), or approve a new `source_path` (`source_path`, budget) | the source is one the team trusts to run; the budget fits | an app waits for an admin; one person's judgment; the inbox takes any visitor's ask |
| OBO delegation | the app needs the visitor's identity; the scope set is acceptable | latency; a rubber-stamped delegation lets the child act as each visitor |
| Shared-SP delegation (only while `shared_sp_delegation_allowed` is on) | the exposure in [8.4](#84-shared-sp-what-it-reaches-and-what-can-be-done) is acceptable for this app | as above, with the largest exposure |

Requests are not gates; a request field is one only if approving it passes a risk the platform cannot remove ([9.4](#94-requests-to-a-platform-admin)). The gate fields are the whole `register` request and `source_path` (the first gate), and `obo` and `shared_sp` in a `delegation` request. A name, a slug and a budget are decided by a platform admin because they touch what is shared, but they are not gates: a wrong approval is visible and can be undone by another edit. Egress is a platform admin's own edit (`egress.edit`), audited, neither a request nor a gate. Attaching a resource is not a gate: the attach check proves the attacher's own rights and the delegations limit the identity.

### 9.4 Requests to a platform admin

[9.3](#93-config-levels-and-review-gates) lists what only a platform admin may do: register an app, and change what is platform-controlled on one. A person does not need a side channel to ask for either, because a side channel leaves no record and gives the reviewer no context. They ask inside appmux, with a reason, and a platform admin answers in an inbox. This is the one request mechanism: the delegations of [8.1](#81-the-three-modes-and-delegation-requests) and the registration of a new app are kinds of it.

**The catalogue.** Three kinds, one per functional group: a new app, a credential passed down, and the settings that other apps or visitors can feel. A request carries one or more fields of its kind with one reason, so a new name and a new budget can travel together. Nothing else can be requested in v1. Each kind is an entry in code (its fields, who may ask, the change it makes, and for each field whether it is a gate), so a new field or kind needs no schema change ([data model 4.3](v1-data-model.md#43-permission_requests)).

| Kind | Asked by | Fields in `payload` | Approval does | Applies | Gate |
|---|---|---|---|---|---|
| `register` | any visitor of appmux | `slug`, `display_name`, `source_path`, `memory_budget_mb` (all four) | creates the app: owner = the asker, stopped, no release, every other field at its default | at once; nothing runs until an app admin presses Update | yes: the register gate |
| `delegation` | an app admin | `obo`, `shared_sp`: one or both, each `true` | sets `apps.obo_delegated` and `apps.shared_sp_delegated` as asked; `shared_sp` is refused while the master switch is off | from the next request to the app; from the next token lent | yes, for each field |
| `settings` | an app admin | one or more of `slug`, `display_name`, `source_path`, `memory_budget_mb` | sets those columns of `apps` | `display_name` at once; `slug` through a restart under the new prefix ([5.5](#55-health-and-swap)); `source_path` at the next Update; `memory_budget_mb` from the app's next start or update, when admission control decides ([5.7](#57-memory-and-admission-control)) | yes when `source_path` is asked: the register gate again; otherwise no |

**Why these fields.** A change needs a platform admin when it touches something that other apps or visitors share. `display_name` is read by every visitor on the launcher, so a misleading name misleads all of them. `slug` is the slot's URL space. `memory_budget_mb` is a share of the slot's memory. `source_path` decides what code runs in the container. A delegation lends an identity to code. What touches only the app itself (environment entries, the health check, wheel sources, resources, sharing, releases) the app admin changes alone. The `owner` and the egress rules are a platform admin's own edit with no request: who answers for an app, and where its code may connect, are the platform admin's judgment from the start.

`source_path` is the workspace folder an app's source is read from. An app admin asks for a new one when the code moves (for example from `/Workspace/Shared/apps/sales` to `/Workspace/Shared/apps/sales-v2`). Nothing is read until the next Update, which reads the folder with the presser's own token. It is a gate because the folder decides what code runs in the slot. `slug` is the URL prefix: a rename moves every link to the app, so the new slug must pass the form check of a registration and be free.

**How a request runs** (every kind but `register`, whose front door is below).

1. The app page shows a **Settings** form (name, slug, source folder, memory budget) and, for OBO and the shared SP, `not allowed` with a **Request** button. The app admin changes one or more fields, or picks a delegation, and enters a reason (up to 500 characters). appmux checks every value with the same code that checks a platform admin's direct edit, leaves out the fields that did not change, refuses a request with none left, inserts the request (`pending`) and writes the audit row `request.create`. The page then shows `requested, waiting for a platform admin` with a **Withdraw** button.
2. Platform admins see every pending request in an **inbox** on the platform page, oldest first, and a pending count on the launcher. A row shows the app, the kind, each field's current value next to the requested one, who asked, how long ago and the reason. A platform admin presses **Approve** or **Deny**. Approving a request that holds a gate field needs a reason; the others take an optional note.
3. **Approve is one transaction** through the platform admin's own role: lock the app's `apps` row, check that the request is still valid (the payload, the requester's rights, for `shared_sp` the master switch, for a `slug` that no app has it), apply the change, close the request and write `request.approve` with the reason and the values before and after (a rename also writes `app.rename` with the old and new slug). If a check fails nothing is written and the screen says why, with one exception: **a taken slug closes the request as `denied` at once**, with the note `slug 'reports' is already taken (automatic)`, the audit row `request.deny` marked automatic, and the admin who pressed Approve recorded as closing it. A taken slug cannot become free by waiting, while the other failures can be fixed and **Approve** pressed again. Deny closes the request (`request.deny`). Withdraw is allowed only while pending (`request.withdraw`).
4. A platform admin may still make any of these changes directly (`app.register`, `app.edit_platform_fields`, `delegation.grant`, `app.set_owner`), with no request.

**Asking for a new app (`register`).** There is no app page yet, so every visitor of appmux sees a **Request a new app** button on the launcher. They enter the slug, a display name, the source folder, a memory budget and a reason. appmux checks the values with the code that checks a direct registration (the slug's form and that no app has it, the folder's form, the budget's range), inserts the request with no app, and writes `request.create`. The launcher lists the visitor's own requests, newest first, with their status, the reviewer's note when denied, and a **Withdraw** button while pending. In the inbox a platform admin sees the proposed app instead of a current value, with the free memory next to the budget, and presses **Approve** (a reason is required) or **Deny**. Approval is the transaction of step 3 with a different change: the check is that the request is still pending, the payload is still valid and no app has the slug (a taken slug is the automatic denial of step 3); the change inserts the `apps` row, owned by the asker, and its stopped `app_config` row, and links the request to the new app. The asker then finds the app on the launcher with `CAN_MANAGE` and presses Update. The budget counts against the slot only while the app runs, and admission control decides then.

**Rules.**

- At most one pending request per (app, kind); for `register`, per proposed slug. To change a pending request, withdraw it and ask again. A field equal to the app's current value is refused, and so is a `register` for a slug an app already has; if every field becomes true while the request waits (a direct change), approving only closes it. A `register` and a rename may name the same free slug: the first one approved takes it and the other is denied automatically.
- One person may have at most 3 pending `register` requests (a constant in the catalogue entry), so a single account cannot fill the inbox.
- Approval applies exactly what was asked, every field or none. To grant less, deny with a note naming what would be approved; the asker asks again with fewer fields.
- **A request grants nothing.** Authorization and the reconciler read only `apps`, so a forged `approved` status or payload changes nothing.
- A person who is both the asker and a platform admin may approve their own request; the audit row shows both names (a second-person rule would lock out a team with one platform admin). There is no notification outside the UI in v1: the asker sees the answer on the app page, or on the launcher for `register`.

**What the premises say.**

- *Premise 1:* a platform admin answers every request, and need not be a workspace admin. Anyone who can reach appmux can ask, so no platform admin has to type a registration in.
- *Premise 2 (malware test):* a child holding the shared SP can write requests of any kind, in any person's name ([4.2](#42-who-writes-what)), and so put a convincing request in front of a platform admin. Nothing changes until a person approves, and the approval screen shows each current value next to the requested one, the asker and the reason. This exposure already existed for the two delegations; the catalogue widens only what a rubber-stamped approval can do, which is why `delete` is left out (below). A forged `register` could give a person an app they never asked for; registering runs nothing by itself, and the screen shows the asker, the folder, the budget and the reason. A forged `settings` request can only propose a name, a slug, a folder or a budget; a look-alike name or slug is what the reviewer is there to catch. A child can also fill the inbox with `register` requests under invented names, which the per-person cap does not stop: the cost is a reviewer's clicks.
- *Premises 3 and 4:* each kind changes one app (or creates one) and never a setting or another app. A budget raise uses memory other apps could have used, and admission control still refuses what does not fit, so an approval cannot overcommit the slot or interrupt a running app. A rename moves only that app's route, and a taken slug is refused, never taken over.
- *Premise 5:* requests add a channel, not a gate. The gates stay three: registering an app (which also covers a new `source_path`, because the reviewer's judgment is the same: the source is one the team trusts to run), OBO and the shared SP. Their cost is unchanged: latency, one person's judgment, rubber-stamping. A `delegation` request carries two gates in one form, but the approval screen lists the exposure of each flag. Registering by request adds one cost: anyone may ask, so the inbox carries asks a platform admin would never have made; the reason (up to 500 characters) and the cap keep the reading short. The other fields (name, slug, budget) ask the app admin for a reason but not the reviewer: a platform admin could already make each change directly, a wrong approval is visible and can be undone by another edit, and a mandatory reason on every approval teaches reviewers to type filler.

**Not requests in v1.** Deleting an app is left out: it is rare and destructive, and a forged request would hurt most there, so it is confirmed with the owner outside the tool. A new owner and an app's egress rules are a platform admin's own edit, as above. The platform settings and anything that affects all children stay a platform admin's own business. A user asking an app admin for access runs in the other direction and is beyond v1 ([15](#15-beyond-v1)). **Adding a field or a kind** later is one catalogue entry and its tests; it states its level of effect and who may ask, and it is a gate only if approving it passes a risk the platform cannot remove.

## 10. Admin UI and metrics

### 10.1 Screens

A Next.js application built as a static export and served by the edge, which also serves the JSON API the pages poll every few seconds, under a strict CSP: `script-src 'self'` plus the SHA-256 of each page's inline scripts (spike 19), which the edge computes from the export's HTML files. The export is built before deploy and ships with the appmux source, so the parent App needs no Node build and runs no `npm install`; the platform would build a `package.json` at deploy if asked (spike 19, [brief](v1-design-brief.md#open-questions)). This is a lean, not an ADR. There is no audit-log screen in v1; the log is read in the database.

| Screen | Who sees it | Content |
|---|---|---|
| **Launcher** (`/`, also `/_appmux/`) | everyone | the apps the visitor may open (a platform admin sees all) as one compact table sorted by name, with its search, **Open** and **Manage** rules below; **Request a new app** as a quiet button in the header; the visitor's own requests, only when there is one; platform admins also see a notice with the pending-request count that links to the inbox |
| **App page** (five sub-pages: Status, Releases, Resources, Sharing, Requests; [ADR 0017](decisions/0017-split-the-admin-ui-into-sub-pages-and-use-databricks-red.md)) | `CAN_MANAGE` on the app | status, detected runtime, releases (status, names, tags, a lock mark on the active and the manually locked ones), **Update**, **Activate** (any release), **Lock** and **Unlock**, delete release, Restart, Stop/Start; the **Requirements** panel (with the `databricks.yml` offers) and the app's resources, secrets as references, with who attached each; a **Settings** form (name, slug, source folder, memory budget) and the delegation status, sent as requests with **Request change**/**Withdraw**; the owner and the egress rules, read-only; the health check, env entries, wheel sources, own-SP keys; sharing; a **Files** view of each release's snapshot; build-log tail; the app's metrics |
| **Platform page** (five sub-pages: Overview, Apps, Access, Egress, Settings; [ADR 0017](decisions/0017-split-the-admin-ui-into-sub-pages-and-use-databricks-red.md)) | `CAN_MANAGE` on the platform | register an app directly; per-app name, slug, source folder, budget and owner; the **inbox** of requests; delegations; global and per-app egress rules; platform admins; settings; the `self_check()` result; memory used against the slot limit |
| **Dashboard** | platform admins (all apps), app admins (their apps) | [10.2](#102-metrics) |

The app and platform screens are each split into sub-pages, one route per page, behind a tab bar; the content of every row above is unchanged, only where it sits:

- **App**: *Status* (state, active release, runtime, budget, notices that need attention, Open, Update, Stop/Start, Restart), *Releases* (the release table and the source viewer), *Resources* (the requirements panel and the attached resources), *Sharing* (grants), *Requests* (the app's settings and delegation requests to a platform admin). The *Status* tab carries the number of notices that need attention and *Requests* the number waiting, so a badge always means something to do.
- **Platform**: *Overview* (the inbox first, then memory against the slot limit and the `self_check()` result; its tab carries the number of pending requests), *Apps* (per-app rows and **Register an app**), *Access* (platform admins and delegations), *Egress* (global rules), *Settings* (the thirteen settings in five groups with jump links, one row each, saved together from one sticky bar).

Two behaviours differ from the text of [9.4](#94-requests-to-a-platform-admin) and of FR-041, FR-042 and FR-051: **Deny** in the inbox now asks first, as Delete and Detach do, because a denial is final for the asker; and a self-check that passed shows its twelve rows folded (they open on their own when any check fails or none ran). Neither changes who may do what, the audit rows or the API.

**The launcher.** The page a normal user lands on has to stay quick to scan when the slot hosts about 100 apps, so it is one table, not a card or a section per app:

- **Table.** Columns *Name* (the display name, with the slug in monospace under it), *State* and *Actions*, sorted by display name with numbers in order (*App 2* before *App 10*). On a phone each row stacks.
- **Search.** A search box on name and slug appears from 8 apps and not before; a poll that shrinks the list below 8 drops the filter.
- **Actions.** *Open* where the app can be opened. For an OBO-delegated app with no grant on it, *Open* is split into *Open as a user* (which asks first) and *Open without token*, the main one last ([9.1](#91-the-model)). An app that cannot be opened says why in the place of *Open*, in plain words: "Not running. Ask the app admin to start it." for a visitor, "Not running. Start it from Manage." for a person who manages it, and "Starting. Open appears once its health check passes." *Manage* appears only on an app the viewer manages.
- **The two ways to open.** A closed "What is the difference?" disclosure above the table explains *without token* and *as a user*, shown only when at least one listed app offers both.
- **Notices and requests.** The "N requests are waiting for a decision" notice is shown to platform admins only, and only when N is above zero. "Your requests" appears only when the visitor has made at least one, newest first, with **Withdraw** on the pending ones.

Every behaviour of the earlier launcher stays reachable; what changed is how much of it a person who only wants to open an app has to read.

**Platform > Settings.** A dense list: one row per setting (its key, what it does, its stored value and the control), grouped into the five groups. Editing a row marks it *Changed*; one bar sticks to the bottom of the screen while anything is changed, saying how many rows are changed, with **Discard** and **Save changes** (primary, last). The admin API has one write, `PUT /platform/settings/{key}`, so **Save changes** sends one call per changed row, one after the other, and reports "2 of 3 saved, 1 failed": a failed call does not stop the others (except that the second of the two release limits is not sent when the first failed, as the server would refuse it), and a failed row stays marked, keeps what was typed and shows its own error. The settings carry no version of their own in the contract, so two admins saving the same key is last-write-wins; the page compares each row with the value it started from, and a row whose stored value moved meanwhile says "Another admin changed this to X while you were editing. Saving replaces it with your value." instead of overwriting silently. Leaving the page with unsaved rows asks first.

**Memory.** The memory section draws the slot as a flat strip of 20 square cells (each 5% of the scale), counted rather than measured so no width needs an inline style: outlined squares are what appmux reserves for itself, filled squares are the budgets committed to apps, pale squares are free. The figures and a percentage are printed beside it, so the fill is never colour alone. Past 75% of the apps' share the committed squares take the warning colour and the label reads *Getting full*, past 90% the danger colour and *Almost full*; these are display thresholds, and admission control ([5.7](#57-memory-and-admission-control)) refuses what does not fit at any fill.

```
Memory   □□□ ■■■■■■■■■■■■■■■ ░░     86% committed ▲ Getting full
         □ reserved for appmux 1,024 MB   ■ committed to apps 6,200 MB   ░ free 968 MB
Apps     10 registered · 10 running · 0 crashed · 1 rejected in the last hour
self_check   ok · 13 checks · 840 ms · 08:00:03
```

**One set of layout rules.** Every form, table and row of buttons follows the same few rules, written once in [`web/README.md`](../../web/README.md#writing-a-page) and decided in [ADR 0018](decisions/0018-settle-the-admin-ui-layout-rules-and-give-settings-one-save-bar.md): a field is a label, the control, then its hint or error *below* the control; every control is the same height; in any row of buttons the primary one is last; spacing between things is 1, 2, 4 or 6 steps; a table stacks on a phone, names its row in the first column and puts its *Actions* last.

### 10.2 Metrics

One-minute aggregates per app are collected in memory and flushed to Lakebase in **one batch per minute**; they are kept 7 days ([data model 8](v1-data-model.md#8-retention-and-volume)).

| Metric (column) | Unit | What it is |
|---|---|---|
| `requests`, `r2xx`, `r3xx`, `r4xx`, `r5xx` | count | requests by status class at the edge |
| `upstream_errors` | count | edge-generated 502/504 (child unreachable, timeout): "the child failed" vs "the child answered 500" |
| `limited` | count | refused by the per-app concurrency, body-size or rate caps (429, 413) |
| `lat_hist` | 11 counts | latency histogram, upper bounds in ms: 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, above; histograms add up across minutes, percentiles do not |
| `bytes_in`, `bytes_out`, `in_flight_max`, `ws_opened`, `ws_open` | bytes, count | traffic, peak concurrency, WebSockets |
| `rss_mb_max`, `restarts` | MB, count | peak resident memory of the child's process tree; restarts in the minute |
| `egress_bytes_out`, `egress_bytes_in`, `egress_dropped`, `egress_dropped_by_rule` | bytes, count, JSON | through the pump; drops by rule id (spots a child probing blocked destinations) |

Platform-wide rows (`app = _platform`) add `mem_used_mb`, `mem_limit_mb`, `children_running`, `children_registered`, `admission_rejections`, `registry_errors`, `registry_ms_max` and `edge_rss_mb`.

Where the figures come from (`metrics/sources.py`, sampled every 15 s): `mem_used_mb` from the container's cgroup (v2 `memory.current`, else v1 `memory.usage_in_bytes`, which Free Edition has; both count the page cache), `mem_limit_mb` from the reader admission control uses ([5.7](#57-memory-and-admission-control)), `edge_rss_mb` from appmux's own `/proc/self/statm`, `children_running` from the reconciler and `children_registered` from the registry. A child's `rss_mb_max` is the largest sum of resident memory over the process tree under its sandbox seen in the minute; the egress columns are what the pump's running totals moved. A figure no file states is left out, never guessed: a minute without `mem_used_mb` stores no `_platform` row, and a child whose tree cannot be read adds no `rss_mb_max`. `restarts` counts each start after a crash and each Restart (an Update is not one), `admission_rejections` each start refused for memory, and `registry_errors` and `registry_ms_max` every registry call (a refused admin write, such as a duplicate name, is not an error). The dashboard derives rates from the counts and reads a percentile off the running totals of `lat_hist` (for example a minute with 412 requests and totals 18, 79, 219, … puts p50 in the bucket ending at 25 ms), reported as that bucket's upper bound, so percentiles are coarse by design. Child logs are not metrics.

Reading them back (`GET /metrics`, `GET /metrics/breakdown`; [contract](../../specs/001-appmux-v1/contracts/admin-api.md#addendum-routes-and-fields-for-the-admin-ui-overhaul-t187-onward)): a window of up to the 7 days kept, provided it holds at most 1440 steps, at a step of 1, 5 or 15 minutes or an hour (24 h at the minute, 5 days at 5 minutes, the whole week at 15 minutes). Over a step the counts and histograms add up, the peaks (`*_max`, `mem_used_mb`, `edge_rss_mb`) take their maximum, and the gauges (`ws_open`, `mem_limit_mb`, the two child counts) the value of the step's last minute; steps start on whole multiples of their length in UTC, so a chart's buckets do not move with the window (`registry/metric_steps.py` holds the rules once, for the SQL and the tests). The breakdown ranks the apps by requests in the window and sums all but the top ones into one `other` series, so the platform view shows which app the traffic and memory belong to.

## 11. Failure behaviour

| Failure | Effect on other apps | What is shown | Recovery |
|---|---|---|---|
| `app.yaml` invalid (both files, a bad `env` entry) | none | the Update is refused, naming the problem; nothing is created | fix the file and press Update |
| `valueFrom` keys are not resources of the app, or a `secret` has no value | none | release `failed` before anything starts: `missing resources: openai-api-key, sql-warehouse` (all at once) | attach them (Requirements panel), press Activate |
| Build fails (compile error, missing wheel such as `no wheel for 'foo' (sha256 3b1e…) in the wheel sources`) | none | release `failed` with reason and build-log tail; the running release keeps serving | fix and press Update |
| Health check does not pass within `health_deadline_s` | none | release `failed`: `health check did not pass within 60 s (last error: connection refused)` | fix, raise the deadline (the app admin, up to 600 s), press Update |
| A renamed app's new copy fails its health check | none | the old copy and route keep serving; the app page shows the failed check; the row already holds the new slug | fix and press Restart; or a platform admin sets the old slug back |
| Attach check fails | none | refused, nothing written: `you do not have access to this SQL warehouse`, `cannot verify access to this resource type; shared_sp requires a verified check`, or the delegation it needs is missing | get access, pick another `access_as`, or request the delegation |
| Request refused | none | nothing written: a request of that kind is already pending, the app already has every value asked, a value is invalid (the message names it), `shared_sp` is off, or a slug belongs to an app (or, for a `register`, to another pending `register`), or the asker has 3 pending `register` | wait, or ask in the existing request; fix the value or the slug |
| Approval fails its check | none | nothing written; the screen says why (the requester lost `CAN_MANAGE`, a value no longer passes, `shared_sp` was switched off) | fix the cause and approve again, or deny the request |
| Approval finds the slug taken (`register` or rename) | none | the request is closed `denied` at once, note `slug 'reports' is already taken (automatic)`; audit `request.deny` marked automatic | the asker asks again with another slug |
| Update would exceed the release cap and every release is protected | none | refused: `app 'sales' has 10 releases and all are protected (active, target or locked); unlock one` | unlock a release |
| Locking would exceed `max_locked_releases` | none | refused: `app 'sales' already has 3 locked releases; unlock one first` | unlock one, or a platform admin raises the cap |
| A requirement the app environment cannot satisfy has no wheel in the wheel sources; or the app environment no longer provides a pin it used to | none | the Update fails, or the release is `failed` before it starts, listing every requirement: `no wheel for 'foo==2.0' (the environment has foo 1.0)` | add the wheels and press Update |
| The appmux SP cannot read a referenced secret | none | release `failed` before it starts: `cannot read secret 'sales-secrets/openai-api-key': ask the scope's manager to give the appmux service principal READ on scope 'sales-secrets'` | the scope's manager grants `READ`; press Activate |
| Update does not fit the memory budget | none | refused: `needs 2061 MB, 1393 MB free` | stop another app or raise the slot's compute |
| Source or wheel folder unreadable, or the presser's token expires during Update | none | the Update fails and nothing changes: the presser lacks read access or the folder is empty (the API answers an empty listing or a 404 to a caller with no access, spike 15), or `session expired during the snapshot; press Update again` | the folder owner grants read; press Update again |
| Snapshot digest does not match at start | none | release `failed`: `snapshot sha256 mismatch` | press Update to take a fresh snapshot |
| A person's group lookup fails | that person only | cached groups for up to an hour; with none, group grants do not match (logged) | restore the Databricks call |
| Child crashes | none | restart with backoff, then `crashed` and 503 for that app only | activate an older release or fix and Update |
| Child exceeds its memory | none | best-effort watchdog stops that app; `RLIMIT_AS` fails its allocations | raise the budget or fix the leak |
| Child's pump or slirp dies | none | no egress for that child; treated as a crash | restart the app |
| Shared-SP token cannot be minted | the child gets 401s | logged | check the appmux SP's credentials; own SP is unaffected |
| Process still starting (port bound, registry or `self_check()` not finished) | all: no route opens | `/` answers `200` "starting", every other path `503` | wait; if it does not end, read the log |
| `self_check()` fails | all: no route opens | status page | fix the cause; restart |
| Lakebase down while running / at startup | requests keep flowing from memory / all fail closed | admin writes fail with a clear error, metrics gap / status page | restore Lakebase |
| A platform admin's Postgres role missing | none | `your database role cannot write platform data; ask the Lakebase project owner` | the project owner grants the role |
| Admin write conflict; removing the last `user` platform admin | none | `changed by someone else, reload and retry`; refused | reload; add another named person first |
| `SIGTERM` | — | stop accepting, flush metrics, stop children in parallel within 15 s | — |

Policy: validation, authorization and sandbox paths fail **closed**; only genuinely best-effort work (metrics buffering) fails open, on purpose.

## 12. Threat model

Every row asks: if the child is malware, what can it do?

| Threat | Control | Residual |
|---|---|---|
| Read other children's or appmux's environment, source or platform secrets (`runtime.cfg`, `dynamic-secrets`, the customer log) | pid and ipc namespaces and a root built from an allow-list, checked by `self_check()` (spike 2) | the run directory is host-backed and can be filled (spike 7: 1 GiB in 1.5 s) |
| Reach appmux's port, loopback, container IP, metadata address | net namespace, pump rules, canary in `self_check()` (it tries every listening port of the container IP) | neighbouring pods unreachable on Free Edition because the platform drops the traffic; untested on a paid workspace |
| Exfiltrate over allowed egress or DNS | per-app allow-lists | default-allow egress; DNS tunnelling |
| Exhaust memory, CPU or processes | `RLIMIT_AS`, admission control, best-effort watchdog; `RLIMIT_NPROC` per sandbox; for CPU, a one-CPU pin with a seccomp filter adopted in v1 (spike 7, [6.4](#64-self-check-failures-and-gaps)) | a fast allocator can OOM the container; a CPU hog stalls every app for about 50 ms at a time unless pinned, and children on the pinned CPU still compete; a fork bomb stops at the limit |
| Read other apps' secrets | a secret is a reference and its value reaches only that child's environment; the appmux SP holds `READ` only on scopes a manager chose, one per app, and the shared-SP approval screen lists them | a shared-SP child can read every scope the appmux SP was given ([8.4](#84-shared-sp-what-it-reaches-and-what-can-be-done)); a revoked `READ` keeps working for minutes ([8.3](#83-secrets-where-the-value-comes-from)) |
| Steal a visitor's token | stripped unless the app is OBO-delegated, a platform admin's gate that an app admin cannot flip; minimal scope set | an approved OBO child acts as the visitor while they visit; Lakebase ignores the scope set, so for a platform admin that includes writing platform data ([4.2](#42-who-writes-what), spike 1) |
| Use the shared SP | delegated per app by a platform admin; reason in the audit log; O1–O3 in [8.4](#84-shared-sp-what-it-reaches-and-what-can-be-done) | writes `app_grants`, `app_config`, `releases`, `app_resources` (secret references) and `permission_requests` of any app, so can skip the attach check; cannot write `apps` or settings |
| Spam the inbox, or fake a request (its status, payload or requester), through the shared SP | a request grants nothing: authorization and the reconciler read only `apps`, which the SP cannot write; one pending request per (app, kind), and at most 3 pending `register` per person; approval is a platform admin's own role and checks the request again; the screen shows the current value next to the requested one; `delete` is not a kind | spam clutters the inbox; a convincing forged request can win a rubber-stamped approval, for any kind in the catalogue (a forged `register` gives a person an app they never asked for, though registering runs nothing by itself); invented names get past the per-person cap; the audit log shows approvals by a person only |
| Borrow a resource through the shared SP (confused deputy) | `shared_sp` resources are accepted only with a verified attach check made with the attacher's own token | a shared-SP child can write `app_resources` directly, which is why the delegation is a gate |
| Make appmux read a folder the app admin could not read | Update reads source and wheel folders with the presser's own token; the appmux SP holds no right on them | none beyond the presser's own rights |
| Attack the admin UI from the browser | UI hardening ([7.3](#73-same-origin-limit-accepted-in-v1)) | same-origin limit accepted |
| Forge identity headers toward another child | the edge removes client-supplied identity headers; children cannot reach the edge; the proxy overwrites them (spike 8) | none known |
| Escape the path prefix (`/sales/../hr`, `//hr`, encoded dots, slug lookalikes) | the routing function normalises and rejects ambiguous paths before matching; fixtures in [14](#14-testing) | none known |
| Malicious source, build scripts or install hooks; a bad wheel offered to many apps | build in the sandbox with the app's egress; wheel modes install wheels only, with an all-deny build egress; immutable snapshots; the app environment and every overlay mounted read-only into children, so a wheel can shadow a package only for its own release; each app's wheel lock names the hashes it installs; `source_path` fixed by the platform admin; `default_wheel_sources` writable by platform admins only | a compromised app admin can run code in that one sandbox; an app whose wheel lock names a bad hash in a default folder installs it |
| App admin widens their own app | `authorize()` plus the database write split | none beyond the shared-SP residual |
| Platform admin abuse | audit log with reasons; every write and every source view is audited; the admin's token is stripped on implicit access unless they choose **Open as a user**, which is audited as `app.view_as_user` | a platform admin can read any app's source and change any app's configuration, `source_path` and egress included; they are the trust root, and implicit access adds nothing they could not already do by reassigning the owner. In **Open as a user** an OBO-delegated child receives the admin's token, which can write platform data: an explicit, audited choice, only on an app whose OBO a platform admin already approved, and the default is the mode without the token |
| Sandbox weakened by a platform change | fail-closed `self_check()` | measurements are Free Edition only |

Out of scope: kernel or container-runtime exploits, hardware side channels, and a compromised appmux process or Databricks platform.

## 13. Deployment and operations

### 13.1 What the operator sets up

**Where the deployment folder comes from.** The operator downloads `appmux-<version>.zip` and its `.sha256` from the GitHub Release of the repository, checks the hash and unpacks it; nobody builds anything and nobody adds a file. The release workflow builds the zip (the admin UI export, the hashed wheels of item 5, the `appmux` package and `bin/slirp4netns`) and attaches it to the tag. `slirp4netns` is `GPL-2.0-or-later`, so the zip also carries its licence text, a `SOURCE.txt` and the upstream source archive of the same release under `licenses/slirp4netns/`; the static binary also contains three `LGPL-2.1` libraries (glibc, glib, libseccomp), so the folder holds the `LGPL-2.1` text and `SOURCE.txt` holds a written offer, valid for three years, for their source. appmux starts the program as a separate process and its own files stay Apache-2.0 ([ADR 0015](../decisions/0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md), accepted 2026-11-01; the release workflow still publishes nothing unless that file's status line says `accepted`). The folder is then uploaded to a workspace folder with the Databricks CLI or the browser and deployed from there. A workspace folder is not expected to keep the mode bits of its files (the first container start found `bin/slirp4netns` present but not executable; the cause is inferred, the mode was not read there), so appmux sets the owner's execute bit on that one file before it first runs it, and refuses to go on if it cannot. A developer builds the same folder with `scripts/assemble-deploy.sh` and deploys it with `scripts/sync-deploy.sh`.

1. **The slot.** Create the Databricks App (`databricks apps create appmux`) with enough compute for the children it will host; appmux reads its memory limit at startup. Grant `CAN USE` to everyone who may reach appmux (the outer layer).
2. **Lakebase.** A project whose owner grants the deployer `CAN MANAGE`; a group role `appmux_sp` that holds the appmux SP's OAuth Postgres role as a member (so the grants name one fixed role whatever the SP's client id is); the group role `appmux_platform_writers`; a role for each platform admin who will use OBO writes, added to that group. The grants are table-level ([data model 6](v1-data-model.md#6-who-writes-what)).
3. **Secret access.** There is nothing to set up for appmux itself: whoever manages an app's secret scope gives the appmux SP `READ` on that scope when the app uses it ([8.3](#83-secrets-where-the-value-comes-from)). The SP can also create a scope of its own (spike 11); whether a human who is not an admin can was not tested.
4. **appmux's own `app.yaml`** (the start command runs the `appmux` package that sits at the root of the deployment folder):

   ```yaml
   command: ["python", "-m", "appmux"]
   env:
     - name: APPMUX_BOOTSTRAP_ADMINS      # honoured only while platform_admins is unset
       value: dana@example.com
     - name: APPMUX_LAKEBASE_DATABASE
       value: databricks_postgres         # the database's name; not a secret
   ```

   The Lakebase database is also attached to the App as a resource: that is what gives the platform the reason to create the SP's Postgres role and to set `PGHOST`. The name of the database is a plain `value`, not a `valueFrom` of that resource, because the resource yields the endpoint path and not a database name ([data model 4.7](v1-data-model.md#47-app_resources)); the first container start with `valueFrom` logged `the database does not exist` (2026-10-31, Free Edition). A new Lakebase project's default database is `databricks_postgres`.

   **User authorization must be on**, or the edge has no visitor token and group grants cannot be resolved ([9.1](#91-the-model)). Scopes (`user_api_scopes`): the default identity scopes (`iam.current-user:read` is what the group lookup uses), no scope for OBO writes to Lakebase (spike 1: the login does not check scopes), `workspace.workspace:read` for the Update's workspace read (spike 15; the coarse name `workspace` is refused), and the attach checks' scopes (spike 16): `sql` (secret, warehouse and function checks, which also need a SQL warehouse the attacher can use), `files.files:read` or `files`, `catalog.tables:read`, `catalog.connections:read`, `dashboards.genie:read` or `genie`, `serving.serving-endpoints:read` or `model-serving`. A type whose scope is missing is `unverified`; `secrets`, `mlflow`, `jobs`, `access-management` and `unity-catalog` are not names an App accepts, so the types that need them stay `unverified`. A scope change takes a stop and start of the App (spike 1). Adding a scope outside the workspace allowlist needs a workspace admin once.
5. **appmux's own dependencies.** The platform installs a deployed App's requirements from the workspace's package configuration, which is blocked where the company index is unreachable. **Vendor the wheels**: ship them in the deployment folder (the release zip already does) and start `requirements.txt` with `--no-index` and `--find-links ./wheels`. The platform honours both lines (spike 13): it runs `pip` on the file, resolves `./wheels` against the deployment folder and installs into the app's environment, and the build log shows no index being consulted. With `--no-index` pip finds a dependency only in the folder (the test package's dependency was found there), so the folder holds a wheel for every package in the closure. A failed install makes that deployment the active one and the slot `UNAVAILABLE`, so read the build log before relying on a redeploy. The environment persists across deployments, so a package dropped from `requirements.txt` stays installed. Spike 13 also ran the fallback, a standard-library start script that downloads the wheels from a workspace folder the appmux SP can read into `/tmp`, runs `pip install --no-index --find-links --target`, then starts appmux with that directory on `PYTHONPATH`; it works once the folder's owner grants the SP `CAN_READ` (without the grant the listing is empty, with no error, so the script treats zero wheels as failure). Use it only if the platform stops honouring the lines. What is installed through `requirements.txt` is also the **base layer of every child's packages** ([5.3](#53-build-and-package-sources)), so keep appmux's own requirements lean; the fallback would put it in `/tmp` and add one more read-only layer for children. Both ran only where the index is reachable.
6. **Default wheel folders (optional).** Upload common wheels to a workspace folder, list it in `default_wheel_sources` and let only platform admins write there. App admins can declare their own folders with no platform step. **Source folders** need no grant to the appmux SP: the person who presses Update needs read access.
7. **First start.** The port answers at once with the "starting" page ([5.6](#56-crash-restart-and-shutdown)); appmux then runs `self_check()` and the status page shows the result; then the bootstrap admin sets `platform_admins`.

### 13.2 Day-2

- **Add or update a child:** register (a platform admin, or by approving a visitor's request, [9.4](#94-requests-to-a-platform-admin)), then Update (an app admin). No appmux redeploy.
- **Add a platform admin:** an entry in `platform_admins` (or a Databricks group listed there) **and** the person's Postgres role in `appmux_platform_writers`. Keep at least one named person.
- **Pass OBO or the shared SP down to an app, or change its name, slug, source folder or memory budget:** the app admin presses **Request** or **Request change** with a reason; a platform admin answers in the inbox ([9.4](#94-requests-to-a-platform-admin)), or makes the change directly. The owner and the egress rules are changed by a platform admin only.
- **Delete an app:** stop and unregister it. Its route closes; its configuration, releases, resources, secrets and requests are deleted with it and the clean-up job removes content-store files nobody references; `metrics` and the audit log stay.
- **Upgrade appmux:** redeploy; a few minutes of outage; children restart from their active release; the environment cache usually survives in `/tmp`.
- **Rotate the appmux SP's credentials:** done on the platform; no stored secret is affected.
- **Where to look first:** the status page, the `self_check()` result, the dashboard, the release's build-log tail. **Backups:** the registry follows the Lakebase project's practices; point-in-time restore on Free Edition is not recorded. Losing the registry loses all state.

### 13.3 Development environment

Development and integration tests run on a Databricks Free Edition workspace: at most 3 apps, each auto-stopped 24 hours after start, one Lakebase project, trusted-domain egress ([reference](../reference/databricks-free-edition.md)). Logic runs locally on any machine; the sandbox needs Linux with unprivileged user namespaces, and local AppArmor blocks `unshare` on this project's machines, so sandbox tests run only on the Databricks container.

## 14. Testing

- **Logic, run locally, written test-first:** the egress rule engine and its editor's refusals; the reconciler state machine; `authorize()` as a table (the rows in [9.2](#92-who-may-do-what), plus groups from a stubbed Databricks lookup, its cache and failure, `everyone`, owner, implicit platform-admin access and its two modes (token stripped by default, passed after the audited switch), the last-admin guard); the routing function (`prefix` matching, path normalisation, reserved slug); header, cookie and `Location` rewriting; secret resolution (a reference read at start, a failed read named in the error, the value never logged or stored); the **`app.yaml` parser** (both files, a bad `env` entry, unknown keys, the manifest, required keys); the **`databricks.yml` parser** (resources mapped to types, a `${…}` value left unresolved, several apps, ignored keys warned, an invalid file never refusing an Update); the **requirements check** (all missing keys listed at once, Activate after attaching); the attach check against stubbed Databricks calls (`ok`, `unverified`, no access, `shared_sp` refused when unverified); the **request state machine and catalogue** (one pending per app and kind, or per proposed slug for `register`, with the per-person cap; one test per kind with a valid and an invalid payload and the asker rule, a request with several fields and one gate field, a slug taken at approval closing the request as denied, a rename (the cascade, the swap under the new prefix, the `404` of the old URL), refusals, approve as one transaction, withdraw only while pending, and a forged `approved` row that changes nothing); release retention (the cap; active, target, in-progress and locked releases protected; tags protect nothing; the lock cap, including two simultaneous locks; a release deletable the moment it stops being active; a full cap refuses; numbers never reused; a deleted release cannot be activated); package resolution (the environment first, a wheel overwriting a differing version, a pin that neither the environment nor a wheel covers, the missing list, store hit by hash, fetch in order, a wheel list per release, the overlay cache key, the re-check at start); the health clock; admission control with the numbers in [5.7](#57-memory-and-admission-control).
- **Sandbox integration, run only on the Databricks container** by a sync, deploy and log-fetch script, not in CI.
- **Fixtures**, created after the design is approved and registered through the normal path: 10 well-behaved apps (light FastAPI, FastAPI holding memory, a Streamlit or Gradio app, a Node app); about 10 hostile or failing apps (an environment snooper, a **package-environment writer** that tries to modify the app environment or an overlay and must get `EROFS`, a localhost / container-IP / metadata scanner, a start-up crash, a crash loop, an always-500 health check, a slow start, a memory hog, a fork bomb, and a **secret and registry snooper** that tries other apps' secrets and the registry with and without the shared-SP token); 2 routing fixtures (one that hard-codes absolute links and must produce the `404` of [7.2](#72-the-selector-interface-and-what-v1-ships), not a misroute, and one that honours a base path); 4 offline-install fixtures (dependencies only in the app's wheel folder; a second app that needs a wheel already in the store; an app fully covered by the environment, which must fetch no wheel; an app that needs a newer version of a package the environment has, whose wheel must win; all with the index unreachable); 2 `app.yaml` fixtures (a `valueFrom` key with no resource, which must fail before start listing every missing key, and an invalid `app.yaml`) and a `databricks.yml` fixture (resources offered, nothing attached until confirmed).
- Success criteria and how each is judged are in the [brief](v1-design-brief.md#success-criteria).

## 15. Beyond v1

Deliberately left out, with room kept where noted:

- Other routing selectors (`prefix-rescue`, `root`, `header`): the `RouteSelector` interface is the room ([7.2](#72-the-selector-interface-and-what-v1-ships)).
- Group managers who are not platform admins.
- Requests for deleting an app, for a platform setting, for a new owner or for egress rules; a redirect from a renamed app's old URL; requests that an app admin decides (a user asking for access); time-limited approvals; notifications outside the UI; and re-checking a resource's attach check after the attacher's access changes.
- Evaluating `${…}` interpolations and variables in `databricks.yml` (v1 shows them unresolved); other Python versions per app; persisted runtime events.
- A pooled SP ([8.4](#84-shared-sp-what-it-reaches-and-what-can-be-done)).
- An npm (Node) offline source equivalent to the wheel installs of [5.3](#53-build-and-package-sources): spike 14 showed it works, the plan left it out of v1, so it stays here.
- Sleep and start-on-request; sharding children across slots; hostname egress rules; IPv6.
- Personal access tokens as a child credential; Git as a source; automatic rollback; an audit-log UI; alerts; per-user usage analytics; editing source in the UI; release diff and preview before promotion; canary traffic; blue/green of appmux itself.
- Admin step-up confirmation; per-app `Content-Security-Policy: sandbox` if the experiment fails; in-process `NOLOGIN` roles with `SET LOCAL ROLE`; durable child logs; versioned secrets and resources per release.
