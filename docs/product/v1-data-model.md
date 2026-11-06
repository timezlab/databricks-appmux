# appmux v1 — data model (Lakebase)

**Status:** design approved; names are final for v1. **This is the design, not a status report:** the tables are created by the baseline migration, and [ARCHITECTURE.md](../../ARCHITECTURE.md) marks what else exists. **Date:** 2026-10-30 (status line updated 2026-11-05).

This document is the detailed design of every table appmux keeps in its Lakebase database (the **registry**). [The v1 design](v1-design.md) says what appmux does; this says what it remembers and why. The [plan](../../specs/001-appmux-v1/data-model.md) adopts these table and column names as the baseline migration; a later change to one is a new migration, not an edit of the baseline. Every example uses made-up names (`example.com` emails, an app called `sales`) and shortened ids and hashes.

Contents: [1 Reading guide and rules](#1-reading-guide-and-rules) · [2 Business view](#2-business-view) · [3 The nine tables](#3-the-nine-tables) · [4 Tables](#4-tables) · [5 Walkthroughs](#5-walkthroughs) · [6 Who writes what](#6-who-writes-what) · [7 Rules enforced in code](#7-rules-enforced-in-code) · [8 Retention and volume](#8-retention-and-volume) · [9 Open points](#9-open-points)

## 1. Reading guide and rules

Each table has the same parts ([4](#4-tables)): **purpose and rules**, **columns** (type, null or default, an example value and the meaning of each), **example rows** (made-up data that stays consistent across the document: app `sales`, owner Alice, platform admin Dana), **DDL** (indicative PostgreSQL, standard features only) and **writer**. A business reader can read the purpose and the example rows and skip the rest; an implementer reads all of them.

| # | Rule | Why |
|---|---|---|
| 1 | **The database holds decisions and history, not the runtime's current state.** What is running now (app state, restart counts, the route table), recent events and the stored files live in memory, in logs and in the content store. | Fewer tables and fewer writers; the reconciler rebuilds run state from the decisions at start. |
| 2 | **Natural key for apps.** `apps.slug` is the primary key and every table points to it as `app_slug`. | A slug is unique, URL-safe and readable. It can be renamed (rarely, by an approved request), so every foreign key to it is `ON UPDATE CASCADE` ([5.7](#57-rename-an-app)). |
| 3 | **Per-app release number.** A release is `(app_slug, release_no)`, the "v7" users see. Numbers are never reused. | Users say "roll back to 5". |
| 4 | **Two writer classes.** A table is written either by platform admins (through their own database role) or by the appmux service principal (SP). The one exception is `permission_requests`, which grants nothing. Where a business object has fields of both kinds it is split in two (`apps` and `app_config`). | The write rule of [design 4.2](v1-design.md#42-who-writes-what) becomes plain table-level `GRANT`s. |
| 5 | **Enumerations are `text` with a `CHECK`; emails are lower case; timestamps are `timestamptz` (UTC).** Every row records who did it: an email, or `db:<role>` for a manual change. The two lists the code owns, `platform_settings.key` and `permission_requests.kind`, are checked in code instead. | A new value is a one-line migration; one person is one string. |
| 6 | **History is never deleted by accident.** Releases are soft-deleted; `audit_log` is append-only; history tables have no foreign key to `apps`. | The audit trail refers to apps that no longer exist. |
| 7 | **`jsonb` only where the whole value is read at once** (settings values, release config, the detected manifest, egress rule lists, a request's payload). Anything filtered, joined or constrained gets columns. | Keeps queries and constraints in SQL. |
| 8 | **Secret values never appear as plaintext** in any column, row or `jsonb`: a secret is a reference (`scope/key`). | [Design 8.3](v1-design.md#83-secrets-where-the-value-comes-from). |
| 9 | **Each setting states its level of effect** (all children, one child, per visitor). | Premise 4. |

## 2. Business view

| Actor | Is | Acts on |
|---|---|---|
| **Platform admin** | a user, or a Databricks group, listed in `platform_admins` | what runs in the slot: registers apps (itself, or by approving a request for a new app), sets each app's egress rules, answers requests, sets platform settings; holds `CAN_MANAGE` on every app implicitly (opens it, reads its snapshot source, edits its configuration) |
| **App admin** | a user, or a Databricks group, holding `CAN_MANAGE` on one app; the owner is always one | the app's releases, resources, secrets, sharing and health check; asks a platform admin for what only a platform admin may change on the app (a delegation, a new name, slug, source folder or memory budget) |
| **User** | anyone with `CAN_USE` on an app | opens the app; anyone who can reach appmux can also ask for a new app |
| **appmux itself** | the running service | snapshots, build results, metrics, the audit trail |

Users and groups are the ones Databricks knows. appmux stores **no list of people and no groups**; it stores grants that name an email or a Databricks group, as Databricks Apps does ([design 9.1](v1-design.md#91-the-model)).

| Entity | In plain words | Table |
|---|---|---|
| **App** | a child registered in the slot, with its name, slug, limits, delegation flags and egress rules | `apps` |
| **App settings** | what the app admin controls: run or stop, which release was chosen to go live, the health check, own-SP keys | `app_config` |
| **Grant** | permission for a person or group on an app | `app_grants` |
| **Release** | a snapshot plus config, with its status, name, tags and lock: the unit that is built, started, activated and rolled back | `releases` |
| **Resource** | something the app declares and uses, with a key its `app.yaml` refers to (`valueFrom`): a secret, a SQL warehouse, … | `app_resources` |
| **Request** | an ask that a platform admin do what only a platform admin may do: register a new app, pass a credential (OBO, the shared SP or both) down to an app, or change its name, slug, source folder or memory budget | `permission_requests` |
| **Setting** | a platform-wide value, including who the platform admins are | `platform_settings` |

The life of a release, at table level:

```
Update pressed ─► snapshot stored in the content store ─► releases row (release_no 9, status pending)
                                                                │
                              status: pending → building → starting → healthy → active     (or failed)
                                                                │
        app_config.target_release_no = 9 ──► the release whose status is active is what a restart runs
        later: name it, tag it, lock it, roll back to it, or delete it unless it is active, target or locked
        (deleting keeps the row and removes the snapshot)
```

## 3. The nine tables

```
platform_settings                          apps ──┬── app_config ──► releases (target)
 (admins, caps, global egress)             slug   ├── app_grants
                                                  ├── app_resources (secret references)
                                                  ├── permission_requests
                                                  └── releases (snapshot digest, wheels, status)
                  metrics · audit_log   (history; no foreign keys)
```

| Table | Holds | Writer | Level of effect |
|---|---|---|---|
| `platform_settings` | platform-wide values: admins, release cap, lock cap, package mode, default wheel sources, the shared-SP master switch, memory reserve, global egress rules | platform admin | all children |
| `apps` | the platform's facts about a child: owner, name, source folder, budget, delegation flags, its egress rules | platform admin | one child |
| `permission_requests` | a request to register an app, for a delegation or to change an app's name, slug, source folder or budget, and its answer | the asker (ask, withdraw): a visitor for a new app, an app admin otherwise; platform admin (answer) | one child |
| `app_config` | run or stop, target release, health check, own-SP keys | app admin | one child |
| `app_grants` | who may use or manage each app | app admin | one child |
| `releases` | snapshot reference, config, detected manifest, wheel manifest, status, name, tags, lock | app admin (through appmux) | one child |
| `app_resources` | the app's own resources, including secret references | app admin | one child |
| `metrics` | per-minute aggregates | appmux | none |
| `audit_log` | every administrative action | any writer, insert only | none |

The earlier draft had 20 tables. These are gone, and where each went:

| Was a table | Now |
|---|---|
| `platform_grants` | the `platform_admins` setting |
| `app_delegations` | two boolean columns on `apps`; who, when and why are in the audit log |
| `egress_rules` | ordered JSON lists: `platform_settings.egress_global` and `apps.egress` |
| `snapshots`, `wheels`, `release_wheels` | the content store (files named by digest); a release records its snapshot digest and its wheel list |
| `release_tags` | `releases.tags` (free labels) |
| `secret_values` | nothing: a `secret` resource is a reference to a Databricks secret ([4.7](#47-app_resources)) |
| `release_status` | columns of `releases`; the live release is the one with status `active` |
| `app_runtime`, `events` | memory and log lines ([rule 1](#1-reading-guide-and-rules)) |
| `registry_version` | the reconciler re-reads the small tables every few seconds |

## 4. Tables

Every table is described the same way. **Purpose and rules** says what it is for. **Columns** lists each column with its type, whether it may be null, an example value and its meaning; the Example column is one complete row, so a reader sees a real record without reading SQL. **Example rows** adds the rows that show the other cases. **DDL** is the indicative PostgreSQL; **Writer** says who may change it. "Null / default" reads "no" for `NOT NULL` with no default, and "yes" for a nullable column.

### 4.1 `platform_settings`

**Purpose and rules.** Platform-wide values: a key and a `jsonb` value. The code owns the list of keys, their types and defaults; an unknown key is rejected. Platform admins are one of these keys, so there is no separate grants table for the platform. A change that breaks a rule between two keys (below) is refused.

**Columns.**

| Column | Type | Null / default | Example | Meaning |
|---|---|---|---|---|
| `key` | `text` | primary key | `max_releases_per_app` | one of the keys below |
| `value` | `jsonb` | no | `10` | the value; its JSON type depends on the key |
| `updated_by` | `text` | no | `dana@example.com` | who changed it last (an email, or `db:<role>`) |
| `updated_at` | `timestamptz` | no, `now()` | `2026-10-30T08:40Z` | when |

**Example rows** — the keys the code accepts, with a sample value:

| key | value (sample) | Meaning | Level of effect |
|---|---|---|---|
| `platform_admins` | `[{"type": "user", "name": "dana@example.com"}, {"type": "group", "name": "data-platform"}]` | who holds `CAN_MANAGE` on the platform, and implicitly on every app ([design 9.1](v1-design.md#91-the-model)); at least one `user` entry must remain | all children |
| `memory_reserve_mb` | `1024` | memory kept for appmux itself ([design 5.7](v1-design.md#57-memory-and-admission-control)) | all children |
| `default_memory_budget_mb` | `512` | the memory budget a new app starts with: what approving a `register` request gives it, and what a direct registration uses when it names none (default 512; 256 to 16384, the range of a new `apps.memory_budget_mb`; apps that already exist keep theirs; [design 5.7](v1-design.md#57-memory-and-admission-control)) | all children |
| `package_mode` | `"wheelhouse"` | `index`, `wheelhouse` or `wheelhouse+index` ([design 5.3](v1-design.md#53-build-and-package-sources)) | all children |
| `default_wheel_sources` | `["/Workspace/Shared/appmux/wheelhouse"]` | folders every Update also reads wheels from | all children |
| `shared_sp_delegation_allowed` | `false` | master switch: when off, no child is lent the appmux SP's token and no request for it is accepted (default `false`) | all children |
| `max_releases_per_app` | `10` | live releases kept per app (default 10; [4.6](#46-releases)) | all children |
| `max_locked_releases` | `3` | releases an app admin may lock by hand per app (default 3; [4.6](#46-releases)) | all children |
| `egress_global` | `[{"action": "deny", "cidr": "@container_ip/32", "port": "any", "comment": "the container's own address"}, {"action": "deny", "cidr": "169.254.0.0/16", "port": "any", "comment": "metadata and link-local"}]` | ordered rules for every child's outbound traffic; first match wins ([design 6.3](v1-design.md#63-egress-rules)) | all children |
| `request_timeout_s` | `120` | per-request timeout the edge enforces on each app (maximum 290, below the platform's 300 s; default 120) | all children |
| `max_request_body_mb` | `100` | request body cap per app (default 100, the largest size tested through the proxy) | all children |
| `max_concurrent_requests` | `64` | requests in flight per app before the edge answers 429 (default 64) | all children |
| `build_timeout_s` | `600` | wall-clock limit of one build in the sandbox (default 600) | all children |
| `max_processes_per_child` | `256` | `RLIMIT_NPROC` set per sandbox (default 256) | all children |
| `child_cpu_pin` | `true` | pin every sandbox to one CPU at `nice 19` and refuse `sched_setaffinity` by seccomp (default on; [design 6.4](v1-design.md#64-self-check-failures-and-gaps)) | all children |
| `attach_check_warehouse_id` | `"<warehouse-id>"` | the SQL warehouse the attach checks of `secret`, warehouse and function resources use; unset by default, and an attach that needs it is refused naming this key | all children |

**Rule between keys:** `max_locked_releases` must be at most `max_releases_per_app - 4`, so the active release, the target and one more release in progress can never fill the cap together with the locks and leave nothing to clean up. Lowering `max_locked_releases` below the number of locks an app already holds unlocks nothing; that app can only add locks again once it is under the new number.

Fixed values such as the drain time (30 s) and the group cache time (300 s) are named constants in code, not settings: nobody needs to change them at run time.

**DDL.**

```sql
CREATE TABLE platform_settings (
  key        text PRIMARY KEY,                       -- checked against the code's list of keys, not by SQL
  value      jsonb NOT NULL,                         -- the shape depends on the key; validated in code
  updated_by text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
```

**Writer:** platform admins. Adding or removing an admin writes an audit row with a reason.

### 4.2 `apps`

**Purpose and rules.** The platform-owned facts of a child. Everything here is decided by a platform admin (the review gate "register an app", [design 9.3](v1-design.md#93-config-levels-and-review-gates)); an app admin can ask for a change to `slug`, `display_name`, `source_path`, `memory_budget_mb` or a delegation, and any visitor for a whole new row, with a request ([4.3](#43-permission_requests)); the owner and the egress rules change only by a platform admin's direct edit. The health check is not here: it affects no other app, so it is in `app_config` ([4.4](#44-app_config)). Fields are **declared only when the platform cannot find them out**: there is no `app_type`; the runtime is detected from the files ([design 5.4](v1-design.md#54-appyaml-requirements-and-start)).

**Columns.**

| Column | Type | Null / default | Example (`sales`) | Meaning |
|---|---|---|---|---|
| `slug` | `text` | primary key | `sales` | URL prefix; lower-case letters, digits, `-`, at most 31 characters; renamed only by an approved `settings` request or a platform admin's edit, and every foreign key to it follows ([5.7](#57-rename-an-app)). No underscore, so `_appmux` can never be a slug |
| `display_name` | `text` | no | `Sales dashboard` | shown in the launcher; changed by a `settings` request or a platform admin |
| `owner` | `text` | no | `alice@example.com` | one user; always an app admin; only a platform admin changes it, directly (`app.set_owner`), with no request |
| `source_path` | `text` | no | `/Workspace/Shared/apps/sales` | folder snapshotted on Update, read with the **presser's own token** |
| `memory_budget_mb` | `integer` | no | `512` | admission control and `RLIMIT_AS` ([design 5.7](v1-design.md#57-memory-and-admission-control)); the table holds 64 to 16384, and a budget typed now is 256 to 16384 (the floor moved up from 64 on 2026-11-04, T213: below 256 a runtime that reserves address space at start dies or hangs, [design 5.7](v1-design.md#57-memory-and-admission-control)), so an app saved under the old floor keeps its budget until someone changes it; a new app starts with the `default_memory_budget_mb` setting ([4.1](#41-platform_settings)) unless a platform admin registers it with another value |
| `obo_delegated` | `boolean` | no, `false` | `true` | the platform admin allows the visitor's token to be passed down to this app |
| `shared_sp_delegated` | `boolean` | no, `false` | `false` | the platform admin allows the appmux SP's token to be lent to this app (effective only while `shared_sp_delegation_allowed` is on) |
| `egress` | `jsonb` | no, `[]` | `[{"action": "allow", "cidr": "10.20.0.0/16", "port": "443", "comment": "warehouse subnet"}, {"action": "deny", "cidr": "0.0.0.0/0", "port": "any", "comment": ""}]` | this app's own egress rules: ordered, first match wins, evaluated before the global rules; set by a platform admin only (the app admin sees them read-only) |
| `version` | `bigint` | no, `1` | `3` | optimistic-concurrency counter: an admin write that read an older value is refused |
| `created_by`, `created_at` | `text`, `timestamptz` | no | `dana@example.com`, 2026-10-30T08:41Z | who registered it (or approved the request for it) and when |
| `updated_by`, `updated_at` | `text`, `timestamptz` | no | `dana@example.com`, 2026-10-30T08:44Z | last platform-field change |

**Example rows** (the other apps of the document):

| slug | display_name | owner | source_path | memory_budget_mb | obo_delegated | shared_sp_delegated | egress |
|---|---|---|---|---|---|---|---|
| `docs` | `Team docs` | `frank@example.com` | `/Workspace/Shared/apps/docs` | `256` | `false` | `false` | `[]` |
| `reports` | `Weekly reports` | `grace@example.com` | `/Workspace/Shared/apps/reports` | `256` | `false` | `false` | `[]` |

A delegation flag means "this credential **may** be passed down to this app". The app admin decides whether to use it, never whether it is allowed. The reason, the person and the time are in the audit log (`request.approve`, `delegation.grant`, `delegation.revoke`), each with a mandatory reason.

**DDL.**

```sql
CREATE TABLE apps (
  slug                text PRIMARY KEY CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,30}$'),   -- renamable; the foreign keys to it are ON UPDATE CASCADE
  display_name        text NOT NULL CHECK (btrim(display_name) <> ''),
  owner               text NOT NULL CHECK (owner = lower(owner)),
  source_path         text NOT NULL CHECK (source_path LIKE '/Workspace/%'),
  memory_budget_mb    integer NOT NULL CHECK (memory_budget_mb BETWEEN 64 AND 16384),
  obo_delegated       boolean NOT NULL DEFAULT false,     -- set only by approving a delegation request, or directly by a platform admin
  shared_sp_delegated boolean NOT NULL DEFAULT false,
  egress              jsonb NOT NULL DEFAULT '[]',        -- [{"action", "cidr", "port", "comment"}], in order
  version             bigint NOT NULL DEFAULT 1,
  created_by          text NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_by          text NOT NULL,
  updated_at          timestamptz NOT NULL DEFAULT now()
);
```

**Writer:** platform admins, including the two delegation flags, a new `slug` and the insert that approving a `register` request makes. Deleting a row deletes the app's configuration through cascades ([5.6](#56-delete-an-app)); renaming one rewrites `app_slug` in the tables that point to it ([5.7](#57-rename-an-app)).

### 4.3 `permission_requests`

**Purpose and rules.** The way a person asks a platform admin, inside appmux, for something only a platform admin may do: register a new app, or change something on an existing one. A request names the app (for a new app, the slug it proposes), a **kind**, what is asked (`payload`) and a **reason** the platform admin will read. Three kinds ship in v1 ([design 9.4](v1-design.md#94-requests-to-a-platform-admin)): registering an app (`register`), passing a credential down (`delegation`: OBO, the shared SP, or both) and changing an app's settings (`settings`: any of `slug`, `display_name`, `source_path`, `memory_budget_mb`). The platform admin approves or denies it, every field or none. **A request grants nothing:** an approval makes the change on `apps` (for `register`, it inserts the row) through the platform admin's own role, and authorization and the reconciler read only `apps`. The app page, the launcher and the inbox read the request row for its state and history.

**`kind` has no list in SQL**, like the keys of `platform_settings`: the code's catalogue owns the kinds, each with its fields, who may ask, the change it makes and which fields are review gates. Adding a field or a kind is a catalogue entry and its tests, not a migration; a row whose kind the catalogue does not know cannot be approved.

**A request to register has no app yet.** Its `app_slug` is null while it waits and the slug it proposes is in `payload`; approving inserts the `apps` row and sets `app_slug` to it, so from then on the request is deleted with the app. A denied or withdrawn one keeps a null `app_slug` as history. The `CHECK` and the unique index below use the proposed slug in its place.

**Columns.**

| Column | Type | Null / default | Example (request 1) | Meaning |
|---|---|---|---|---|
| `request_id` | `bigint` | primary key, generated | `1` | |
| `app_slug` | `text` | yes | `sales` | the app; deleted with it, and renamed with it. Null only for a request to register, until it is approved |
| `kind` | `text` | no | `delegation` | one of the catalogue's kinds ([design 9.4](v1-design.md#94-requests-to-a-platform-admin)) |
| `payload` | `jsonb` | no, `{}` | `{"obo": true}` | what is asked, as the kind defines it: the flags for a delegation, the new values of the fields to change for `settings`, the new app's fields for `register`; a JSON object |
| `reason` | `text` | no | `dashboard queries the SQL warehouse as the viewer` | what the platform admin reads; up to 500 characters |
| `requested_by` | `text` | no | `alice@example.com` | an app admin (any visitor, for `register`) |
| `requested_at` | `timestamptz` | no, `now()` | 2026-10-30T08:43Z | |
| `status` | `text` | no, `'pending'` | `approved` | `pending`, `approved`, `denied` or `withdrawn` |
| `closed_by` | `text` | yes | `dana@example.com` | the platform admin who answered (for an automatic denial, the one who pressed Approve), or the requester who withdrew |
| `closed_at` | `timestamptz` | yes | 2026-10-30T08:44Z | null exactly while `pending` |
| `closed_note` | `text` | yes | `the app reads per-viewer data only` | the reason or note that came with the answer |

**Example rows:**

| request_id | app_slug | kind | payload | reason | requested_by | requested_at | status | closed_by | closed_at | closed_note |
|---|---|---|---|---|---|---|---|---|---|---|
| 2 | `docs` | `settings` | `{"display_name": "Team docs", "memory_budget_mb": 256}` | search holds the index in memory and needs 256 MB; the name should say what the app is | `frank@example.com` | 2026-10-30T08:47Z | `approved` | `dana@example.com` | 2026-10-30T08:49Z | fits: 1918 MB free |
| 3 | `docs` | `delegation` | `{"obo": true}` | search reads the shared index as the viewer | `frank@example.com` | 2026-10-30T09:05Z | `denied` | `dana@example.com` | 2026-10-30T09:20Z | the index is public: no token needed |
| 4 | `sales` | `settings` | `{"slug": "revenue"}` | the dashboard now covers all revenue, not only sales | `alice@example.com` | 2026-10-30T09:35Z | `pending` | | | |
| 5 | `docs` | `delegation` | `{"obo": true}` | show each reader their own workspace files | `frank@example.com` | 2026-10-30T09:50Z | `pending` | | | |
| 6 | | `register` | `{"slug": "wiki", "display_name": "Team wiki", "source_path": "/Workspace/Shared/apps/wiki"}` | the team wiki, a small Flask app | `frank@example.com` | 2026-10-30T09:55Z | `pending` | | | |
| 7 | `reports` | `register` | `{"slug": "reports", "display_name": "Weekly reports", "source_path": "/Workspace/Shared/apps/reports"}` | the weekly finance report page, a Streamlit app | `grace@example.com` | 2026-10-30T10:05Z | `approved` | `dana@example.com` | 2026-10-30T10:12Z | finance owns the folder; the default 512 MB fits |
| 8 | `docs` | `settings` | `{"slug": "reports"}` | docs is becoming the reports hub | `frank@example.com` | 2026-10-30T10:08Z | `denied` | `dana@example.com` | 2026-10-30T10:20Z | slug 'reports' is already taken (automatic) |

Requests 3 and 5 are both `delegation` for `docs`: the second was possible only because the first was closed. Request 6 has no app yet, so its `app_slug` is empty and the slug is in the payload; request 7 was linked to `reports` when Dana approved it. Request 8 asked for `reports` while that slug was still free; request 7 took it first, so when Dana pressed Approve the request was closed as denied automatically, with Dana as `closed_by`.

**`payload` by kind.** The keys are the names of the `apps` columns the approval sets (for a delegation, the flag names without `_delegated`), checked by the same code that checks a direct edit. A request is a review gate when it holds a gate field: every `register` request, `source_path`, `obo` and `shared_sp`.

| kind | payload (sample) |
|---|---|
| `register` | `{"slug": "reports", "display_name": "Weekly reports", "source_path": "/Workspace/Shared/apps/reports"}`: all three keys are required and no others are accepted (there is no budget: the new app gets `default_memory_budget_mb`, read when the request is approved); the owner is the requester and every other column of `apps` takes its default. A pending row stored before this rule may still hold `memory_budget_mb`; approving ignores it |
| `delegation` | `{"obo": true}`, `{"shared_sp": true}` or both; only `true`, because taking a delegation away is a platform admin's `delegation.revoke` |
| `settings` | `{"display_name": "Team docs", "memory_budget_mb": 256}`: any non-empty subset of `slug`, `display_name`, `source_path` and `memory_budget_mb` |

**DDL.**

```sql
CREATE TABLE permission_requests (
  request_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  app_slug     text REFERENCES apps (slug) ON UPDATE CASCADE ON DELETE CASCADE,        -- null only while a request to register waits
  kind         text NOT NULL CHECK (kind ~ '^[a-z][a-z_]{0,31}$'),   -- the list of kinds is in code
  payload      jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(payload) = 'object'),
  reason       text NOT NULL CHECK (btrim(reason) <> '' AND char_length(reason) <= 500),
  requested_by text NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied', 'withdrawn')),
  closed_by    text,         -- the platform admin who answered, or the requester who withdrew
  closed_at    timestamptz,
  closed_note  text,
  CHECK (app_slug IS NOT NULL OR payload ? 'slug'),                  -- a request to register names its slug in payload
  CHECK ((status = 'pending') = (closed_at IS NULL)),
  CHECK ((closed_at IS NULL) = (closed_by IS NULL))
);
CREATE UNIQUE INDEX one_pending_request ON permission_requests (coalesce(app_slug, payload ->> 'slug'), kind) WHERE status = 'pending';
```

**Approve** is one transaction through the platform admin's own role: lock the app's `apps` row, check the request again as it stands now (still `pending`; the payload still valid; the requester still holds `CAN_MANAGE`; for `delegation`, the master switch still on for `shared_sp`; for a `slug`, no other app has it), apply the change to `apps` and add 1 to its `version`, set `status = 'approved'` and `closed_*`, and insert the audit row `request.approve` with the reason and the values before and after (a rename also inserts `app.rename`). A failed check writes nothing and the screen says why, with one exception: if the slug is taken, the transaction instead sets `status = 'denied'`, `closed_by` to the platform admin who pressed Approve and `closed_note` to `slug '<slug>' is already taken (automatic)`, and inserts `request.deny` with `"automatic": true`, because waiting cannot make a taken slug free. The change is exactly what was asked, every field or none: to grant less, deny with a note naming what would be approved and let the app admin ask again with fewer fields. If the app already has every requested value when the platform admin answers (a direct change in between), approving only closes the request. **Approving a `register` request** has no `apps` row to lock or change: after the same checks (still `pending`, payload valid, no app has the slug yet; a taken slug is the automatic denial above) the transaction inserts `apps` (owner = the requester, `created_by` = the platform admin, `memory_budget_mb` = the `default_memory_budget_mb` setting as it stands when the platform admin presses Approve, every other column at its default) and the `app_config` row (stopped, no target), sets the request's `app_slug` and closes it; the primary key of `apps` refuses a second approval of the same slug. **Deny** closes the row with an optional note (`request.deny`). **Withdraw** is allowed only while pending (`request.withdraw`). A platform admin may also make any of these changes **directly** with no request (`app.register`, `app.edit_platform_fields`, `delegation.grant`, `app.set_owner`), and only a platform admin removes a delegation (`delegation.revoke`); a revoked delegation leaves its old requests as history.

**Self-approval is allowed but recorded:** a person who is both an asker and a platform admin can approve their own request, and the audit row shows both names. A second-person rule was rejected because it would lock out a team with one platform admin. There is no notification outside the UI in v1: the platform page has an inbox and platform admins see a pending count on the launcher.

**Writer:** the appmux SP inserts requests and withdraws them (the asker's actions); a platform admin's role closes them. This is the one table both classes write. It is safe because the row grants nothing: a child holding the shared-SP token can at most add spam, ask for something in another person's name (a request to register an app included), or mark its own request `approved`, and nothing on `apps` changes. A request to register is open to any visitor, so spam can also arrive under invented names: each is one row that a reviewer denies with a click.

### 4.4 `app_config`

**Purpose and rules.** What the app admin controls, one row per app, created together with the `apps` row. Splitting it from `apps` is what gives each table one writer class ([rule 4](#1-reading-guide-and-rules)). `desired_state` is `running` or `stopped`. `target_release_no` is the release the app admin chose to go live by pressing Update or Activate; nobody approves it, and it is `null` until the first Update. The release that last passed health is the one with status `active` ([4.6](#46-releases)); the two differ while an update is in progress or after a failed one. The health check (`health_path`, `health_deadline_s`) is here, not in `apps`, because it affects no other app: the app admin sets it ([design 5.5](v1-design.md#55-health-and-swap)). The two `own_sp_*_key` columns are the **keys of two `secret` resources** holding an existing service principal's client id and secret ([design 8.1](v1-design.md#81-the-three-modes-and-delegation-requests)); both set or both null.

**Columns.**

| Column | Type | Null / default | Example (`sales`) | Meaning |
|---|---|---|---|---|
| `app_slug` | `text` | primary key | `sales` | the app; deleted and renamed with it |
| `desired_state` | `text` | no, `'stopped'` | `running` | `running` or `stopped` |
| `target_release_no` | `integer` | yes | `7` | the release the app admin chose to go live; refers to `releases` |
| `health_path` | `text` | no, `'/'` | `/` | path the health check requests; any answer below 500 passes |
| `health_deadline_s` | `integer` | no, `60` | `60` | seconds a new release has, from the start of its process, to pass; 5 to 600 |
| `own_sp_client_id_key` | `text` | yes | `sp-client-id` | key of the `secret` resource with the own SP's client id |
| `own_sp_client_secret_key` | `text` | yes | `sp-client-secret` | key of the `secret` resource with its client secret |
| `version` | `bigint` | no, `1` | `12` | optimistic-concurrency counter |
| `updated_by` | `text` | no | `alice@example.com` | |
| `updated_at` | `timestamptz` | no, `now()` | 2026-10-30T09:14Z | |

**Example rows:**

| app_slug | desired_state | target_release_no | health_path | health_deadline_s | own_sp_client_id_key | own_sp_client_secret_key | version | updated_by | updated_at |
|---|---|---|---|---|---|---|---|---|---|
| `docs` | `stopped` | *null* | `/healthz` | `120` | *null* | *null* | `1` | `frank@example.com` | 2026-10-29T16:10Z |

**DDL.**

```sql
CREATE TABLE app_config (
  app_slug                 text PRIMARY KEY REFERENCES apps (slug) ON UPDATE CASCADE ON DELETE CASCADE,
  desired_state            text NOT NULL DEFAULT 'stopped' CHECK (desired_state IN ('running', 'stopped')),
  target_release_no        integer,
  health_path              text NOT NULL DEFAULT '/' CHECK (health_path LIKE '/%'),
  health_deadline_s        integer NOT NULL DEFAULT 60 CHECK (health_deadline_s BETWEEN 5 AND 600),
  own_sp_client_id_key     text,
  own_sp_client_secret_key text,
  version                  bigint NOT NULL DEFAULT 1,
  updated_by               text NOT NULL,
  updated_at               timestamptz NOT NULL DEFAULT now(),
  CHECK ((own_sp_client_id_key IS NULL) = (own_sp_client_secret_key IS NULL))
);
```

The three composite keys are not in this `CREATE TABLE`: they refer to `releases` and `app_resources`, which come later, so the `ALTER TABLE` at the end of [4.7](#47-app_resources) adds them once every table exists (spike 9 ran the block with them inside and PostgreSQL refused it: `relation "releases" does not exist`). They are `DEFERRABLE INITIALLY DEFERRED` so that a rename of the slug can rewrite `app_config`, `releases` and `app_resources` in any order and be checked once, at commit. Spike 9 ran this DDL on PostgreSQL 17.11 (Lakebase, Free Edition only; [log](../../specs/001-appmux-v1/spikes/README.md#spike-9--cascade-privileges-on-lakebase-and-the-slirp4netns-licence-2026-10-30)): the cascade and the deferred checks work together, and a rename also went through when the three keys were made not deferrable, so the deferral is insurance against trigger order, not a measured need. **Writer:** the appmux SP for updates, including a platform admin's edits of the app's configuration after `authorize()` ([6](#6-who-writes-what)); the platform admin's role inserts the first row at registration.

### 4.5 `app_grants`

**Purpose and rules.** Who may open or manage each app. `CAN_MANAGE` includes `CAN_USE`. A principal has one level per app. The owner is not a row: it is `apps.owner`, always `CAN_MANAGE`. Neither is a platform admin's implicit `CAN_MANAGE` on every app: it is derived from the `platform_admins` setting ([design 9.1](v1-design.md#91-the-model)), so it is not in the sharing list and cannot be removed from an app. `everyone` mirrors Databricks' "Anyone in my organization can use". **Groups are resolved from Databricks**, not from appmux ([design 9.1](v1-design.md#91-the-model)). A `service_principal` row lets another Databricks App's service principal open the app: it names the application id that the Apps proxy forwards in `X-Forwarded-Email`, and it is `CAN_USE` only; a stored `CAN_MANAGE` counts as `CAN_USE`, as for `everyone` ([ADR 0020](../decisions/0020-let-a-service-principal-hold-an-app-grant.md)). An application id has no `@`, so it can never equal a user's email.

**Columns.**

| Column | Type | Null / default | Example | Meaning |
|---|---|---|---|---|
| `app_slug` | `text` | part of the key | `sales` | the app; deleted and renamed with it |
| `principal_type` | `text` | part of the key | `group` | `user`, `group`, `everyone` or `service_principal` |
| `principal` | `text` | part of the key | `sales-team` | an email in lower case, a Databricks group name, `*` for `everyone`, or the application id of a service principal (a UUID in lower case) |
| `permission` | `text` | no | `CAN_USE` | `CAN_USE` or `CAN_MANAGE` |
| `granted_by` | `text` | no | `alice@example.com` | an app admin |
| `granted_at` | `timestamptz` | no, `now()` | 2026-10-30T08:52Z | |

**Example rows:**

| app_slug | principal_type | principal | permission | granted_by | granted_at |
|---|---|---|---|---|---|
| `sales` | `user` | `carol@example.com` | `CAN_MANAGE` | `alice@example.com` | 2026-10-30T08:53Z |
| `docs` | `everyone` | `*` | `CAN_USE` | `frank@example.com` | 2026-10-29T16:10Z |
| `sales` | `service_principal` | `4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab` | `CAN_USE` | `alice@example.com` | 2026-11-03T10:05Z |

**DDL.** The baseline below; `migrations/0003_service_principal_grants.sql` then replaces the `principal_type` check with one that also admits `service_principal`, and adds `app_grants_service_principal_check`: `principal_type <> 'service_principal' OR principal ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'`. A registry loaded before 0003 existed gets it applied by hand ([TD-008](../tech-debt.md)); the file is one `ALTER TABLE` that drops both checks `IF EXISTS` before it adds them, so it applies whole or not at all, even in autocommit, and a second run changes nothing.

```sql
CREATE TABLE app_grants (
  app_slug       text NOT NULL REFERENCES apps (slug) ON UPDATE CASCADE ON DELETE CASCADE,
  principal_type text NOT NULL CHECK (principal_type IN ('user', 'group', 'everyone')),
  principal      text NOT NULL,
  permission     text NOT NULL CHECK (permission IN ('CAN_USE', 'CAN_MANAGE')),
  granted_by     text NOT NULL,
  granted_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (app_slug, principal_type, principal),
  CHECK ((principal_type = 'everyone') = (principal = '*')),
  CHECK (principal_type <> 'user' OR principal = lower(principal))
);
CREATE INDEX app_grants_by_principal ON app_grants (principal_type, principal);  -- the launcher
```

**Writer:** app admins (through the appmux SP).

### 4.6 `releases`

**Purpose and rules.** One row per snapshot-plus-config: the unit that is built, health-checked, activated and rolled back. A release's **content** (snapshot, config, detected manifest) never changes after creation, and its wheel list is written once, when the Update flow creates the row (a config edit that creates a release copies the list of the release it starts from), and is only read afterwards by the build, a restart or a rollback; its **status**, **name**, **tags** and **lock** do change. Snapshots and wheels are files in the content store named by their digest, so identical bytes are stored once; the row points to them. Deleting a release removes the pointers and keeps the row, so numbers are never reused and audit entries still resolve.

`status` is `pending → building → starting → healthy → active`, plus `failed` and `superseded`; `status_reason` and `build_log_tail` (the last 40 lines of a build, or the last 50 lines of the child's output for a failed start, kept for failed releases) explain a failure.

**Columns.**

| Column | Type | Null / default | Example (`sales` release 7) | Meaning |
|---|---|---|---|---|
| `app_slug` | `text` | part of the key | `sales` | the app; deleted and renamed with it |
| `release_no` | `integer` | part of the key | `7` | the "v7" users see; `max + 1` at creation, never reused |
| `snapshot_digest` | `text` | yes | `sha256:9f2c…` | the snapshot in the content store; null once the release is deleted |
| `detected` | `jsonb` | no | see below | what appmux found in the snapshot: runtime, command, `app.yaml` env, `databricks.yml` resources, warnings |
| `config` | `jsonb` | no, `{}` | see below | what the app admin adds without touching the child's source: env entries, wheel sources |
| `wheels` | `jsonb` | no, `[]` | see below | the wheels the install used, with source and hash; written once, by the Update flow, and never changed afterwards (a deletion empties it) |
| `status` | `text` | no, `'pending'` | `active` | the states above |
| `status_reason` | `text` | yes | *null* | why a release is `failed`, for example `health check did not pass within 60 s (last error: connection refused)` |
| `build_log_tail` | `text` | yes | *null* | the last 40 lines of the build, or the last 50 lines of the child's output for a failed start, kept for failed releases |
| `status_at` | `timestamptz` | no, `now()` | 2026-10-30T09:14Z | when the status last changed |
| `label` | `text` | yes | `fix pagination` | a name, 1 to 80 characters |
| `tags` | `text[]` | no, `{}` | `{stable}` | free labels, lower case, up to 32 characters each, up to 10 per release; **they protect nothing** |
| `locked_by` | `text` | yes | *null* | who locked it by hand; null = not locked by hand |
| `locked_at` | `timestamptz` | yes | *null* | when; set together with `locked_by` |
| `created_by` | `text` | no | `alice@example.com` | who pressed Update (or changed the config) |
| `created_at` | `timestamptz` | no, `now()` | 2026-10-30T09:12Z | |
| `deleted_by` | `text` | yes | *null* | `appmux` for automatic clean-up |
| `deleted_at` | `timestamptz` | yes | *null* | set when deleted; the row stays |

**Example rows** (a selection of the app's rows):

| release_no | status | snapshot_digest | label | tags | locked_by | created_by | created_at | deleted_at |
|---|---|---|---|---|---|---|---|---|
| `3` | `failed` | *null* | | `{}` | | `alice@example.com` | 2026-10-27T11:40Z | 2026-10-29T10:05Z |
| `5` | `superseded` | `sha256:41be…` | `before pagination` | `{legacy-ui}` | `alice@example.com` | `alice@example.com` | 2026-10-27T15:02Z | |
| `6` | `superseded` | `sha256:77d0…` | | `{}` | | `alice@example.com` | 2026-10-29T10:20Z | |
| `7` | `active` | `sha256:9f2c…` | `fix pagination` | `{stable}` | | `alice@example.com` | 2026-10-30T09:12Z | |
| `8` | `failed` | `sha256:c3a8…` | | `{}` | | `alice@example.com` | 2026-10-30T09:30Z | |

Release 5 is locked by Alice (`locked_at` 2026-10-28T09:00Z) and stays until someone unlocks it. Release 7 is active, so it is protected without a lock of its own. Releases 6 and 8 are neither, so either may be deleted. Release 8's `status_reason` is `health check did not pass within 60 s (last error: connection refused)`.

`detected` of release 7 — what appmux found in the snapshot's files: the manifest it read from `app.yaml` ([design 5.4](v1-design.md#54-appyaml-requirements-and-start)) and the resources its `databricks.yml` declares, which the Requirements panel offers to attach:

```json
{
  "runtime": "python",
  "framework": "fastapi",
  "command": ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"],
  "env": [
    {"name": "LOG_LEVEL", "value": "info"},
    {"name": "OPENAI_API_KEY", "value_from": "openai-api-key"},
    {"name": "WAREHOUSE_ID", "value_from": "sql-warehouse"},
    {"name": "MLFLOW_EXPERIMENT_ID", "value_from": "experiment"}
  ],
  "bundle_resources": [
    {"key": "sql-warehouse", "type": "sql_warehouse", "target": "a1b2c3d4e5f67890", "permission": "CAN_USE"},
    {"key": "experiment", "type": "mlflow_experiment", "target": "456789012", "permission": "CAN_EDIT"},
    {"key": "openai-api-key", "type": "secret", "target": "sales-secrets/openai-api-key", "permission": "READ"},
    {"key": "forecast-model", "type": "serving_endpoint", "target": null, "permission": "CAN_QUERY"}
  ],
  "warnings": [
    "app.yaml: unknown key 'resources' ignored",
    "databricks.yml: 'permissions' and 'user_api_scopes' ignored",
    "databricks.yml: resource 'forecast-model' has no value yet (${var.endpoint}); fill it in when attaching"
  ]
}
```

`config` of release 7 — what the app admin adds without touching the child's source:

```json
{
  "env": [
    {"name": "LOG_LEVEL", "value": "info"},
    {"name": "WAREHOUSE", "value_from": "sql-warehouse"}
  ],
  "wheel_sources": ["/Workspace/Users/alice@example.com/wheels/sales"]
}
```

`wheels` of release 7 — the wheels the install used, so restarts, rebuilds and rollbacks need neither the source folders nor anyone's token. Only wheels that were **installed** are listed: a package the app environment already provides at the pinned version is not ([design 5.3](v1-design.md#53-build-and-package-sources)):

```json
[
  {"sha256": "3b1e…", "filename": "fastapi-0.115.0-py3-none-any.whl", "source": "/Workspace/Users/alice@example.com/wheels/sales"},
  {"sha256": "a07c…", "filename": "pydantic_core-2.23.4-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", "source": "/Workspace/Shared/appmux/wheelhouse"}
]
```

- **Required resource keys** of a release = every `value_from` in `detected.env` plus every `value_from` in `config.env`. The check against the app's resources runs before the build ([design 5.4](v1-design.md#54-appyaml-requirements-and-start)). `detected.bundle_resources` is only an offer: it grants and requires nothing.
- **Tags** are short free labels (`stable`, `legacy-ui`). They are not unique and have **no effect** on retention. `label` is a free-text name up to 80 characters.
- **Locks** are what protect a release ([below](#46-releases)).
- Resources are **not** part of a release: they belong to the app and take effect at the next start ([4.7](#47-app_resources)).

**DDL.**

```sql
CREATE TABLE releases (
  app_slug        text NOT NULL REFERENCES apps (slug) ON UPDATE CASCADE ON DELETE CASCADE,
  release_no      integer NOT NULL CHECK (release_no > 0),
  snapshot_digest text CHECK (snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
  detected        jsonb NOT NULL,                      -- set once, at creation
  config          jsonb NOT NULL DEFAULT '{}',         -- set once, at creation
  wheels          jsonb NOT NULL DEFAULT '[]',         -- set once, by the install
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'building', 'starting', 'healthy', 'active', 'failed', 'superseded')),
  status_reason   text,
  build_log_tail  text,
  status_at       timestamptz NOT NULL DEFAULT now(),
  label           text CHECK (label IS NULL OR char_length(label) BETWEEN 1 AND 80),
  tags            text[] NOT NULL DEFAULT '{}',        -- free labels; no effect on retention
  locked_by       text,                                -- a manual lock; the active release needs none
  locked_at       timestamptz,
  created_by      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  deleted_by      text,
  deleted_at      timestamptz,
  PRIMARY KEY (app_slug, release_no),
  CHECK ((deleted_at IS NULL) = (snapshot_digest IS NOT NULL)),   -- a live release has a snapshot; a deleted one has none
  CHECK ((deleted_at IS NULL) = (deleted_by IS NULL)),
  CHECK ((locked_at IS NULL) = (locked_by IS NULL)),
  CHECK (deleted_at IS NULL OR locked_at IS NULL)                  -- a release is unlocked before it is deleted
);
CREATE UNIQUE INDEX one_active_release ON releases (app_slug) WHERE status = 'active';   -- exactly one live release
```

`release_no` is `max(release_no) + 1` computed in the creating transaction; a clash is a unique violation and the transaction retries.

**Retention, locks and deleting releases.**

- **The cap.** A platform admin sets `max_releases_per_app` (default 10; for example 20). Nothing in the code fixes the number. Only live (not deleted) releases count.
- **Protected releases.** A release is protected, and cannot be deleted by anyone, while it is any of: **active** (the live one is locked by default, with no action and no column), the **target**, **in progress** (`pending`, `building`, `starting` or `healthy`), or **locked by hand**. A release that is none of these can be deleted.
- **Locks.** An app admin may lock any live release, for example a known good version, with `locked_by` and `locked_at`; a lock may be removed by any app admin. The platform setting `max_locked_releases` (default 3) caps the locks **per app**: locking a fourth is refused with `app 'sales' already has 3 locked releases; unlock one first`. Only manual locks count, never the implicit protection of the active release. The count and the lock happen in one transaction that first locks the app's `app_config` row (`SELECT … FOR UPDATE`), so two simultaneous locks cannot both pass the cap. A hand-locked release stays locked after it stops being active; one that is not locked is deletable as soon as it is superseded, unless it is the target.
- **Automatic clean-up** runs when an Update would create a release beyond the cap. It deletes the **oldest unprotected release**. The rule between the two settings ([4.1](#41-platform_settings)) guarantees there is one; if there is none anyway, the Update is refused: `app 'sales' has 10 releases and all are protected (active, target or locked); unlock one`.
- **Manual delete.** An app admin may delete any unprotected release. A confirmation shows its name and tags. To delete a locked release, unlock it first: two deliberate steps.
- **Roll back to any release.** An app admin may activate any live release, including one that never passed health (it shows its `failed` status; this is also how a release that failed on missing resources is started once they are attached).
- **What deletion does.** Sets `deleted_at` and `deleted_by`, clears `snapshot_digest`, `wheels` and `tags`. The clean-up job then removes content-store files that no live release references.

**Writer:** the appmux SP, on an app admin's action, and for status changes by the reconciler.

### 4.7 `app_resources`

**Purpose and rules.** The resources an app uses. **Each app declares its own**, as a standalone Databricks App does: they are not a subset of a platform list, and no platform admin chooses them. Each has a **key**, a **type**, a target and a permission. The child's `app.yaml` says `valueFrom: sql-warehouse`; appmux looks up the resource with that key and puts the value in the environment. A `secret` resolves to the secret's value, any other type to its `target` string (the value Databricks gives `valueFrom` for that type).

A resource gets in by one of two paths, and both end at the **same attach check** with the attacher's own token:
- **From the child's `databricks.yml`.** Update reads the snapshot's bundle file and offers its resources, already filled in, in the Requirements panel (`releases.detected.bundle_resources`, [4.6](#46-releases)). The app admin confirms or edits each one. Nothing is attached by reading the file.
- **By hand**, on the app page, when there is no bundle file or the admin wants another resource.

Types follow the Databricks list ([reference](../reference/databricks-apps-platform.md)). The third column is the value `valueFrom` yields; the last is the key a `databricks.yml` uses for the type, which is how the offer is built:

| `type` | Databricks resource | Value for `valueFrom` (`target`) | Permission levels | In `databricks.yml` |
|---|---|---|---|---|
| `secret` | Secret | a **reference** `scope/key`; the value is read from the scope at start | `READ` | `secret` |
| `sql_warehouse` | SQL warehouse | warehouse id | `CAN_USE`, `CAN_MANAGE` | `sql_warehouse` |
| `mlflow_experiment` | MLflow experiment | experiment id | `CAN_READ`, `CAN_EDIT`, `CAN_MANAGE` | `experiment` |
| `serving_endpoint` | Model serving endpoint | endpoint name | `CAN_VIEW`, `CAN_QUERY`, `CAN_MANAGE` | `serving_endpoint` |
| `lakebase_database` | Lakebase database | endpoint path (Autoscaling) or host (Provisioned) | `CAN_CONNECT_AND_CREATE` | `database` or `postgres` |
| `job` | Lakeflow job | job id | `CAN_VIEW`, `CAN_MANAGE_RUN`, `CAN_MANAGE` | `job` |
| `uc_table` | Unity Catalog table | `catalog.schema.table` | `SELECT`, `MODIFY` | `uc_securable`, `securable_type: TABLE` |
| `uc_volume` | Unity Catalog volume | `/Volumes/catalog/schema/volume` | `READ_VOLUME`, `WRITE_VOLUME` | `uc_securable`, `securable_type: VOLUME` |
| `uc_function` | User-defined function | `catalog.schema.function` | `EXECUTE` | `uc_securable`, `securable_type: FUNCTION` |
| `uc_connection` | Unity Catalog connection | connection name | `USE_CONNECTION` | `uc_securable`, `securable_type: CONNECTION` |
| `genie_space` | Genie space | space id | `CAN_VIEW`, `CAN_RUN`, `CAN_EDIT`, `CAN_MANAGE` | `genie_space` |
| `vector_search_index` | AI Search index | `catalog.schema.index` | `SELECT` | none: attach by hand |
| `databricks_app` | Databricks app | app name | `CAN_USE` | `app` |
| `gateway_model_service` | Unity Gateway model service | `catalog.schema.service` | `EXECUTE` | `uc_securable`, `securable_type: MODEL_SERVICE` |

The catalogue lives in code, so a new Databricks type is a code change; the table does not constrain the type name. Permission names are appmux's spelling of the Databricks level names. Reading a bundle entry: its `name` becomes the key (the admin renames it if it breaks the key pattern); a permission that is not a level of the type (for example `IS_OWNER`, or `WRITE` on a secret, which appmux never needs) is shown as a warning and the admin picks a level; a value that is a `${…}` interpolation is shown as unresolved and the admin types it. The file's `config`, `permissions`, `user_api_scopes`, targets and variables are ignored with a warning: appmux never honours a grant that a file declares.

**Which identity uses it.** Databricks sets the app's service principal permission when a resource is added; a child has no service principal. So each non-secret resource says `access_as`: `obo` (the visitor), `own_sp` (the app's own existing service principal) or `shared_sp` (the appmux service principal), `shared_sp` needing its delegation and `own_sp` its own-SP keys; `obo` can be declared at any time, because the delegation gates only whether the visitor's token reaches the child. appmux **never grants** access to a resource: it checks that the person attaching it could use it themselves, with their own token, and stores the result (`ok`, `unverified`; `shared_sp` accepts only `ok`). The identities, the attach-check steps and the reasons are in [design 8.2](v1-design.md#82-resources-and-env-entries); the check proves access at attach time and is not repeated.

**Secrets are references.** A `secret` resource names an existing Databricks secret, `scope/key`, instead of holding a value, so an app admin attaches a secret the way they attach any other resource and types no value into appmux. `access_as` is empty: the value is read **by the appmux service principal when the child starts**, and the child receives it in its environment. The appmux SP needs `READ` on the scope, which only the scope's manager can give and appmux never gives; a start that cannot read the value fails with `cannot read secret 'sales-secrets/openai-api-key': ask the scope's manager to give the appmux service principal READ on scope 'sales-secrets'`. Spike 11 (Free Edition) verified that an App's service principal reads a value with only `READ` on the scope ([design 8.3](v1-design.md#83-secrets-where-the-value-comes-from)); a revoked `READ` keeps working for a few minutes, so a start may succeed or fail for that long after the manager removes it.

**Attach checks.** The check runs with the attacher's token, as a read-only call per type; the one that worked on Free Edition (spike 16) and the OAuth scope the appmux App must declare for it are below. "Level" means the response carries the caller's own permission level; where it does not, a successful call proves the attacher can reach the object, not which level they hold.

| `type` | Check (as the attacher) | App scope | Result when it fails |
|---|---|---|---|
| `secret` | `SELECT length(secret('scope', 'key'))` on a SQL warehouse; returns the length, never the value, and a missing key fails like no `READ` | `sql` | `FAILED` statement (HTTP is 200; read `status.state`) |
| `sql_warehouse` | `SELECT 1` on the warehouse (a use test; it can start the warehouse) | `sql` | `FAILED`; `GET /sql/warehouses/{id}` is open to any identity and proves nothing |
| `serving_endpoint` | `GET /serving-endpoints/{name}`; **level** in `permission_level` | `serving.serving-endpoints:read` or `model-serving` | 403 or 404 |
| `uc_table` | `GET /unity-catalog/tables/{name}` | `catalog.tables:read` | 403 |
| `uc_volume` | `GET /fs/directories/Volumes/…` (the Files API) | `files.files:read` or `files` | 403 |
| `uc_function` | `DESCRIBE FUNCTION` on a SQL warehouse | `sql` | `FAILED` statement |
| `uc_connection` | `GET /unity-catalog/connections/{name}` | `catalog.connections:read` | 403 |
| `genie_space` | `GET /genie/spaces/{id}` | `dashboards.genie:read` or `genie` | 403 |
| `mlflow_experiment`, `job` | none: the call needs a scope (`mlflow`, `jobs`) that no App accepts | — | stored `unverified` |
| `databricks_app` | none: `GET /apps/{name}` answers any identity | — | stored `unverified` |
| `vector_search_index`, `gateway_model_service` | none tested: Free Edition has no vector search endpoint or gateway service | — | stored `unverified` |
| `lakebase_database` | a login with the attacher's OBO token ([spike 1](../../specs/001-appmux-v1/spikes/README.md#spike-1--obo-login-to-lakebase-2026-10-30)); Lakebase checks the token, not its scopes | none | stored `unverified`: only an expired token was refused |

The checks of `secret`, `sql_warehouse` and `uc_function` run a statement, so they need a SQL warehouse the attacher can use, running or able to start; which one is a plan decision. The REST calls for a volume and a function need the scope `unity-catalog`, which no App accepts, so `uc_volume` uses the Files API and `uc_function` uses SQL.

**Columns.**

| Column | Type | Null / default | Example (`sql-warehouse`) | Meaning |
|---|---|---|---|---|
| `app_slug` | `text` | part of the key | `sales` | the app; deleted and renamed with it |
| `key` | `text` | part of the key | `sql-warehouse` | the name `valueFrom` uses; lower case letters, digits, `.`, `_` and `-`, up to 63 characters |
| `type` | `text` | no | `sql_warehouse` | a type of the catalogue above |
| `target` | `text` | no | `a1b2c3d4e5f67890` | the object, or `scope/key` for a secret |
| `permission` | `text` | no | `CAN_USE` | a level of the type; `READ` for a secret |
| `access_as` | `text` | yes | `obo` | `obo`, `own_sp` or `shared_sp`; null exactly for a secret |
| `check_result` | `text` | no | `ok` | `ok`: the attacher's token reached it (the check per type is above); `unverified`: no usable check for this type or scope |
| `attached_by` | `text` | no | `alice@example.com` | the person whose token made the check |
| `attached_at` | `timestamptz` | no, `now()` | 2026-10-30T08:55Z | |

**Example rows** for `sales`:

| key | type | target | permission | access_as | check_result | attached_by | attached_at |
|---|---|---|---|---|---|---|---|
| `experiment` | `mlflow_experiment` | `456789012` | `CAN_EDIT` | `own_sp` | `ok` | `alice@example.com` | 2026-10-30T09:01Z |
| `forecast-model` | `serving_endpoint` | `sales-forecast` | `CAN_QUERY` | `obo` | `unverified` | `alice@example.com` | 2026-10-30T09:02Z |
| `openai-api-key` | `secret` | `sales-secrets/openai-api-key` | `READ` | *null* | `ok` | `alice@example.com` | 2026-10-30T08:58Z |
| `sp-client-id` | `secret` | `sales-secrets/sp-client-id` | `READ` | *null* | `ok` | `alice@example.com` | 2026-10-30T09:03Z |
| `sp-client-secret` | `secret` | `sales-secrets/sp-client-secret` | `READ` | *null* | `ok` | `alice@example.com` | 2026-10-30T09:04Z |

**DDL.**

```sql
CREATE TABLE app_resources (
  app_slug     text NOT NULL REFERENCES apps (slug) ON UPDATE CASCADE ON DELETE CASCADE,
  key          text NOT NULL CHECK (key ~ '^[a-z0-9][a-z0-9._-]{0,62}$'),
  type         text NOT NULL,                          -- a type of the catalogue in code
  target       text NOT NULL CHECK (btrim(target) <> ''),
  permission   text NOT NULL,                          -- a level of the type, validated in code
  access_as    text CHECK (access_as IN ('obo', 'own_sp', 'shared_sp')),
  check_result text NOT NULL CHECK (check_result IN ('ok', 'unverified')),
  attached_by  text NOT NULL,
  attached_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (app_slug, key),
  CHECK ((type = 'secret') = (access_as IS NULL)),                  -- a secret is read by appmux, not used as a credential
  CHECK (type <> 'secret' OR target ~ '^[^/]+/[^/]+$'),             -- scope/key
  CHECK (NOT (access_as = 'shared_sp' AND check_result <> 'ok'))    -- shared_sp only after a verified check
);
```

**DDL, last: the composite keys of `app_config`** ([4.4](#44-app_config)). Run after the `CREATE TABLE` of `releases` and `app_resources`.

```sql
ALTER TABLE app_config
  ADD FOREIGN KEY (app_slug, target_release_no) REFERENCES releases (app_slug, release_no) DEFERRABLE INITIALLY DEFERRED,
  ADD FOREIGN KEY (app_slug, own_sp_client_id_key)     REFERENCES app_resources (app_slug, key) DEFERRABLE INITIALLY DEFERRED,
  ADD FOREIGN KEY (app_slug, own_sp_client_secret_key) REFERENCES app_resources (app_slug, key) DEFERRABLE INITIALLY DEFERRED;
```

**When changes apply.** Resources belong to the app, not to a release (as in Databricks). An attached, changed or detached resource takes effect when the app next starts; the app page shows "restart to apply". A rollback runs an old release against the **current** resources, so a release whose requirements are no longer met fails before it starts ([design 5.4](v1-design.md#54-appyaml-requirements-and-start)). **Writer:** app admins, through the appmux SP, after the attach check.

### 4.8 `metrics`

**Purpose and rules.** One row per app per minute while the app runs, flushed from memory in one batch per minute and kept 7 days ([8](#8-retention-and-volume)). The platform's own row uses `app_slug = '_platform'`, so there is no foreign key; it fills the eight platform columns, which are null in an app's row. Counts add up across minutes; percentiles do not, so latency is a histogram of 11 counts ([design 10.2](v1-design.md#102-metrics)).

**Columns.**

| Column | Type | Null / default | Example (`sales`, 09:15) | Meaning |
|---|---|---|---|---|
| `app_slug` | `text` | part of the key | `sales` | the app, or `_platform` |
| `minute` | `timestamptz` | part of the key | 2026-10-30T09:15Z | start of the minute |
| `requests` | `integer` | no, `0` | `412` | requests at the edge |
| `r2xx`, `r3xx`, `r4xx`, `r5xx` | `integer` | no, `0` | `389`, `11`, `9`, `3` | by status class; they add up to `requests` |
| `upstream_errors` | `integer` | no, `0` | `2` | 502/504 the edge made itself (child unreachable, timeout) |
| `limited` | `integer` | no, `0` | `0` | refused by the concurrency, body-size or rate caps (429, 413) |
| `lat_hist` | `integer[]` | no | `{18,61,140,88,52,29,14,6,3,1,0}` | 11 counts; upper bounds in ms: 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, above |
| `bytes_in`, `bytes_out` | `bigint` | no, `0` | `184320`, `5242880` | traffic through the edge |
| `in_flight_max` | `integer` | no, `0` | `7` | peak concurrent requests |
| `ws_opened`, `ws_open` | `integer` | no, `0` | `0`, `0` | WebSockets opened in the minute; open at its end |
| `rss_mb_max` | `integer` | no, `0` | `341` | peak resident memory of the child's process tree, in MB |
| `restarts` | `integer` | no, `0` | `0` | restarts in the minute |
| `egress_bytes_out`, `egress_bytes_in` | `bigint` | no, `0` | `91234`, `2310456` | through the pump |
| `egress_dropped` | `integer` | no, `0` | `0` | packets dropped by egress rules |
| `egress_dropped_by_rule` | `jsonb` | no, `{}` | `{}` | drops by rule id (`app:2` is the second rule of the app's list, `global:1` the first global rule); spots a child probing blocked destinations |
| `mem_used_mb`, `mem_limit_mb` | `integer` | yes | *null* | `_platform` only: memory in use and the container's limit |
| `children_running`, `children_registered` | `integer` | yes | *null* | `_platform` only |
| `admission_rejections`, `registry_errors` | `integer` | yes | *null* | `_platform` only |
| `registry_ms_max`, `edge_rss_mb` | `integer` | yes | *null* | `_platform` only: slowest registry call in the minute; the edge's own memory |

The p50 of that minute: the running totals of `lat_hist` are 18, 79, 219, 307 …, and the 206th of 412 requests falls in the third bucket, so the dashboard reports "at most 25 ms".

**Example rows:**

| app_slug | minute | requests | r5xx | rss_mb_max | mem_used_mb | mem_limit_mb | children_running | children_registered | edge_rss_mb |
|---|---|---|---|---|---|---|---|---|---|
| `sales` | 2026-10-30T09:15Z | `412` | `3` | `341` | | | | | |
| `_platform` | 2026-10-30T09:15Z | `412` | `3` | `0` | `1500` | `6144` | `1` | `2` | `160` |

**DDL.**

```sql
CREATE TABLE metrics (
  app_slug               text NOT NULL,                   -- no foreign key: '_platform' is not an app
  minute                 timestamptz NOT NULL,
  requests               integer NOT NULL DEFAULT 0,
  r2xx                   integer NOT NULL DEFAULT 0,
  r3xx                   integer NOT NULL DEFAULT 0,
  r4xx                   integer NOT NULL DEFAULT 0,
  r5xx                   integer NOT NULL DEFAULT 0,
  upstream_errors        integer NOT NULL DEFAULT 0,
  limited                integer NOT NULL DEFAULT 0,
  lat_hist               integer[] NOT NULL CHECK (cardinality(lat_hist) = 11),
  bytes_in               bigint NOT NULL DEFAULT 0,
  bytes_out              bigint NOT NULL DEFAULT 0,
  in_flight_max          integer NOT NULL DEFAULT 0,
  ws_opened              integer NOT NULL DEFAULT 0,
  ws_open                integer NOT NULL DEFAULT 0,
  rss_mb_max             integer NOT NULL DEFAULT 0,
  restarts               integer NOT NULL DEFAULT 0,
  egress_bytes_out       bigint NOT NULL DEFAULT 0,
  egress_bytes_in        bigint NOT NULL DEFAULT 0,
  egress_dropped         integer NOT NULL DEFAULT 0,
  egress_dropped_by_rule jsonb NOT NULL DEFAULT '{}',
  mem_used_mb            integer,                          -- the next eight: '_platform' rows only
  mem_limit_mb           integer,
  children_running       integer,
  children_registered    integer,
  admission_rejections   integer,
  registry_errors        integer,
  registry_ms_max        integer,
  edge_rss_mb            integer,
  PRIMARY KEY (app_slug, minute),
  CHECK ((app_slug = '_platform') = (mem_used_mb IS NOT NULL))
);
```

**Writer:** appmux SP, one batch per minute; the hourly delete removes rows older than 7 days.

### 4.9 `audit_log`

**Purpose and rules.** The record of every administrative action: who, what, on which app, with which reason. Append-only: no role has `UPDATE` or `DELETE`. Written in the same transaction as the change, by whichever identity made it (so both write identities hold `INSERT`). The actor is the email from the proxy headers, `db:<role>` for a manual change, or `appmux` for automatic clean-up and for its own start-up record of the service principal. Actions that pass a risk through a gate **require a reason**. No UI in v1; read it in SQL. There is no foreign key to `apps`, so entries outlive the apps they describe.

**Columns.**

| Column | Type | Null / default | Example | Meaning |
|---|---|---|---|---|
| `id` | `bigint` | primary key, generated | `412` | orders entries that share a timestamp |
| `ts` | `timestamptz` | no, `now()` | 2026-10-30T08:44Z | when |
| `actor` | `text` | no | `dana@example.com` | an email, `db:<role>`, or `appmux` |
| `action` | `text` | no | `request.approve` | one of the names below |
| `app_slug` | `text` | yes | `sales` | null for platform-wide actions such as `settings.edit` |
| `detail` | `jsonb` | no, `{}` | `{"request": 1, "kind": "delegation"}` | what changed; **never a secret value** |
| `reason` | `text` | yes | `the app reads per-viewer data only` | mandatory for gate actions |

**Example rows:**

| ts | actor | action | app_slug | detail | reason |
|---|---|---|---|---|---|
| 2026-10-28T09:00Z | `alice@example.com` | `release.lock` | `sales` | `{"release": 5, "locks": "1 of 3"}` | |
| 2026-10-29T10:05Z | `alice@example.com` | `release.delete` | `sales` | `{"release": 3}` | |
| 2026-10-30T08:41Z | `dana@example.com` | `app.register` | `sales` | `{"owner": "alice@example.com", "budget_mb": 512}` | |
| 2026-10-30T08:43Z | `alice@example.com` | `request.create` | `sales` | `{"request": 1, "kind": "delegation"}` | dashboard queries the SQL warehouse as the viewer |
| 2026-10-30T08:44Z | `dana@example.com` | `request.approve` | `sales` | `{"request": 1, "kind": "delegation", "after": {"obo": true}}` | the app reads per-viewer data only |
| 2026-10-30T08:49Z | `dana@example.com` | `request.approve` | `docs` | `{"request": 2, "kind": "settings", "before": {"display_name": "Docs", "memory_budget_mb": 128}, "after": {"display_name": "Team docs", "memory_budget_mb": 256}}` | |
| 2026-10-30T08:52Z | `alice@example.com` | `grant.app` | `sales` | `{"principal": "group:sales-team", "permission": "CAN_USE"}` | |
| 2026-10-30T08:55Z | `alice@example.com` | `resource.attach` | `sales` | `{"key": "sql-warehouse", "type": "sql_warehouse", "access_as": "obo", "check": "ok", "from": "databricks.yml"}` | |
| 2026-10-30T08:58Z | `alice@example.com` | `resource.attach` | `sales` | `{"key": "openai-api-key", "type": "secret", "target": "sales-secrets/openai-api-key", "check": "ok"}` | |
| 2026-10-30T09:12Z | `alice@example.com` | `release.create` | `sales` | `{"release": 7, "digest": "sha256:9f2c…"}` | |
| 2026-10-30T09:14Z | `alice@example.com` | `release.activate` | `sales` | `{"release": 7}` | |
| 2026-10-30T09:40Z | `appmux` | `release.delete` | `sales` | `{"release": 1, "by": "retention", "cap": 10}` | |
| 2026-10-30T09:45Z | `dana@example.com` | `release.view_source` | `sales` | `{"release": 7, "path": "app.py"}` | |
| 2026-10-30T10:20Z | `dana@example.com` | `request.deny` | `docs` | `{"request": 8, "kind": "settings", "automatic": true}` | slug 'reports' is already taken (automatic) |
| 2026-11-06T07:02Z | `appmux` | `platform.service_principal` | | `{"client_id": "0d4c1b84-3f5a-4a52-9a52-6a9c1f3e7b10", "previous_client_id": "5b7e2c10-91d4-4c3e-8a67-2f0e5d8a1c44", "automatic": true}` | |
| 2026-11-06T07:02Z | `appmux` | `app.stop` | `sales` | `{"automatic": true}` | the appmux service principal changed |

Action names: `app.register`, `app.delete`, `app.rename`, `app.view_as_user`, `app.set_owner`, `app.edit_platform_fields`, `app.start`, `app.stop`, `app.restart`, `request.create`, `request.withdraw`, `request.approve`, `request.deny`, `delegation.grant`, `delegation.revoke`, `platform_admin.add`, `platform_admin.remove`, `grant.app`, `egress.edit`, `settings.edit`, `resource.attach`, `resource.detach`, `config.edit`, `release.create`, `release.activate`, `release.tag`, `release.untag`, `release.label`, `release.lock`, `release.unlock`, `release.delete`, `release.view_source`, `app.view_logs` (the first read of an app's output by an actor in 10 minutes; no content), `platform.service_principal` (appmux's own record of the application id it runs as; actor `appmux`, no `app_slug`; `detail` holds `client_id` and, when it replaced another, `previous_client_id`; see below).

**The service principal record.** At every start appmux reads the newest rows with actor `appmux` and action `platform.service_principal` (the `audit_by_actor` index), takes the first whose `detail.client_id` has the shape of an application id, and compares it with the id it runs as now ([design 5.6](v1-design.md#56-crash-restart-and-shutdown), [ADR 0022](../decisions/0022-stop-every-app-when-the-appmux-service-principal-changes.md)). No such row writes one and stops nothing; the same id writes nothing; another id writes a row with `previous_client_id` and stops every running app in the same transaction, one `app.stop` row each. It is a log row, not a table or a column, so no migration is needed and no role can delete it. Anyone who can insert audit rows (the shared-SP token holds that right, [design 8.4](v1-design.md#84-shared-sp-what-it-reaches-and-what-can-be-done)) can forge one, and the most it does is stop the apps at the next start.

**DDL.**

```sql
CREATE TABLE audit_log (
  id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts       timestamptz NOT NULL DEFAULT now(),
  actor    text NOT NULL,
  action   text NOT NULL,                           -- one of the names above, checked in code
  app_slug text,                                    -- no foreign key
  detail   jsonb NOT NULL DEFAULT '{}',
  reason   text
);
CREATE INDEX audit_by_app ON audit_log (app_slug, ts DESC);
CREATE INDEX audit_by_actor ON audit_log (actor, ts DESC);
```

**Writer:** either identity, insert only.

## 5. Walkthroughs

Each scenario lists the rows that change, so the model can be checked against the business flow. All use the data above.

### 5.1 Register an app

Dana registers `sales`. In one transaction through Dana's own database role: insert `apps` (slug, owner Alice, `source_path`, budget; both delegation flags false), insert the `app_config` row (stopped, no target), insert `audit_log` `app.register`. No release exists yet; nothing starts.

**By request.** Grace, who can open appmux but owns no app, presses **Request a new app** on the launcher and fills in the slug `reports`, a name, the folder and a reason (the form has no memory field). Through the appmux SP: insert `permission_requests (NULL, register, {slug, display_name, source_path}, reason, grace)` (request 7) and `audit_log` `request.create`. The launcher lists the request as waiting, with a **Withdraw** button. Dana opens the inbox, reads it with the platform default as the budget and the free memory next to it, and presses **Approve**, typing a reason. Through Dana's own role, in one transaction: check that no app has the slug `reports`, insert `apps` (owner Grace, `created_by` Dana, `memory_budget_mb` = `default_memory_budget_mb` = 512, both delegation flags false) and `app_config` (stopped, no target), set the request's `app_slug` to `reports`, close it (`approved`, `closed_by` Dana) and insert `audit_log` `request.approve`. Grace finds `reports` on the launcher as its owner and presses Update. Refusals, with nothing written: a slug that an app already has or that another `register` request is waiting for, and a fourth pending `register` request from one person. If another request takes the slug before Dana presses Approve, Approve closes this one as `denied` automatically ([4.3](#43-permission_requests)).

### 5.2 Ask a platform admin, and approve

**A delegation.** Alice opens the app page, which says `OBO: not allowed` with a **Request** button. Alice types the reason. Through the appmux SP: insert `permission_requests (sales, delegation, {"obo": true}, reason, alice)` and `audit_log` `request.create`. The page now says `OBO: requested, waiting for a platform admin` with a **Withdraw** button. Dana sees `1 pending` on the launcher, opens the inbox, reads the request and presses **Approve**, typing a reason. Through Dana's own role, in one transaction: lock the `sales` row of `apps`, check the request again, set `apps.obo_delegated = true`, close the request (`approved`, `closed_by` Dana), insert `audit_log` `request.approve`. From the next request the edge stops stripping `X-Forwarded-Access-Token` for `sales`. Refusals, with nothing written: a second `delegation` request for `sales` while one is pending; a request for a flag that is already set; a `delegation` request with `shared_sp` while `shared_sp_delegation_allowed` is off. A request with both flags is one form with two gates: the approval screen lists the exposure of each, and Dana approves or denies both.

**Settings.** Frank, the owner of `docs`, wants 256 MB instead of 128 MB and a clearer name (request 2 in [4.3](#43-permission_requests)). On the app page Frank changes the **Settings** form (name `Docs` → `Team docs`, memory budget 128 → 256) and enters a reason. Through the appmux SP: insert `permission_requests (docs, settings, {"display_name": "Team docs", "memory_budget_mb": 256}, reason, frank)` and `audit_log` `request.create`. The inbox row reads `docs · name: Docs → Team docs · memory budget: 128 MB → 256 MB`, with the free memory beside it. Dana approves with only a note, because neither field is a review gate (`source_path` would be). Through Dana's own role, in one transaction: lock the `docs` row of `apps`, set `display_name` and `memory_budget_mb = 256`, add 1 to `version`, close the request, insert `audit_log` `request.approve` with the values before and after. The name shows on the launcher at once; the running `docs` keeps its limit, and the new budget counts from its next start, when admission control decides whether it fits ([design 5.7](v1-design.md#57-memory-and-admission-control)). Refusals, with nothing written: a budget outside 64 to 16384; a request in which every field equals the app's current value; a `settings` request with no field; a second pending `settings` request for `docs`.

### 5.3 Attach a resource

Alice adds the SQL warehouse `a1b2c3d4e5f67890` as key `sql-warehouse`, `CAN_USE`, `access_as = obo`, either from the offer in the Requirements panel (filled in from the snapshot's `databricks.yml`, [5.4](#54-update-with-requirements)) or by typing the same values.
1. `authorize` passes (`CAN_MANAGE` on `sales`). `apps.obo_delegated` is not read: declaring `obo` is setting.
2. Using Alice's token, appmux reads the warehouse; the call succeeds → `check_result = ok`.
3. Insert `app_resources` and `audit_log` `resource.attach` (its `detail` says `"from": "databricks.yml"` when the offer was used).

A secret is attached the same way, and no value is typed. Alice picks `sales-secrets/openai-api-key` (offered by the bundle file, or typed). Using Alice's token, appmux lists the keys of scope `sales-secrets` and finds `openai-api-key` → `check_result = ok`; the row holds the reference only. When the child starts, the appmux SP reads the value. If the scope's manager has not yet given the appmux SP `READ` on `sales-secrets`, the start fails with the message of [4.7](#47-app_resources) and nothing else is affected.

Refusals, with nothing written: Alice cannot read the warehouse (`you do not have access to this SQL warehouse`); Alice cannot list the scope (`you do not have READ on secret scope 'sales-secrets'`); `access_as = shared_sp` for a type that cannot be checked (`cannot verify access to this resource type; shared_sp requires a verified check`); `access_as = shared_sp` while `sales` is not delegated the shared service principal.

### 5.4 Update, with requirements

Alice presses Update (Alice's own token is in the request).
1. `authorize(app.update)`. appmux reads `source_path` and the wheel sources with **Alice's token**, computes the digest and stores the snapshot in the content store (or finds it). Wheels are fetched only for packages the app environment does not already provide at the pinned version ([design 5.3](v1-design.md#53-build-and-package-sources)).
2. appmux reads the snapshot's `app.yaml` and writes the manifest into `detected`; if the snapshot has a `databricks.yml` it also writes the resources of the chosen app into `detected.bundle_resources`, as an offer only. An invalid `app.yaml` refuses the Update here ([design 5.4](v1-design.md#54-appyaml-requirements-and-start)).
3. If live releases are 10 of 10, the oldest unprotected one (not active, target, in progress or locked) is deleted first ([4.6](#46-releases)) and logged as `release.delete` by `appmux`.
4. Insert `releases` (`release_no = 9`, status `pending`, `wheels` filled by the Update flow, as in step 1), `audit_log` `release.create`; set `app_config.target_release_no = 9`.
5. The reconciler checks the required resource keys against `app_resources`. If `openai-api-key` and `sql-warehouse` are missing, release 9 becomes `failed` with `status_reason = "missing resources: openai-api-key, sql-warehouse"` and nothing is built. Alice attaches them ([5.3](#53-attach-a-resource)) and presses **Activate** on release 9: it starts from the stored snapshot, with no source read and no token.
6. With the requirements met, the reconciler builds and starts release 9 beside the running one; after the health check release 9 becomes `active` and the previous release `superseded` in one transaction.

The token is held in memory for step 1 only; it is never stored. An Update that outlasts it fails with `session expired during the snapshot; press Update again`.

### 5.5 Roll back, name, tag, lock and delete

Alice picks release 5 and presses Activate: `app_config.target_release_no = 5`, `audit_log` `release.activate`. The reconciler starts release 5 from its stored snapshot and wheels and swaps after the health check. Alice tags release 7 `stable` and names release 5 `before pagination` (`releases.tags` and `releases.label`); neither protects anything. Alice **locks** release 5 (`locked_by`, `locked_at`, `audit_log` `release.lock`): it counts as 1 of the 3 locks the app may hold. Alice deletes release 6: allowed, because it is not active, not the target, not in progress and not locked. `deleted_at` is set, `snapshot_digest`, `wheels` and `tags` are cleared, `audit_log` `release.delete`; the clean-up job removes files nobody else references. Alice tries to delete release 5: refused, `release 5 is locked; unlock it first`. When release 7 is replaced by release 9 it stops being active; as it holds no lock, it becomes deletable at once.

### 5.6 Delete an app

Dana deletes `sales`: the app is stopped first, then `DELETE FROM apps WHERE slug = 'sales'`. Cascades remove `app_config`, `app_grants`, `app_resources`, `permission_requests` and `releases`. `audit_log` and `metrics` stay. The clean-up job removes content-store files nobody references.

### 5.7 Rename an app

Alice asks to rename `sales` to `revenue` (request 4 in [4.3](#43-permission_requests), where it is still waiting: kind `settings`, payload `{"slug": "revenue"}`, a reason). The request runs as in [5.2](#52-ask-a-platform-admin-and-approve). When Dana presses **Approve**, through Dana's own role and in one transaction: lock the `sales` row of `apps`; check that no app has `revenue`; `UPDATE apps SET slug = 'revenue'` and add 1 to `version`; close the request; insert `audit_log` `request.approve` and `app.rename` (`{"from": "sales", "to": "revenue"}`, under the new slug). If an app has `revenue` by then, the transaction instead closes the request as `denied` with `closed_by` Dana and the note `slug 'revenue' is already taken (automatic)`, and nothing else changes.

`ON UPDATE CASCADE` rewrites `app_slug` in `app_config`, `app_grants`, `app_resources`, `releases` and `permission_requests`; the composite keys of `app_config` are deferred, so their check runs at commit, after every table has the new slug. `metrics` and `audit_log` have no foreign key and keep `sales`: the metrics age out in 7 days, and an audit row names the slug of its time. The content store is keyed by digest, so no file moves. The reconciler sees the new slug at its next read: a running `revenue` restarts through the normal swap under the new `APPMUX_BASE_PATH` and `DATABRICKS_APP_NAME`, the old URL answers `404` from then on, and a stopped app needs nothing more ([design 5.5](v1-design.md#55-health-and-swap)). Refusals, with nothing written: a slug that fails the form check; the slug the app already has.

## 6. Who writes what

Two database identities write the registry ([design 4.2](v1-design.md#42-who-writes-what)):

| Identity | Used for | Tables it may write |
|---|---|---|
| **Platform admin's own role** (a member of the group role `appmux_platform_writers`; the login is that person's OBO token) | what decides what runs in the slot | `apps` (insert, update, delete), `platform_settings`, `permission_requests` (update: answer), `app_config` (insert only: the first row), `audit_log` (insert) |
| **appmux service principal** | everything an app admin does, and results | `app_config` (update), `app_grants`, `releases`, `app_resources`, `metrics`, `permission_requests` (insert, update: ask and withdraw), `audit_log` (insert) |

Indicative grants (the plan fixes names; spike 9 ran them as written and they give exactly the table above, [log](../../specs/001-appmux-v1/spikes/README.md#spike-9--cascade-privileges-on-lakebase-and-the-slirp4netns-licence-2026-10-30)):

```sql
CREATE ROLE appmux_platform_writers NOLOGIN;
GRANT INSERT, UPDATE, DELETE ON apps, platform_settings TO appmux_platform_writers;
GRANT UPDATE ON permission_requests TO appmux_platform_writers;
GRANT INSERT ON app_config, audit_log TO appmux_platform_writers;

GRANT INSERT, UPDATE ON releases, permission_requests TO appmux_sp;
GRANT INSERT, UPDATE, DELETE ON app_grants, app_resources TO appmux_sp;
GRANT INSERT, DELETE ON metrics TO appmux_sp;
GRANT UPDATE ON app_config TO appmux_sp;
GRANT INSERT ON audit_log TO appmux_sp;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO appmux_sp, appmux_platform_writers;
REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC;
```

The split by table needs no column-level grant. Lakebase adds its own grants to every new table (spike 9): `databricks_gateway` and `databricks_reader_<n>` can read it, and `databricks_superuser` holds every privilege on it with grant option, so the split binds the two roles above and not a member of `databricks_superuser` (the project owner's role is one by default, spike 1). The `REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC` line removes nothing, since `PUBLIC` holds no such privilege; the log is append-only because no appmux role is granted `UPDATE` or `DELETE` on it, which the run confirmed for both. A platform admin's implicit `CAN_MANAGE` on every app ([design 9.1](v1-design.md#91-the-model)) changes none of this: when a platform admin edits an app's configuration, `authorize()` passes and the appmux SP writes, as for any app admin, and the audit row names the platform admin. A rename is the one platform write that reaches other tables: `UPDATE apps SET slug` cascades into `app_config`, `app_grants`, `app_resources`, `releases` and `permission_requests`, which the platform admin's role cannot write directly; like the delete cascade, it works under that role's privileges, because PostgreSQL runs the cascaded writes with the rights of the table owner (spike 9). The residual: a child holding the shared-SP token can write everything in the second row — any app's grants, config, releases, resources (secret references) and requests, of any kind (a request to register an app included) and in anyone's name — but **cannot** write an `apps` row (so no delegation flag, name, slug, budget, source folder or egress rule) or a setting (so no platform admin and no master switch). It can also bypass the attach check ([4.7](#47-app_resources)) by writing `app_resources` directly, which is why the shared-SP delegation is a review gate.

## 7. Rules enforced in code

These cannot be stated as `CHECK` constraints; each has a test.

| # | Rule | Test |
|---|---|---|
| 1 | The owner cannot be removed or demoted: `apps.owner` is always `CAN_MANAGE`; `app_grants` cannot name the owner with a lower level | table-driven `authorize` rows |
| 2 | `platform_admins` always keeps at least one `user` entry | removing the last one is refused |
| 3 | `app_config.own_sp_*_key` point to `secret` resources of the same app | setting a non-secret key is refused |
| 4 | `access_as = 'obo'` needs `apps.obo_delegated`; `'shared_sp'` needs `apps.shared_sp_delegated` **and** `shared_sp_delegation_allowed`; `'own_sp'` needs `app_config.own_sp_*` | attach refusals in [5.3](#53-attach-a-resource) |
| 5 | A resource's `permission` is a level of its `type` | catalogue test |
| 6 | The attach check runs with the attacher's token and refuses on "no access"; `unverified` never reaches `shared_sp` | fixtures with a stub for the Databricks calls |
| 7 | **Requests, state machine:** one pending request per (app, kind), or per (proposed slug, `register`) (the partial unique index); withdraw only while pending, by the requester (for any kind but `register`, also an app admin); approve is one transaction (lock the `apps` row, or for `register` check that no app has the slug; check again, apply, close, audit with the values before and after, and with a reason when a gate field is present); a taken slug closes the request as `denied` automatically instead of failing; the request's `status` and `payload` are never read by `authorize` or the reconciler | state-machine tests per kind, including a forged `approved` row that must change nothing |
| 8 | A release may be deleted only if it is not the target, not active, not in progress and not locked | delete refusals |
| 9 | Automatic clean-up deletes the oldest release that meets none of the conditions in rule 8; if nothing is deletable the Update is refused | clean-up tests with a full cap and with locked releases |
| 10 | Locking a release is refused when the app already holds `max_locked_releases` locks; the count and the lock share one transaction that locks the `app_config` row; the settings reject `max_locked_releases` above `max_releases_per_app - 4` | two concurrent lock calls at the cap; settings tests |
| 11 | A content-store file is removed only when no live release references it and it is older than a day | clean-up job test |
| 12 | `detected.env` entries have exactly one of `value` and `value_from`; `app.yaml` plus `app.yml` is refused; `databricks.yml` only feeds `detected.bundle_resources` and its `config`, `permissions`, `user_api_scopes`, targets and variables are never applied | `app.yaml` and `databricks.yml` parser tests |
| 13 | `slug` changes only by an approved `settings` request or a platform admin's edit, never to a taken slug, and writes `app.rename`; an app is deleted only when stopped; `release_no` is never reused | register, rename, delete and concurrent-Update tests |
| 14 | A setting key not in the code's list is rejected | settings test |
| 15 | A secret value never appears in `audit_log.detail`, `releases.config`, `releases.detected`, `build_log_tail` or logs; a `secret` resource's `target` is `scope/key` | secret tests, including a build log that echoes a value |
| 16 | **Request kinds:** `kind` must be in the code's catalogue and `payload` must pass that kind's schema, checked by the same code as a direct edit of each field; a request is refused when every field equals the app's current value, for `shared_sp` while the master switch is off, for a `slug` an app already has, for a `settings` payload with no field, and for `register` when an app or a pending request already has the slug or the asker already has 3 pending `register` requests; an unknown kind in a row cannot be approved; approving a request that holds a gate field (every `register`, `source_path`, `obo`, `shared_sp`) needs a reason; at approval a taken slug closes the request as `denied` automatically, and no other failed check writes anything | one test per catalogue entry (a valid and an invalid payload, the asker rule, the effect on `apps`), a rename, a taken slug at approval, two requests for the same free slug, and a row of an unknown kind that must stay unapproved |
| 17 | **Implicit platform-admin access:** a platform admin has `CAN_MANAGE` on every app with no row in `app_grants`; the edge strips `X-Forwarded-Access-Token` for one who is neither the owner nor named by a grant, unless they chose **Open as a user** (audited as `app.view_as_user`); every view of a release's source writes `release.view_source` | table-driven `authorize` rows, and an OBO fixture app opened by a platform admin that must receive no token by default and the token after the switch, with its audit row |
| 18 | **Egress rules:** a per-app list is edited only by a platform admin, and the editor refuses an `allow` that overlaps `@container_ip`, `@appmux_port` or `169.254.0.0/16` | editor refusal rows; an app admin's attempt to edit |

## 8. Retention and volume

| Data | Kept | Clean-up |
|---|---|---|
| Rows of an app | while the app exists | cascades on app delete |
| Live releases per app | up to the cap (default 10; for example 20); at most 3 are locked by hand (`max_locked_releases`) | [4.6](#46-releases) |
| Deleted release rows; requests to register an app that were denied or withdrawn (no app row) | forever (tiny) | none |
| Content-store files (snapshots, wheels) | while a live release references them | clean-up job, daily |
| `metrics` | 7 days: 10,080 rows per app, about 1.0 M rows for 100 apps, about 250 MB at an estimated 250 B per row | hourly delete by `minute` |
| `audit_log` | forever | none |

Rough volume for 100 apps at a cap of 20: about 2,000 release rows, 1,000 resource rows and a few hundred grants — trivial for Lakebase. **Storage is the cost to watch**, and it sits in the content store, not in the database: 100 apps × 20 releases × 5 MB (an assumed average source size) is about 10 GB before the sharing of identical snapshots and wheels. The cap is therefore a platform admin's cost control as well as a convenience.

## 9. Open points

Each was an unverified assumption the data model depends on; all were checked in the spikes of 2026-10-30 on Free Edition, and each entry says what was found.

1. **OBO to Lakebase login** (spike 1, done 2026-10-30, [log](../../specs/001-appmux-v1/spikes/README.md#spike-1--obo-login-to-lakebase-2026-10-30)). Verified on Free Edition: a platform admin's OBO token logs in as their own role and writes only through `appmux_platform_writers`; no OAuth scope is needed, and a connection survives the token's expiry. Lakebase ignores a token's scopes, so an OBO child holding an admin's token can write platform data ([design 4.2](v1-design.md#42-who-writes-what)); the owner accepted this on 2026-10-30 ([spec question 5](../../specs/001-appmux-v1/spec.md#questions-for-the-owner), option a; [ADR 0004](../decisions/0004-keep-decisions-in-a-nine-table-lakebase-registry-with-split-writers.md)).
2. **Groups through nesting** (spike 12, done 2026-10-30, [log](../../specs/001-appmux-v1/spikes/README.md#spike-12--nested-groups-in-the-scim-me-response-2026-10-30)). Verified: `Me` returns groups reached through nesting, typed `indirect`, so grants match direct and indirect memberships ([design 9.1](v1-design.md#91-the-model)). The answers lag a membership change by up to about 75 s.
3. **Reading the workspace with the visitor's token** (spike 15, done 2026-10-30, [log](../../specs/001-appmux-v1/spikes/README.md#spike-15--workspace-file-read-with-the-pressers-obo-token-2026-10-30)). Verified on Free Edition: an OBO token with the app scope `workspace.workspace:read` lists, gets the status of and exports files in the presser's folder, and cannot write with it; without the scope the call is refused. A principal with no access gets an **empty listing or a 404, not an access error**, so Update reports "empty, or you cannot read it" and refuses a snapshot of zero files ([design 13.1](v1-design.md#131-what-the-operator-sets-up)). A folder the presser cannot read was not tested (the owner is a workspace admin).
4. **Per-type attach checks** (spike 16, done 2026-10-30, [log](../../specs/001-appmux-v1/spikes/README.md#spike-16--per-type-attach-checks-2026-10-30)). Verified on Free Edition: eight of the fourteen types have a check with an observed refusal, the other six are `unverified` ([4.7](#47-app_resources)). A read proves access, and the level only for a serving endpoint.
5. **Cascade privileges** (spike 9, done 2026-10-30, [log](../../specs/001-appmux-v1/spikes/README.md#spike-9--cascade-privileges-on-lakebase-and-the-slirp4netns-licence-2026-10-30)). Verified: a `DELETE` on `apps` and an `UPDATE` of `apps.slug` by a member of `appmux_platform_writers` cascade into the five child tables in one transaction, and the deferred composite keys of `app_config` let the rename through. The DDL needed one correction ([4.4](#44-app_config), [4.7](#47-app_resources)).
6. **Content store location** (spike 4, done 2026-10-30, [log](../../specs/001-appmux-v1/spikes/README.md#spike-4--content-store-location-2026-10-30)). Verified on Free Edition: the appmux SP writes, reads and deletes files in its own workspace folder up to 500 MB each, and in a Unity Catalog volume up to at least 1000 MB ([design 3.3](v1-design.md#33-where-state-lives)). A snapshot of many small files costs one call per file; the plan chooses the layout.
7. **`app.yaml` details** (spike 17, done 2026-10-30, [log](../../specs/001-appmux-v1/spikes/README.md#spike-17--appyaml-on-the-real-platform-2026-10-30)). Verified on Free Edition: Databricks substitutes `$DATABRICKS_APP_PORT` in a `command` argument and not `${DATABRICKS_APP_PORT}`, never substitutes inside `env` values, and a `valueFrom` naming no resource logs an error but still starts the deployment ([design 5.4](v1-design.md#54-appyaml-requirements-and-start)).
8. **Reading a secret value from an App** (spike 11, done 2026-10-30, [log](../../specs/001-appmux-v1/spikes/README.md#spike-11--secrets-api-from-an-app-2026-10-30)). Verified on Free Edition: the appmux SP reads a value with `READ` on the scope, so secret references ship ([4.7](#47-app_resources)) and the fallback store is not planned. The attacher's OBO token cannot list a scope's keys (no accepted app scope covers the Secrets API); the attach check is the SQL `secret()` call instead. A revoked `READ` keeps working for minutes. Whether a human who is not an admin can create a scope was not tested.
9. **Package overlay on the real container** (spike 18, done 2026-10-30, [log](../../specs/001-appmux-v1/spikes/README.md#spike-18--package-overlay-on-the-real-container-2026-10-30)). Verified on Free Edition: installing the differing wheels with `--no-index --no-deps --require-hashes` into a per-release folder and putting it first on `PYTHONPATH` makes the overlay's versions win, and the read-only remount holds; the lock must cover the whole closure, because overriding an environment package can break other environment packages that pin it ([design 5.3](v1-design.md#53-build-and-package-sources)).
10. **Admin UI build** (spike 19). Verified (spike 19, Free Edition): a Next.js 16.4.3 static export works behind a base path and the proxy, but only with per-page SHA-256 hashes of its inline scripts in `script-src`; `script-src 'self'` alone leaves the page stuck. The platform builds a `package.json` in a Python App, yet the export is still built before deploy and ships with the source ([design 10.1](v1-design.md#101-screens)).
