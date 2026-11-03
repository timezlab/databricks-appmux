# Contract: admin JSON API

The API the exported admin UI polls and writes through, under `/_appmux/api/v1/`. Every route calls `authorize(actor, action, app)` with the action named below ([design 9.2](../../../docs/product/v1-design.md#92-who-may-do-what)) and no business rule lives in a handler ([design 3.4](../../../docs/product/v1-design.md#34-module-boundaries)). The route names are this plan's; the actions, rules and messages are the design's. FR-053: every step of the SC-004 walkthrough has a route here.

The request and response bodies are in [Schemas](#schemas), with one valid example per schema in [examples/](examples/manifest.json). Each example is validated against the backend's Pydantic models and the UI client's Zod schemas by tests (T186 backend, T171 UI), so that the tables, the examples and the code stay one contract.

## Conventions

- JSON in and out (`application/json`); times are ISO 8601 UTC. The actor is the forwarded identity of [edge-http.md](edge-http.md); there is no separate session or token.
- A row that can be edited concurrently carries `version`, and only the data model's two version columns exist: `apps` and `app_config`. A write that changes one of those rows sends the `version` it read (`PATCH /apps/{app}`, `PUT /apps/{app}/owner`, `PUT /apps/{app}/egress` and `PUT` and `DELETE /apps/{app}/delegation/{kind}` send the `apps` one; `PUT /apps/{app}/config` sends the `app_config` one); a stale one answers `409` with `changed by someone else, reload and retry`. Every write that changes an `apps` row returns the new `apps` version (`AppDetail.version`, or `RequestRow.app_version` for an approval), and a write to `app_config` returns the new `config_version`, so a client never keeps a stale one.
- `PUT` replaces the whole value it names (a release's name and tags, the whole editable config, a whole egress or admin list, one resource, one grant, one setting); `PATCH` changes only the fields present in the body (`PATCH /apps/{app}` is the only one).
- Errors share one shape and never carry a stack trace, an SQL message or an internal path:

  ```json
  { "error": { "code": "slug_taken", "message": "slug 'reports' is already taken" } }
  ```

  | Status | Meaning |
  |---|---|
  | `400` | the body fails its schema, or a rule of the design refuses it (the message names the field or rule) |
  | `401` | no forwarded identity |
  | `403` | `authorize()` denied; the caller already sees the screen, so the denial is stated |
  | `404` | no such app, release or request; an app the caller cannot open gets the same answer as a missing one. Also no such route: a path with the other trailing slash is a `404`, never a redirect |
  | `405` | the path exists, the method does not (code `method_not_allowed`) |
  | `409` | stale `version`, or a state that forbids the action (for example deleting the active release) |
  | `500` | a defect of the server (code `internal_error`, a fixed sentence); the detail goes to the server log only |
  | `503` | the registry is down and the route needs it; reads of the in-memory route table still answer |

- A route that reads a body wants `Content-Type: application/json` and at most 1 MiB; anything else is `400`. Every response, errors included, carries `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.
- No string in a request body may contain NUL, at any depth or in an object key: `400` (Postgres text and `jsonb` cannot store one).
- Mutating routes require `Sec-Fetch-Site: same-origin` (anything else is `403` `cross_origin`) and write an `audit_log` row with the actor ([data model 9](../../../docs/product/v1-data-model.md)); a call that changes nothing, where its route says so, records no row.
- A route that needs a part of the running service is mounted only when the service has that part: the Update flow (`POST /apps/{app}/releases`, the release page, name and tags, lock, unlock and delete), the app control (activate, `PUT /apps/{app}/config`, start, stop and restart), the attach check (requirements, attach and detach) and the content store (the source viewer). The running service has them all; code that builds the edge alone may not, and those paths then answer `404` `not_found`.
- Lists are not paginated except `audit` (`from`, `to`, `limit`); `metrics` takes a window (`from`, `to`) and is capped in rows (see `MetricsResponse`). An app list is bounded by about 100 apps; `GET /requests` has no filter and no paging in v1.

## Routes

`app` is an app slug. "Needs" is the `authorize()` action, with the permission the design gives it. "Request" and "Response" name the body schemas of [Schemas](#schemas): `—` is no body, `204` is an empty success. A path `{id}` is a release number (integer) under `/releases` and a request id (integer) under `/requests`.

### Identity and launcher

| Route | Needs | Request | Response | Returns / does |
|---|---|---|---|---|
| `GET /me` | any visitor | — | `MeResponse` | the actor: email, groups, whether a platform admin |
| `GET /apps` | any visitor | — | `AppList` | the apps the actor can open (all for a platform admin), each with state and, for an OBO app with no grant on a platform admin, the two open modes |
| `POST /apps/{app}/open-mode` | `app.view_as_user` | `OpenModeRequest` | `204` | body `{"mode": "as_user" \| "without_token"}`; sets the edge cookie (the `204` carries it in `Set-Cookie`); `as_user` writes an audit row, `without_token` reduces access, changes no row and records none; `503` `open_mode_unavailable` when the process has no edge to set the cookie |

### Apps

| Route | Needs | Request | Response | Returns / does |
|---|---|---|---|---|
| `POST /apps` | `app.register` | `RegisterAppRequest` | `AppDetail` | body: `slug`, `display_name`, `source_path`, `memory_budget_mb`, `owner`; written under the admin's own token (`403` `user_token_missing` without it); a taken slug is `409` `slug_taken` and nothing is written; the app starts stopped with no release |
| `GET /apps/{app}` | `CAN_MANAGE` on the app | — | `AppDetail` | status, detected runtime, releases, requirements, resources (secrets as references), config, delegation status, egress (read-only), owner |
| `PATCH /apps/{app}` | `app.edit_platform_fields` | `UpdateAppRequest` | `AppDetail` | any of `slug` (also writes `app.rename`), `display_name`, `source_path`, `memory_budget_mb`; needs `version`; written under the admin's own token (`403` `user_token_missing` without it); a taken `slug` is `409` `slug_taken`; a field that already holds the value sent is not a change, and when none differs the answer is the current `AppDetail` with nothing written or recorded |
| `PUT /apps/{app}/owner` | `app.set_owner` | `SetOwnerRequest` | `AppDetail` | body: `owner`; needs `version`; written under the admin's own token (`403` `user_token_missing` without it); naming the current owner changes and records nothing |
| `DELETE /apps/{app}` | `app.delete` | — | `204` | stops and unregisters; configuration, releases, resources, secrets and requests go with it |
| `POST /apps/{app}/start` | `app.start` | — | `AppStateResponse` | returns the new state; `409` `no_release` when the app has no active release and no target waiting to be built; starting a crashed app restarts it; the audit row is written even when the app was running already |
| `POST /apps/{app}/stop` | `app.stop` | — | `AppStateResponse` | returns the new state; the audit row is written even when the app was stopped already |
| `POST /apps/{app}/restart` | `app.restart` | — | `AppStateResponse` | returns the new state; `409` `no_release` as for start; a running app is restarted beside the running copy |

### Releases

| Route | Needs | Request | Response | Returns / does |
|---|---|---|---|---|
| `POST /apps/{app}/releases` | `app.update` | — | `ReleaseCreated` (`202`) or `ReleaseUnchanged` (`200`) | starts an Update with the presser's token; `202` returns the new release id and progress is read from `GET …/releases/{id}`; `200` means the folder's digest equals a live release's, so it is "already up to date" and no release is created and no audit row is written; without the presser's token (`403` `user_token_missing`) the folder cannot be read and the flow does not run; the flow's own refusals keep its message and answer `404`, `400`, `409` (code `conflict`) or `503`; a created release writes `release.create` with the release number and the snapshot digest |
| `GET /apps/{app}/releases/{id}` | `CAN_MANAGE` | — | `ReleaseDetail` | status, name, tags, lock, reason and build-log tail, detected manifest, wheel list, warnings; a deleted or unknown release is `404` |
| `POST /apps/{app}/releases/{id}/activate` | `release.activate` | — | `ReleaseDetail` | any live release; goes through the health-gated swap; the answer is `pending` until the reconciler has swapped to it; `409` `requirements_missing` (checked before anything is written, every missing key named in the message; not asked of the active release), `409` `release_in_progress` while the release is `building`, `starting` or `healthy`; a deleted or unknown release is `404` |
| `PUT /apps/{app}/releases/{id}` | `release.label`, `release.tag`, `release.untag` | `UpdateReleaseRequest` | `ReleaseDetail` | body: `name`, `tags`; a blank name or tag is `400`; a repeated tag counts once; one audit row per kind of change (`release.label`, `release.tag`, `release.untag`), and a request that changes nothing answers the current release and records nothing |
| `PUT /apps/{app}/releases/{id}/lock` | `release.lock` | — | `ReleaseDetail` | refused `409` `lock_limit_reached` at `max_locked_releases` (also when it is `0`); locking a locked release is `200` and records nothing |
| `DELETE /apps/{app}/releases/{id}/lock` | `release.unlock` | — | `ReleaseDetail` | unlocking an unlocked release is `200` and records nothing |
| `DELETE /apps/{app}/releases/{id}` | `release.delete` | — | `204` | `409` `release_protected` for the active, target, in-progress and locked releases, with the reason in the message; a deleted or unknown release is `404`. A lock and a delete (and the cap's own clean-up) write the app's settings row at the version they read, so when another of them wrote first the answer is `409` `changed_by_others`. `PUT` of the name and tags, lock, unlock and delete answer `503` while the registry is down, even where they would change nothing |
| `GET /apps/{app}/releases/{id}/source` | `release.view_source` | — | `ReleaseSourceListing` | file list; binary files are listed, not returned (a file is `text` when it is at most 1 MiB, holds no NUL and is valid UTF-8, otherwise `binary`); each call writes an audit row, and a registry that cannot record the look answers `503` with no content |
| `GET /apps/{app}/releases/{id}/source/{path}` | `release.view_source` | — | `ReleaseSourceFile` | one text file; binary files are listed, not returned; each call writes an audit row. `400` for a `path` that is not relative inside the snapshot (an empty or `.` or `..` part, a control character, more than 512 characters), `400` `not_text` for a binary file or one over 1 MiB, `404` for a file the snapshot does not hold. `503` `snapshot_missing` or `content_store_unavailable` when the snapshot cannot be fetched, `500` `snapshot_unreadable` when it is not a readable archive |

### Config, resources and grants

| Route | Needs | Request | Response | Returns / does |
|---|---|---|---|---|
| `PUT /apps/{app}/config` | `config.edit` | `UpdateConfigRequest` | `ConfigChangeResult` | env entries, wheel sources, `health_path`, `health_deadline_s`, own-SP keys; creates a new release with the same snapshot; needs `version`; `400` when an own-SP key does not name an attached `secret` resource, `409` `no_release` when env entries or wheel sources are set and the app has no release yet |
| `GET /apps/{app}/requirements` | `CAN_MANAGE` | — | `RequirementsResponse` | each required key, attached or missing, with the bundle offer |
| `PUT /apps/{app}/resources/{key}` | `resource.attach` | `AttachResourceRequest` | `ResourceView` | runs the attach check with the person's token; a `secret` is a `scope/key` reference. A key that is not of the form of `AttachResourceRequest` is `400`. A refusal of the attach rules is `400` with one of these codes and nothing is written: `unknown_type`, `bad_permission`, `bad_target`, `access_as_invalid`, `delegation_missing`, `own_sp_keys_missing`, `own_sp_keys_invalid`, `unverified_shared_sp`, `check_warehouse_unset`, `check_warehouse_denied`, `token_missing` (no token to check with), `no_access` (the person's token cannot reach the object). A check that could not run is `503` `check_unavailable` and is never taken as a pass. Re-attaching the resource that holds the own service principal's keys as anything but a `secret` is `409` `in_use` |
| `DELETE /apps/{app}/resources/{key}` | `resource.detach` | — | `204` | `404` `no_such_resource` when the app has no resource under that key; `409` `in_use` for a resource that `own_sp_client_id_key` or `own_sp_client_secret_key` names |
| `PUT /apps/{app}/grants` | `grant.app` | `GrantRequest` | `AppGrant` | `type` is `user`, `group` or `everyone`; `everyone` is `CAN_USE` only and the owner cannot be named with a lower level (both `400`, see `GrantRequest`); a principal that already holds that level gets the stored grant with `200` and nothing is recorded |
| `DELETE /apps/{app}/grants/{type}/{name}` | `grant.app` | — | `204` | `type` is `user`, `group` or `everyone`; `name` is the rest of the path, so a group name may hold `/`; naming the owner is `400` (the owner is not a grant row and cannot be removed); a principal with no grant is `404` |

### Requests

| Route | Needs | Request | Response | Returns / does |
|---|---|---|---|---|
| `GET /requests` | any visitor sees their own; `CAN_MANAGE` on the app sees its app's; platform admins see the inbox | — | `RequestList` | every request the actor may see, oldest first; no filter and no paging in v1, the client builds the three views from it (see `RequestList`) |
| `POST /requests` | `request.create` | `CreatePermissionRequest` | `RequestRow` | body: `kind` (`register`, `delegation`, `settings`), `app` (or the proposed slug), `payload`, `reason` (required, up to 500 characters). Refusals carry a `code`: `400` `unknown_kind` (a kind outside the three), `400` `no_change` (a field already holds the value asked for, or a delegation is already held), `409` `slug_taken`, `409` `request_pending` (one pending request per app and kind, or per proposed slug), `409` `too_many_pending` (a person may have 3 pending `register` requests), `409` `shared_sp_not_allowed` (the master switch is off); an app that does not exist is `404` |
| `POST /requests/{id}/withdraw` | `request.withdraw` | — | `RequestRow` | only the asker, only while pending: someone else is `403` `not_the_asker`, a closed request is `409` `not_pending`; a request the caller may not see is `404` |
| `POST /requests/{id}/approve` | `request.decide` | `DecidePermissionRequest` | `RequestRow` | body: `reason` (required when the request holds a gate field, else `400` `reason_required`), `note`; approval is one transaction under the admin's own role (`403` `user_token_missing` without the admin's token) and answers the new `apps` version as `app_version`; a taken slug closes the request as `denied`. The request is checked again as it stands: `409` `not_pending`, `409` `app_missing` (the app is gone), `409` `requester_not_admin` (the asker no longer manages the app), `409` `shared_sp_not_allowed`, `400` `request_invalid` (the stored payload no longer passes the checks of a new request). An approved rename is followed by moving the running app: `503` `rename_not_applied` means the new slug is saved and Restart on the app retries the move, `503` `rename_unavailable` means the process cannot move apps and nothing was saved |
| `POST /requests/{id}/deny` | `request.decide` | `DecidePermissionRequest` | `RequestRow` | body: `reason`, `note`; closes the request; `403` `user_token_missing` without the admin's token, `409` `not_pending` for a closed request |
| `PUT /apps/{app}/delegation/{kind}` | `delegation.grant` | `DelegationReasonRequest` (body) | `AppDetail` | `kind` is `obo` or `shared_sp` (any other word is `404`); body: `reason` (required), `version`; written under the admin's own token (`403` `user_token_missing` without it); `409` `already_delegated` when the app holds it, `409` `shared_sp_not_allowed` for `shared_sp` while the master switch is off; takes effect at the app's next start |
| `DELETE /apps/{app}/delegation/{kind}?reason=&version=` | `delegation.revoke` | `DelegationReasonRequest` (query) | `AppDetail` | `kind` is `obo` or `shared_sp`; no body: the fields of `DelegationReasonRequest` are query parameters, percent-encoded (`reason` required, not blank, at most 500 characters; `version` required; a missing or unlisted parameter, or a `version` that is not an integer, is `400`); revoke records the `reason`; written under the admin's own token (`403` `user_token_missing` without it); `409` `not_delegated` when the app does not hold it; a revoke is never refused because the `shared_sp` master switch is off |

### Platform

| Route | Needs | Request | Response | Returns / does |
|---|---|---|---|---|
| `GET /platform` | `CAN_MANAGE` on the platform | — | `PlatformView` | settings, admins, global egress, `self_check` result, memory against the slot limit, and every app's platform fields, delegations and egress (`apps`), so the page needs no call per app; nothing is invented for a part that is unknown, so it is `503` `self_check_pending` before the first self-check has finished, `503` `memory_unknown` while the slot's memory is not known, `503` `self_check_unavailable` when the process has no self-check, and `503` `settings_invalid` (naming the keys) when a stored setting no longer fits its key |
| `PUT /platform/settings/{key}` | `settings.edit` | `PutSettingRequest` | `PlatformSetting` | `key` is one of `memory_reserve_mb`, `package_mode`, `default_wheel_sources`, `shared_sp_delegation_allowed`, `max_releases_per_app`, `max_locked_releases`, `request_timeout_s`, `max_request_body_mb`, `max_concurrent_requests`, `build_timeout_s`, `max_processes_per_child`, `child_cpu_pin`, `attach_check_warehouse_id` (the keys of [data model 4.1](../../../docs/product/v1-data-model.md#41-platform_settings) except `platform_admins` and `egress_global`); those two (`400` `setting_has_own_route`), an unknown key (`400` `unknown_setting`), a value of the wrong type or outside the key's range, `null` (no setting is stored as `null`) and a rule between keys broken are `400`; the message names the key and never repeats the value |
| `PUT /platform/admins` | `platform_admin.add`, `platform_admin.remove` | `PutAdminsRequest` | `AdminList` | refused `400` when no `user` entry would remain or an entry is listed twice; a list that adds and removes nothing answers `200` with the list in force and records nothing |
| `PUT /platform/egress` | `egress.edit` | `PutEgressRequest` | `EgressRules` | an ordered rule list; the editor refuses an `allow` that overlaps `@container_ip`, `@appmux_port` or `169.254.0.0/16` (`400` `egress_rules_refused`, nothing written); `503` `egress_targets_unknown` while the container's address is not known, and `503` `egress_not_applied` when the list is saved but the running apps were not told: saving it again retries |
| `PUT /apps/{app}/egress` | `egress.edit` | `PutAppEgressRequest` | `AppDetail` | an ordered rule list; the editor refuses an `allow` that overlaps `@container_ip`, `@appmux_port` or `169.254.0.0/16`; needs `version`; the refusals and the `503` codes are those of `PUT /platform/egress` |
| `POST /platform/self-check` | `CAN_MANAGE` on the platform | — | `SelfCheckResult` | re-runs `self_check()`; returns the per-check result; a run that fails is `200` with `passed: false`, not an error; it records no audit row; `503` `self_check_unavailable` when the process has no self-check |
| `GET /audit?app=&from=&to=&limit=` | `CAN_MANAGE` on the platform | — | `AuditList` | audit rows; there is no audit screen in v1, so this is for operators; a parameter sent twice, a time without a zone, `from` not earlier than `to`, an `app` that is not a slug or a `limit` outside 1 to 1000 is `400` (the message names the parameter, not the value) |

### Metrics

| Route | Needs | Request | Response | Returns / does |
|---|---|---|---|---|
| `GET /metrics?app=&from=&to=` | `CAN_MANAGE` on the app, or on the platform for all apps and the platform row | — | `MetricsResponse` | one-minute rows with the columns of [design 10.2](../../../docs/product/v1-design.md#102-metrics); histograms are returned as counts and the UI derives percentiles; at most 10000 rows, oldest minutes dropped (`truncated`); `400` for a parameter sent twice, a time without a zone, `from` not earlier than `to`, an `app` that is neither a slug nor `_platform`, or a window longer than 1440 minutes |

## Schemas

Each body of the Routes tables has a subsection below. The choices here are the plan's; the fields come from the columns of the [data model](../../../docs/product/v1-data-model.md) and the screens of [design 10.1](../../../docs/product/v1-design.md#101-screens), and no schema adds behaviour the design does not decide.

### Reading the schemas

- **Names.** Schemas are PascalCase; fields are `snake_case`. Request schemas end in `Request`.
- **Types.** `string`, `integer`, `boolean`, `object`, `array<T>`, and `time`: a string in ISO 8601 UTC with whole seconds and a trailing `Z`, for example `2026-10-30T09:14:00Z`. `X or null` is a nullable field. A value from a closed list says `One of …` in the meaning. `any` is a JSON value whose type depends on another field.
- **Presence.** In a response every listed field is `always` present; a field with no value is `null`, never absent. In a request a field is `required`, or `optional` (omit it or send `null`). A request with a field the schema does not list is `400` naming the field (no mass assignment). A body whose JSON does not match its schema is `400`.
- **Nested fields.** A dotted name (`config.health_path`) is a field of an object inside the schema; `releases[]` means every element of the array.
- **Lists.** A route that returns a list returns an object whose `items` array holds the elements, never a bare array, so a list can later grow metadata (`AppList`, `RequestList`, `AdminList`, `EgressRules`, `AuditList`, `MetricsResponse`, `ReleaseSourceListing`). A collection inside a larger object (`AppDetail.releases`) is a plain array named by its plural noun. Each list states its order.
- **Identifiers.** An app is its `slug`; a release is its `release_no` (integer, per app, never reused); a request is its `request_id` (integer). `version` is an integer counter of the row the schema names; a write sends the value it read. Only `apps` (`AppDetail.version`, `PlatformApp.version`) and `app_config` (`AppDetail.config.version`) carry one; `RequestRow.app_version` and `ConfigChangeResult.config_version` return the value after a write.
- **Secrets.** A secret appears only as a `scope/key` reference (`target` of a `secret` resource, or the key of a resource named in `own_sp_*_key`). No schema carries a secret value, a token or a client secret. A plain env `value` is stored and shown, so it is never a secret.
- **Success.** `200` with the response schema, or `204` with no body; `POST /apps/{app}/releases` answers `202` when it starts an Update. A schema returned by a write holds the state after the write.
- **Examples.** [examples/](examples/) holds `<SchemaName>.json` for every schema in the request and response sections, and [examples/manifest.json](examples/manifest.json) maps each schema name to `{"file", "direction", "routes"}`. `direction` is `request` or `response`. `routes` lists the routes of the Routes tables (`METHOD path`, no query string) whose request or response is the schema or contains it. A request example holds every required field and some optional ones; a response example holds every field. The three shared types below are checked through the examples that contain them.

### Shared types

Three objects appear in both directions (a request sends them, a response returns them), so they have no manifest entry of their own.

#### Principal

Who a list of platform admins names. An app grant carries the same two fields flat (`AppGrant`).

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `type` | string | required | One of `user`, `group`, `everyone`. Platform admins are `user` or `group` only |
| `name` | string | required | A lower-case email for `user`, a Databricks group name for `group`, `*` for `everyone` |

#### EgressRule

One rule of an ordered egress list; the first match wins and no match allows ([design 6.3](../../../docs/product/v1-design.md#63-egress-rules)).

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `action` | string | required | One of `allow`, `deny` |
| `cidr` | string | required | A CIDR such as `10.20.0.0/16`, or one that starts with the placeholder `@container_ip` (`@container_ip/32`), resolved at start |
| `port` | string | required | `any`, a single port such as `443`, or the placeholder `@appmux_port` |
| `comment` | string | required | Free text for the next reader; may be empty |

#### EnvEntry

One environment entry of a release: `app.yaml` names it, the release config may add or override it ([design 8.2](../../../docs/product/v1-design.md#82-resources-and-env-entries)). Exactly one of `value` and `value_from` is not null.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `name` | string | required | The variable name |
| `value` | string or null | optional | A plain value, stored and shown; never a secret |
| `value_from` | string or null | optional | The key of one of the app's resources (`valueFrom` in `app.yaml`). In a request it follows the resource-key rule of `AttachResourceRequest`; in a response it can be any non-empty string of at most 255 characters, because the registry already accepted it |

### Request bodies

#### OpenModeRequest

Used by `POST /apps/{app}/open-mode`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `mode` | string | required | One of `as_user`, `without_token`: how this platform admin opens the app from now on |

#### RegisterAppRequest

Used by `POST /apps`. The values pass the same checks as a `register` request.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `slug` | string | required | Lower-case letters, digits and `-`, starting with a letter or digit, at most 31 characters; not taken |
| `display_name` | string | required | Shown in the launcher; not blank |
| `source_path` | string | required | A workspace folder, starting with `/Workspace/` |
| `memory_budget_mb` | integer | required | 64 to 16384 |
| `owner` | string | required | Email of the owner, lower case |

#### UpdateAppRequest

Used by `PATCH /apps/{app}`. At least one field besides `version` must be present.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `version` | integer | required | `AppDetail.version` as read |
| `slug` | string | optional | The new slug; same form as at registration, and free |
| `display_name` | string | optional | The new name |
| `source_path` | string | optional | The new folder, starting with `/Workspace/` |
| `memory_budget_mb` | integer | optional | 64 to 16384 |

#### SetOwnerRequest

Used by `PUT /apps/{app}/owner`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `version` | integer | required | `AppDetail.version` as read |
| `owner` | string | required | Email of the new owner, lower case |

#### UpdateReleaseRequest

Used by `PUT /apps/{app}/releases/{id}`. It replaces the name and the tags together; the actions asked of `authorize()` follow what changes (`release.label`, `release.tag`, `release.untag`).

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `name` | string or null | required | The release's name (`label` in the data model), 1 to 80 characters; `null` clears it |
| `tags` | array<string> | required | Free labels, lower case, up to 32 characters each, up to 10; they protect nothing |

#### UpdateConfigRequest

Used by `PUT /apps/{app}/config`. It replaces the editable configuration as a whole, so the UI sends back what it read with its changes.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `version` | integer | required | `AppDetail.config.version` as read |
| `env` | array<EnvEntry> | required | The release's own env entries |
| `wheel_sources` | array<string> | required | Workspace folders the next Update also reads wheels from |
| `health_path` | string | required | Starts with `/`; any answer below 500 passes |
| `health_deadline_s` | integer | required | 5 to 600 |
| `own_sp_client_id_key` | string or null | required | Key of the `secret` resource holding the own SP's client id; set together with the next field or both `null` |
| `own_sp_client_secret_key` | string or null | required | Key of the `secret` resource holding its client secret |

#### AttachResourceRequest

Used by `PUT /apps/{app}/resources/{key}`; the key is the path parameter (1 to 63 characters, lower-case letters, digits, `.`, `_` and `-`, starting with a letter or a digit).

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `type` | string | required | A type of the resource catalogue ([data model 4.7](../../../docs/product/v1-data-model.md#47-app_resources)), for example `sql_warehouse` or `secret` |
| `target` | string | required | The object, or `scope/key` for a `secret`; never a secret value |
| `permission` | string | required | A level of the type; `READ` for a secret |
| `access_as` | string or null | optional | One of `obo`, `own_sp`, `shared_sp`. Required for every type but `secret`; `null` or omitted for a `secret` |
| `from_bundle` | boolean | optional | `true` when the person accepted an offer of the release's `databricks.yml` (recorded in the audit row); default `false` |

#### GrantRequest

Used by `PUT /apps/{app}/grants`. It adds the grant or changes its level.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `type` | string | required | One of `user`, `group`, `everyone` |
| `name` | string | required | A lower-case email, a Databricks group name, or `*` for `everyone` |
| `permission` | string | required | One of `CAN_USE`, `CAN_MANAGE`; `everyone` takes `CAN_USE` only ([FR-022](../spec.md)) |

Each of these is refused with status `400` (the first row of the status table: a rule of the design; the message names the field and the rule), and nothing is written:

| Body | Why |
|---|---|
| `{"type": "everyone", "name": "*", "permission": "CAN_MANAGE"}` | `everyone` is `CAN_USE` only |
| `{"type": "everyone", "name": "sales-team", "permission": "CAN_USE"}` | `everyone` is named `*` |
| `{"type": "user", "name": "alice@example.com", "permission": "CAN_USE"}`, when Alice is the app's owner | the owner is always `CAN_MANAGE` and cannot be named with a lower level ([data model rule 1](../../../docs/product/v1-data-model.md#7-rules-enforced-in-code)) |
| `{"type": "user", "name": "Carol", "permission": "CAN_USE"}` | a `user` is a lower-case email |
| `{"type": "team", "name": "x", "permission": "CAN_USE"}` | `type` is not one of the three |

`DELETE /apps/{app}/grants/{type}/{name}` naming the owner is `400` for the same reason.

#### CreatePermissionRequest

Used by `POST /requests`. The `payload` holds the keys of the kind ([data model 4.3](../../../docs/product/v1-data-model.md#43-permission_requests), [design 9.4](../../../docs/product/v1-design.md#94-requests-to-a-platform-admin)):

| `kind` | `app` | `payload` |
|---|---|---|
| `register` | the proposed slug, equal to `payload.slug` | `slug`, `display_name`, `source_path`, `memory_budget_mb`, all four |
| `delegation` | the app's slug | `{"obo": true}`, `{"shared_sp": true}` or both; only `true` |
| `settings` | the app's slug | any non-empty subset of `slug`, `display_name`, `source_path`, `memory_budget_mb` |

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `kind` | string | required | One of `register`, `delegation`, `settings` |
| `app` | string | required | See the table above |
| `payload` | object | required | What is asked, by kind |
| `reason` | string | required | What the platform admin reads; not blank, up to 500 characters |

#### DecidePermissionRequest

Used by `POST /requests/{id}/approve` and `POST /requests/{id}/deny`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `reason` | string or null | optional | Required to approve a request with `requires_reason` true; stored with the audit row |
| `note` | string or null | optional | Shown to the asker with the answer (`closed_note`); when it is missing or blank, the trimmed `reason` is stored as `closed_note` instead, and a withdrawal stores none |

#### DelegationReasonRequest

Used by `PUT` and `DELETE /apps/{app}/delegation/{kind}`. The `PUT` sends these fields as its JSON body. The `DELETE` has no body, because nothing shows that a `DELETE` body survives the Databricks proxy: it sends the same two fields as query parameters, `?reason=<percent-encoded text>&version=<integer>`, with the same rules.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `version` | integer | required | `AppDetail.version` as read |
| `reason` | string | required | Why the platform admin grants or revokes; not blank, at most 500 characters; stored in the audit row |

#### PutSettingRequest

Used by `PUT /platform/settings/{key}`. The keys `platform_admins` and `egress_global` are changed through `PUT /platform/admins` and `PUT /platform/egress`; sent here they are `400`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `value` | any | required | The new value; its JSON type is the key's ([data model 4.1](../../../docs/product/v1-data-model.md#41-platform_settings)) |

#### PutAdminsRequest

Used by `PUT /platform/admins`. It replaces the list; the entries added and removed are audited one by one.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `admins` | array<Principal> | required | The whole new list; `user` and `group` entries only, and at least one `user` |
| `reason` | string | required | Why the list changes; not blank |

#### PutEgressRequest

Used by `PUT /platform/egress`. The global rules are the `egress_global` setting, which has no version column.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `rules` | array<EgressRule> | required | The whole new ordered list, possibly empty |

#### PutAppEgressRequest

Used by `PUT /apps/{app}/egress`. The app's rules are a column of `apps`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `version` | integer | required | `AppDetail.version` as read |
| `rules` | array<EgressRule> | required | The whole new ordered list, possibly empty |

### Response bodies

#### ErrorBody

Returned by every route on `400`, `401`, `403`, `404`, `409` and `503`. The `code` is a short `snake_case` word the UI may branch on; the `message` is for the person and names the field or rule.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `error` | object | always | The error |
| `error.code` | string | always | For example `slug_taken`. Beyond the status words (`bad_request`, `unauthorized`, `forbidden`, `not_found`, `conflict`, `unavailable`), the UI may branch on `changed_by_others` (`409`), `registry_unavailable` (`503`), `owner_protected` (`400`: the owner is not a grant row), `no_such_grant` (`404`), `registry_role_missing` (`403`: the write needs a database role the caller's token does not give), `row_exists` (`409`), `row_missing` (`404`), `invalid_row` (`400`), `user_token_missing` and `cross_origin` (`403`), `method_not_allowed` (`405`) and `internal_error` (`500`). A code that belongs to one route is named in the Routes tables |
| `error.message` | string | always | What failed and what to do next; never a stack trace, an SQL message or a path |

#### MeResponse

Returned by `GET /me`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `email` | string | always | The actor's email, lower case |
| `groups` | array<string> | always | The actor's Databricks group names (direct and indirect), from the cache of [design 9.1](../../../docs/product/v1-design.md#91-the-model) |
| `is_platform_admin` | boolean | always | Whether the actor holds `CAN_MANAGE` on the platform |

#### AppSummary

One app on the launcher.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `slug` | string | always | The app |
| `display_name` | string | always | Shown in the launcher |
| `state` | string | always | One of `stopped`, `starting`, `running`, `crashed` |
| `permission` | string | always | The actor's level: `CAN_USE` or `CAN_MANAGE` (a platform admin holds `CAN_MANAGE` on every app) |
| `open_modes` | array<string> | always | Empty when the app has a single **Open**; `["without_token", "as_user"]` when the app is OBO-delegated and the actor is a platform admin who is neither the owner nor named by a grant |

#### AppList

Returned by `GET /apps`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `items` | array<AppSummary> | always | The apps the actor can open, ordered by `display_name`, then `slug` |

#### AppDetail

Returned by `GET /apps/{app}` and by every write that changes the `apps` row (`POST /apps`, `PATCH /apps/{app}`, `PUT /apps/{app}/owner`, `PUT /apps/{app}/egress`, `PUT` and `DELETE /apps/{app}/delegation/{kind}`), so `version` is always the one after the write. Only live releases are listed; a deleted release answers `404`. A release that failed after it became the target stays the target while the active release keeps serving, so `config.target_release_no` and the `active` release differ after a failed Update ([design 5.1](../../../docs/product/v1-design.md#51-states)).

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `slug` | string | always | The app |
| `display_name` | string | always | Shown in the launcher |
| `owner` | string | always | Email of the owner (always an app admin) |
| `source_path` | string | always | Folder snapshotted on Update, read with the presser's token |
| `memory_budget_mb` | integer | always | 64 to 16384 |
| `state` | string | always | One of `stopped`, `starting`, `running`, `crashed` |
| `runtime` | string or null | always | The runtime detected in the active release, `python` or `node`; `null` while no release is active |
| `restart_to_apply` | boolean | always | `true` when a resource or an own-SP key changed after the app last started ("restart to apply") |
| `version` | integer | always | Version of the app row; `UpdateAppRequest.version` |
| `created_by` | string | always | Who registered it, or approved the request for it |
| `created_at` | time | always | |
| `updated_by` | string | always | Who last changed a platform field |
| `updated_at` | time | always | |
| `releases` | array<ReleaseSummary> | always | Live releases, newest `release_no` first |
| `requirements` | RequirementsResponse | always | The Requirements panel |
| `resources` | array<ResourceView> | always | The app's resources, ordered by `key`; secrets as references |
| `config` | object | always | What the app admin controls |
| `config.desired_state` | string | always | One of `running`, `stopped` |
| `config.target_release_no` | integer or null | always | The release chosen to go live; `null` before the first Update |
| `config.env` | array<EnvEntry> | always | The target release's own env entries; empty without a target |
| `config.wheel_sources` | array<string> | always | The target release's wheel source folders; empty without a target |
| `config.health_path` | string | always | Path the health check requests |
| `config.health_deadline_s` | integer | always | Seconds a new release has to pass, 5 to 600 |
| `config.own_sp_client_id_key` | string or null | always | Key of the `secret` resource with the own SP's client id |
| `config.own_sp_client_secret_key` | string or null | always | Key of the `secret` resource with its client secret |
| `config.version` | integer | always | Version of the config row; `UpdateConfigRequest.version` |
| `config.updated_by` | string | always | |
| `config.updated_at` | time | always | |
| `delegation` | DelegationStatus | always | What a platform admin allowed this app |
| `egress` | array<EgressRule> | always | The app's own egress rules, in order; read-only for an app admin |
| `grants` | array<AppGrant> | always | The sharing list, `user` then `group` then `everyone`, then by `name`. The owner and the platform admins are not rows |

#### AppStateResponse

Returned by `POST /apps/{app}/start`, `/stop` and `/restart`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `state` | string | always | The run state right after the action: `stopped`, `starting`, `running` or `crashed` |
| `desired_state` | string | always | `running` or `stopped` |

#### ReleaseSummary

One release in the app page's list.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `release_no` | integer | always | The "v7" users see |
| `status` | string | always | One of `pending`, `building`, `starting`, `healthy`, `active`, `failed`, `superseded` |
| `status_reason` | string or null | always | Why a release is `failed` |
| `status_at` | time | always | When the status last changed |
| `name` | string or null | always | The release's name (`label`), 1 to 80 characters |
| `tags` | array<string> | always | Free labels; they protect nothing |
| `is_target` | boolean | always | `true` for the release `config.target_release_no` names |
| `locked_by` | string or null | always | Who locked it by hand; `null` when not locked by hand (the active release is protected without a lock) |
| `locked_at` | time or null | always | When; `null` exactly when `locked_by` is |
| `created_by` | string | always | Who pressed Update, or changed the config |
| `created_at` | time | always | |

#### ReleaseDetail

Returned by `GET /apps/{app}/releases/{id}` and by the release writes (`activate`, `PUT …/{id}`, `PUT` and `DELETE …/lock`). It holds every field of `ReleaseSummary` and adds the content of the release.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `release_no` | integer | always | The "v7" users see |
| `status` | string | always | One of `pending`, `building`, `starting`, `healthy`, `active`, `failed`, `superseded` |
| `status_reason` | string or null | always | Why a release is `failed` |
| `status_at` | time | always | When the status last changed |
| `name` | string or null | always | The release's name (`label`) |
| `tags` | array<string> | always | Free labels |
| `is_target` | boolean | always | `true` for the target release |
| `locked_by` | string or null | always | Who locked it by hand |
| `locked_at` | time or null | always | When |
| `created_by` | string | always | Who pressed Update, or changed the config |
| `created_at` | time | always | |
| `snapshot_digest` | string | always | `sha256:` and 64 hex digits |
| `build_log_tail` | string or null | always | The last 40 lines of the build, kept for failed releases |
| `detected` | object | always | What appmux found in the snapshot ([design 5.4](../../../docs/product/v1-design.md#54-appyaml-requirements-and-start)) |
| `detected.runtime` | string | always | `python` or `node`; a release whose snapshot recorded no runtime (detection failed) shows `python`, so that its `status_reason` can still be read |
| `detected.framework` | string or null | always | A hint such as `fastapi`; never a gate |
| `detected.command` | array<string> | always | The start command as an argument list |
| `detected.env` | array<EnvEntry> | always | The `env` entries of `app.yaml` |
| `detected.bundle_resources` | array<BundleResource> | always | The offers read from `databricks.yml`; they grant and require nothing |
| `detected.warnings` | array<string> | always | Ignored keys and unresolved values, shown on the app page |
| `config` | object | always | What the app admin added without touching the source |
| `config.env` | array<EnvEntry> | always | The release's own env entries |
| `config.wheel_sources` | array<string> | always | Workspace folders wheels are read from |
| `wheels` | array<object> | always | The wheels the install used; empty before the install |
| `wheels[].sha256` | string | always | 64 hex digits |
| `wheels[].filename` | string | always | The wheel file name |
| `wheels[].source` | string | always | The folder it came from |

#### ReleaseCreated

Returned by `POST /apps/{app}/releases`: the Update is under way; read `GET …/releases/{id}` for progress.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `release_no` | integer | always | The new release |
| `status` | string | always | `pending` |

Sent with `202`.

#### ReleaseUnchanged

Returned with `200` by `POST /apps/{app}/releases` when the folder's digest equals a live release's digest: appmux reports "already up to date" and creates no release ([spec US1 scenario 5](../spec.md)). A created release answers `202` with `ReleaseCreated`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `status` | string | always | `up_to_date` |
| `release_no` | integer | always | The live release whose digest equals the folder's: the active release, unless an older live release is the one that matches. No release is created and `config.target_release_no` does not change |

#### ReleaseSourceListing

Returned by `GET /apps/{app}/releases/{id}/source`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `release_no` | integer | always | The release |
| `items` | array<object> | always | The snapshot's files, ordered by `path` |
| `items[].path` | string | always | Relative to the snapshot root |
| `items[].size_bytes` | integer | always | |
| `items[].kind` | string | always | `text` (can be read) or `binary` (listed, not returned) |

#### ReleaseSourceFile

Returned by `GET /apps/{app}/releases/{id}/source/{path}`. A binary file is `400` `not_text`, and so is a text file over 1 MiB.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `release_no` | integer | always | The release |
| `path` | string | always | The file |
| `size_bytes` | integer | always | |
| `content` | string | always | The file's text |

#### ConfigChangeResult

Returned by `PUT /apps/{app}/config`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `config_version` | integer | always | The new `config.version` |
| `release_no` | integer or null | always | The release created by a change of env entries or wheel sources; `null` when only the health check or the own-SP keys changed (they belong to the app, not to a release) |

#### BundleResource

One resource the release's `databricks.yml` declares, offered in the Requirements panel ([design 5.4](../../../docs/product/v1-design.md#54-appyaml-requirements-and-start)).

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `key` | string | always | The entry's `name`, which becomes the resource key |
| `type` | string | always | The appmux type mapped from the file's type key |
| `target` | string or null | always | The id or name; `null` when the file holds a `${…}` interpolation the admin must fill in |
| `permission` | string | always | As written in the file; it may not be a level of the type, which the release's `detected.warnings` then say |

#### Requirement

One key a release needs: every `value_from` of `detected.env` and of `config.env`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `key` | string | always | The resource key |
| `state` | string | always | One of `attached`, `missing` |
| `offer` | BundleResource or null | always | The bundle's entry for this key, when the file declares one |
| `suggested_type` | string or null | always | A hint from the key's name for a missing key with no offer; never a gate |

#### RequirementsResponse

Returned by `GET /apps/{app}/requirements`, and held in `AppDetail.requirements`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `release_no` | integer or null | always | The release the panel is for: the target; `null` before the first Update |
| `items` | array<Requirement> | always | The required keys, ordered by `key` |
| `unreferenced_offers` | array<BundleResource> | always | Resources the bundle declares that no `value_from` mentions and the app has not attached; they can be attached too |

#### ResourceView

Returned by `PUT /apps/{app}/resources/{key}`, and held in `AppDetail.resources`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `key` | string | always | The name `valueFrom` uses |
| `type` | string | always | A type of the catalogue |
| `target` | string | always | The object; `scope/key` for a `secret` |
| `permission` | string | always | A level of the type; `READ` for a secret |
| `access_as` | string or null | always | `obo`, `own_sp` or `shared_sp`; `null` exactly for a `secret` |
| `check_result` | string | always | One of `ok`, `unverified` |
| `attached_by` | string | always | The person whose token made the check |
| `attached_at` | time | always | |

#### AppGrant

Returned by `PUT /apps/{app}/grants`, and held in `AppDetail.grants`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `type` | string | always | One of `user`, `group`, `everyone` |
| `name` | string | always | An email, a group name, or `*` |
| `permission` | string | always | One of `CAN_USE`, `CAN_MANAGE` |
| `granted_by` | string | always | An app admin |
| `granted_at` | time | always | |

#### DelegationStatus

Held in `AppDetail.delegation`, which `PUT` and `DELETE /apps/{app}/delegation/{kind}` return. Pending requests for a delegation are read from `GET /requests`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `obo_delegated` | boolean | always | The visitor's token may be passed down to this app |
| `shared_sp_delegated` | boolean | always | The appmux service principal's token may be lent to this app |
| `shared_sp_delegation_allowed` | boolean | always | The platform master switch; while `false` a `shared_sp` delegation has no effect and no request for it is accepted |

#### RequestRow

One permission request ([data model 4.3](../../../docs/product/v1-data-model.md#43-permission_requests)). Returned by every route of the Requests table that changes or creates one, and held in `RequestList`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `request_id` | integer | always | |
| `app_slug` | string or null | always | The app; `null` for a `register` request until it is approved |
| `kind` | string | always | One of `register`, `delegation`, `settings` |
| `payload` | object | always | What is asked, by kind (see `CreatePermissionRequest`) |
| `reason` | string | always | The asker's reason |
| `requested_by` | string | always | The asker's email |
| `requested_at` | time | always | |
| `status` | string | always | One of `pending`, `approved`, `denied`, `withdrawn` |
| `closed_by` | string or null | always | The platform admin who answered, or the asker who withdrew; `null` while pending |
| `closed_at` | time or null | always | `null` exactly while pending |
| `closed_note` | string or null | always | The reason or note that came with the answer |
| `current` | object or null | always | The inbox's "current value next to the requested one": for `settings`, the app's current value of each key in `payload`; for `delegation`, the current flag of each key; `null` for `register` |
| `requires_reason` | boolean | always | `true` when `payload` holds a gate field (every `register`, `source_path`, `obo`, `shared_sp`), so approving needs a `reason` |
| `app_version` | integer or null | always | Only in the answer to `POST /requests/{id}/approve`: the `apps` version after the approval wrote or created the app's row, so the client holds the new one; `null` in every other answer, in `GET /requests`, and when approving only closed the request |

#### RequestList

Returned by `GET /requests`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `items` | array<RequestRow> | always | Every request the actor may see: their own, those of the apps they hold `CAN_MANAGE` on, and for a platform admin all of them. Ordered by `requested_at`, oldest first, then `request_id` |

The server sends one set in one order and the client builds the three views from it, as [design 9.4](../../../docs/product/v1-design.md#94-requests-to-a-platform-admin) orders them: the platform admin's **inbox** is the `pending` ones, oldest first (the order sent); the launcher's **own requests** (`requested_by` is the actor) are newest first, so the client reverses them; an **app's requests** (`app_slug` equals the app) are newest first too, a choice of this contract because the design names no order for the app page. There is no filter and no paging in v1, so the list grows with the history (recorded as [TD-007](../../../docs/tech-debt.md#td-007-get-requests-returns-the-whole-history)).

#### PlatformView

Returned by `GET /platform`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `settings` | object | always | The effective value of each key of [data model 4.1](../../../docs/product/v1-data-model.md#41-platform_settings) except `platform_admins` and `egress_global`, which have their own fields; a key never set holds its default |
| `settings.memory_reserve_mb` | integer | always | Memory kept for appmux itself |
| `settings.package_mode` | string | always | One of `index`, `wheelhouse`, `wheelhouse+index` |
| `settings.default_wheel_sources` | array<string> | always | Folders every Update also reads wheels from |
| `settings.shared_sp_delegation_allowed` | boolean | always | The master switch |
| `settings.max_releases_per_app` | integer | always | Live releases kept per app |
| `settings.max_locked_releases` | integer | always | Hand locks per app; at most `max_releases_per_app - 4` |
| `settings.request_timeout_s` | integer | always | Per-request timeout, at most 290 |
| `settings.max_request_body_mb` | integer | always | Request body cap |
| `settings.max_concurrent_requests` | integer | always | Requests in flight per app |
| `settings.build_timeout_s` | integer | always | Wall-clock limit of one build |
| `settings.max_processes_per_child` | integer | always | `RLIMIT_NPROC` per sandbox |
| `settings.child_cpu_pin` | boolean | always | Pin every sandbox to one CPU |
| `settings.attach_check_warehouse_id` | string or null | always | The SQL warehouse of the attach checks; `null` while unset |
| `admins` | array<Principal> | always | The `platform_admins` entries |
| `egress_global` | array<EgressRule> | always | The global egress rules, in order |
| `self_check` | SelfCheckResult | always | The last `self_check()` result |
| `memory` | object | always | The slot's memory ([design 5.7](../../../docs/product/v1-design.md#57-memory-and-admission-control)) |
| `memory.limit_mb` | integer | always | The container's memory limit |
| `memory.reserve_mb` | integer | always | `memory_reserve_mb` |
| `memory.committed_mb` | integer | always | Budgets of the running apps with the per-child overhead and any update overlap |
| `memory.free_mb` | integer | always | `limit_mb - reserve_mb - committed_mb` |
| `apps` | array<PlatformApp> | always | Every registered app, ordered by `display_name`, then `slug`: what the platform page lists ([design 10.1](../../../docs/product/v1-design.md#101-screens)) in this one response, so it needs no `GET /apps/{app}` per app (up to about 100) |

#### PlatformApp

One app on the platform page; held in `PlatformView.apps`. It carries the platform-controlled fields, the delegations and the app's own egress rules, and the `version` a `PATCH /apps/{app}` sends.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `slug` | string | always | The app |
| `display_name` | string | always | |
| `source_path` | string | always | The folder snapshotted on Update |
| `memory_budget_mb` | integer | always | 64 to 16384 |
| `owner` | string | always | Email of the owner |
| `obo_delegated` | boolean | always | The visitor's token may be passed down to this app |
| `shared_sp_delegated` | boolean | always | The appmux service principal's token may be lent to this app |
| `egress` | array<EgressRule> | always | The app's own egress rules, in order |
| `version` | integer | always | Version of the app row; the same value as `AppDetail.version` |

#### PlatformSetting

Returned by `PUT /platform/settings/{key}`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `key` | string | always | The key |
| `value` | any | always | The stored value |
| `updated_by` | string | always | |
| `updated_at` | time | always | |

#### AdminList

Returned by `PUT /platform/admins`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `items` | array<Principal> | always | The platform admins after the change, in the order sent; when the request added and removed nothing, the list in force, in its stored order |

#### EgressRules

Returned by `PUT /platform/egress`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `items` | array<EgressRule> | always | The global rules after the change, in order |

#### SelfCheckResult

Returned by `POST /platform/self-check`, and held in `PlatformView.self_check`. Any failed check makes the whole result failed, and a canary that cannot run, times out or raises counts as failed.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `passed` | boolean | always | `true` only when every check passed |
| `ran_at` | time | always | When the run ended |
| `duration_ms` | integer | always | How long it took |
| `checks` | array<object> | always | One entry per check of [design 6.4](../../../docs/product/v1-design.md#64-self-check-failures-and-gaps), in run order |
| `checks[].name` | string | always | The check's name, which the status page also shows |
| `checks[].passed` | boolean | always | |
| `checks[].detail` | string or null | always | Why it failed; `null` when it passed |

#### AuditRow

One `audit_log` row ([data model 4.9](../../../docs/product/v1-data-model.md#49-audit_log)).

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `id` | integer | always | Orders rows that share a timestamp |
| `ts` | time | always | |
| `actor` | string | always | An email, `db:<role>` or `appmux` |
| `action` | string | always | One of the action names of the data model |
| `app_slug` | string or null | always | `null` for platform-wide actions |
| `detail` | object | always | What changed; never a secret value |
| `reason` | string or null | always | The reason given, mandatory for gate actions |

#### AuditList

Returned by `GET /audit?app=&from=&to=&limit=`. `app` filters by slug. `to` defaults to now and `from` to 24 hours before `to`; `limit` defaults to 100 and is at most 1000. The window is half-open (`from <= ts < to`) and there is no cursor: to read further back, send the `ts` of the last row as the next `to`. A row whose stored text the audit model would no longer accept as a new entry (text shaped like a credential, which a change made by hand in the database can hold) is returned with `detail` `{}`, a fixed `reason`, and `withheld` in place of any of `actor`, `action` or `app_slug` that is not valid, so the fact stays visible and the text does not leave the server.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `from` | time | always | The start of the window applied |
| `to` | time | always | The end of the window applied |
| `items` | array<AuditRow> | always | Rows in the window, newest first |

#### MetricsRow

One app, or the platform (`app_slug` `_platform`), for one minute ([data model 4.8](../../../docs/product/v1-data-model.md#48-metrics), [design 10.2](../../../docs/product/v1-design.md#102-metrics)). The last eight fields are `null` in an app's row.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `app_slug` | string | always | The app, or `_platform` |
| `minute` | time | always | Start of the minute |
| `requests` | integer | always | Requests at the edge |
| `r2xx` | integer | always | Status class 2xx |
| `r3xx` | integer | always | Status class 3xx |
| `r4xx` | integer | always | Status class 4xx |
| `r5xx` | integer | always | Status class 5xx; the four classes add up to `requests` |
| `upstream_errors` | integer | always | 502 and 504 the edge made itself |
| `limited` | integer | always | Refused by the concurrency, body-size or rate caps |
| `lat_hist` | array<integer> | always | 11 counts; upper bounds in ms: 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, above |
| `bytes_in` | integer | always | |
| `bytes_out` | integer | always | |
| `in_flight_max` | integer | always | Peak concurrent requests |
| `ws_opened` | integer | always | WebSockets opened in the minute |
| `ws_open` | integer | always | WebSockets open at its end |
| `rss_mb_max` | integer | always | Peak resident memory of the child's process tree |
| `restarts` | integer | always | Restarts in the minute |
| `egress_bytes_out` | integer | always | |
| `egress_bytes_in` | integer | always | |
| `egress_dropped` | integer | always | Packets dropped by egress rules |
| `egress_dropped_by_rule` | object | always | Drops by rule id (`app:2`, `global:1`) as integers; `{}` when none |
| `mem_used_mb` | integer or null | always | `_platform` only |
| `mem_limit_mb` | integer or null | always | `_platform` only |
| `children_running` | integer or null | always | `_platform` only |
| `children_registered` | integer or null | always | `_platform` only |
| `admission_rejections` | integer or null | always | `_platform` only |
| `registry_errors` | integer or null | always | `_platform` only |
| `registry_ms_max` | integer or null | always | `_platform` only: slowest registry call in the minute |
| `edge_rss_mb` | integer or null | always | `_platform` only: the edge's own memory |

#### MetricsResponse

Returned by `GET /metrics?app=&from=&to=`. `app` is a slug, or `_platform` for the platform row; without it a platform admin gets every app and the platform row. `to` defaults to now and `from` to one hour before `to`; a window of more than 1440 minutes is `400`. The response holds at most 10000 rows in total (the default hour for 101 series is about 6000): when the window would return more, whole minutes are dropped oldest first, so every series keeps the same span, `from` moves to the start of the oldest minute kept and `truncated` is `true`. A client that needs the dropped minutes asks again with a shorter window or one `app`.

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `from` | time | always | The start of the window applied (later than asked when `truncated`) |
| `to` | time | always | The end of the window applied |
| `truncated` | boolean | always | `true` when the 10000-row cap dropped the oldest minutes |
| `items` | array<MetricsRow> | always | Minutes in the window, oldest first, then by `app_slug`; at most 10000 |

## Addendum: routes and fields for the admin UI overhaul (T187 onward)

Added 2026-11-03 after the owner's hand test ([ADR 0019](../../../docs/decisions/0019-give-the-admin-ui-its-own-identity-and-a-theme-switch.md),
[ADR 0020](../../../docs/decisions/0020-let-a-service-principal-hold-an-app-grant.md),
[UI design](../../../docs/product/admin-ui-design.md)). Everything above is unchanged except where a row below says
so. Each new schema gets an example in [examples/](examples/manifest.json) and a Pydantic and a Zod model, as for the
rest of the contract. A lane that finds a detail below wrong changes this section in the same commit as its code.

### Routes

| Route | Needs | Response | Returns / does |
|---|---|---|---|
| `GET /principals?q=&types=&limit=` | `principal.search`: a platform admin, or anyone with `CAN_MANAGE` on at least one app | `PrincipalList` | suggestions for the Sharing form. `q` is at most 64 characters of `A-Z a-z 0-9 space . _ @ + -` (anything else is `400`, because the text goes into a SCIM filter); `types` is a comma list of `user`, `group`, `service_principal`, `everyone` (default all); `limit` 1 to 50 (default 10). An empty `q` returns only the `everyone` row. Each directory is read with the person's token first and the platform's service-principal token when that is refused; a directory that neither can read is named `unavailable` in `sources` and the UI keeps free typing. `403` `user_token_missing` is not raised: the fallback exists for that |
| `GET /workspace/browse?path=` | `workspace.browse`: any visitor with a token | `WorkspaceListing` | the sub-folders and files of one workspace folder, read with the person's token (scope `workspace.workspace:read`). No `path` returns the roots (`My folder`, `Shared`). `400` for a path that does not start with `/Workspace/`, holds `..`, a control character or more than 512 characters; `404` `not_found`; `403` `no_access`; `503` `workspace_unavailable`. At most 200 entries, folders first, then by name (`truncated`). Writes nothing and records nothing |
| `GET /apps/{app}/logs?after=&limit=` | `app.view_logs`: `CAN_MANAGE` on the app (a platform admin included) | `AppLogs` | the last lines of the app's output, from a ring of 2000 lines or 256 KiB per app that survives a restart and a failed start; `after` is a `seq` the client already has (0 for all), `limit` 1 to 1000 (default 500). Lines pass the same credential masking as `build_log_tail`. Polling writes no audit row, except that the first read of an app by an actor in 10 minutes writes one `app.view_logs` row (no content) |
| `GET /apps/{app}/environment` | `app.view_environment`: `CAN_MANAGE` on the app | `AppEnvironment` | what the app is started with: runtime versions, variables and packages (see schema). A value that belongs to a `secret` resource is never returned (`is_secret` is `true`, `value` is `null`) |
| `GET /resources/types` | `resource.attach` on at least one app, or a platform admin | `ResourceTypeList` | the resource catalogue of [data model 4.7](../../../docs/product/v1-data-model.md#47-app_resources) with a label, the permission levels and the access modes of each type, so the UI holds no copy of it |
| `GET /resources/suggestions?type=&q=&limit=` | the same | `ResourceSuggestionList` | objects of one type the person can see, read with their token: SQL warehouses, serving endpoints, secret scopes (and, for `q` of the form `scope/`, the keys of a scope; never values), MLflow experiments, jobs. A type with no safe list call answers `items: []` and `source: "not_supported"`. `limit` 1 to 50 (default 20); `400` `unknown_type` |
| `GET /metrics?app=&from=&to=&step=` | as before | `MetricsResponse` | **changed**: `step` is `1m` (default: the stored minute rows, as before), `5m`, `15m` or `1h`; a value sent twice or outside the list is `400`. Over a step the counts, the histograms (bucket by bucket) and `egress_dropped_by_rule` (rule by rule) are summed; the peaks `in_flight_max`, `rss_mb_max`, `registry_ms_max`, `mem_used_mb` and `edge_rss_mb` are maximised; the gauges `ws_open`, `mem_limit_mb`, `children_running` and `children_registered` are the value of the step's latest minute; a column null in every minute stays null. `minute` is the start of the step, a whole multiple of the step from 2000-01-01T00:00Z, so steps fall on the same UTC instants whatever the window. A step the window cuts holds only the minutes inside it and keeps its own start; a step with no minute is left out. The window may be up to 7 days provided it holds at most 1440 steps, counted as its length divided by the step, rounded up (so 24 h at `1m`, 5 days at `5m`, 7 days at `15m` or `1h`); otherwise `400` `window_too_long`, whose message names the smallest `step` that fits (none does past 7 days). The 10000-row cap and `truncated` are unchanged, with whole steps dropped in place of whole minutes |
| `GET /metrics/breakdown?from=&to=&step=&limit=` | `CAN_MANAGE` on the platform | `MetricsBreakdown` | the apps (not the `_platform` row) ranked by their requests in the window, most first, then by slug; the top `limit` (1 to 20, default 8; anything else is `400`) each as a series of points, the rest summed step by step as `other`; the same `step`, window rules and default window (the hour before `to`) as `GET /metrics`. There is no row cap: at most 21 series of 1440 steps |

New actions in `authz/actions.py`: `principal.search`, `workspace.browse`, `app.view_logs`, `app.view_environment`,
`resource.suggest` (the last answers both resource routes). They are reads: no `Sec-Fetch-Site` rule beyond the
usual, and no audit row except where a row says so.

### Changed schemas

- `Principal`, `GrantRequest`, `AppGrant`: `type` also takes `service_principal`. Its `name` is the service
  principal's application id, a lower-case UUID. It holds `CAN_USE` only (`400` for `CAN_MANAGE`, as for `everyone`).
  The edge matches it when the forwarded identity of the request equals that id (an App's service principal reaches a
  child with its application id as the identity, measured 2026-11-03, [reference](../../../docs/reference/mcp-and-a2a.md)).
  `DELETE /apps/{app}/grants/service_principal/{id}` removes it. Platform admins stay `user` or `group` only.
- `ReleaseDetail`: new `failure_hint` (string or null). `reason` names how a started child ended when it did
  (`the app exited with code 1 after 2.3 s`, `was stopped by signal SIGKILL`), the health wait ends when the process
  exits instead of at the deadline, and `build_log_tail` holds the child's last 50 lines of output for a failed start.
  `failure_hint` is one sentence the person can act on, for example that the memory budget is below what the runtime
  needs (the address-space limit is twice the budget, and Node.js needs about 768 MB); `null` when none applies.

### New schemas

#### PrincipalList

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `items` | array<PrincipalSuggestion> | always | `everyone` first when it matches, then groups, users, service principals, each by name |
| `truncated` | boolean | always | `true` when more than `limit` matched |
| `sources` | object | always | for `users`, `groups` and `service_principals`: one of `caller_token`, `platform_token`, `unavailable` |

`PrincipalSuggestion`: `type` (`user`, `group`, `service_principal`, `everyone`), `name` (what `GrantRequest.name` takes:
a lower-case email, a group name, an application id, `*`), `display_name` (string or null), `detail` (string or null:
the e-mail of a user whose display name is shown, the application id of a service principal).

#### WorkspaceListing

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `path` | string or null | always | The folder listed; `null` for the roots |
| `parent` | string or null | always | The folder above, `null` at a root |
| `roots` | array<{label, path}> | always | Filled only when `path` is `null`, else `[]` |
| `entries` | array<{name, path, kind}> | always | `kind` is `folder` or `file` |
| `truncated` | boolean | always | |
| `markers` | array<string> | always | Which of `app.yaml`, `databricks.yml`, `requirements.txt`, `package.json`, `pyproject.toml` the folder holds |
| `looks_like_app` | boolean | always | `true` when `app.yaml` or `requirements.txt` or `package.json` is a marker |
| `runtime_hint` | string or null | always | `python`, `node` or `null` |
| `suggested_memory_mb` | integer or null | always | The smallest budget the hinted runtime needs (768 for `node`), else `null`; from the same constant the launcher's limit comes from |

#### AppLogs

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `state` | string | always | The app's state as in `AppStateResponse`, or `never_started` |
| `instance` | string or null | always | Changes at every start; the client clears its view when it changes |
| `lines` | array<{seq, at, stream, text}> | always | Oldest first; `seq` rises by one; `stream` is `stdout`, `stderr` or `appmux` (a line appmux wrote about the app: started, exited with code) |
| `next_after` | integer | always | The `seq` to send as `after` next time |
| `dropped` | integer | always | Lines after `after` that the ring no longer holds |
| `last_exit` | object or null | always | `code`, `signal`, `at`, `ran_for_s` of the last end of a process, or `null` |

#### AppEnvironment

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `release` | integer or null | always | The active release the figures describe |
| `runtime` | object | always | `kind` (`python`, `node`, `unknown`), `python_version`, `node_version` (strings or null) |
| `variables` | array<{name, value, source, resource_key, is_secret}> | always | `source` is `config` (the release's env entries), `resource` (a `valueFrom`, shown by its key) or `platform` (set for every child: port, host, root path); `value` is `null` for a secret |
| `packages` | array<{name, version, source}> | always | `source` is `release` (declared by `requirements.txt` or `package.json`), `wheel` (resolved for this release) or `platform` (provided by the platform's own environment) |

#### ResourceTypeList and ResourceSuggestionList

`ResourceTypeList.items`: `type`, `label`, `description`, `permissions` (the levels, first is the default),
`access_as` (the modes the type allows, empty for a `secret`), `target_hint` (an example of a target, for the field's
placeholder), `can_suggest` (boolean). `ResourceSuggestionList`: `items` of `target`, `label`, `detail` (string or
null), `permissions`; `truncated`; `source` (`caller_token`, `unavailable`, `not_supported`); `note` (string or
null, in words, for example why the list is empty).

#### MetricsBreakdown

| Field | Type | Presence | Meaning |
|---|---|---|---|
| `from` | time | always | The start of the window applied |
| `to` | time | always | The end of the window applied |
| `step` | string | always | `1m`, `5m`, `15m` or `1h` |
| `series` | array<{app_slug, display_name, points}> | always | The top apps by requests, most first; `display_name` is a string, or `null` for an app no longer registered; `points` are `{minute, requests, r4xx, r5xx, rss_mb_max, bytes_out, egress_dropped, lat_hist}` per step, oldest first, with no gaps filled, summed as in `GET /metrics` (`minute` is the start of the step) |
| `other` | array of points or null | always | The apps below the cut added together step by step, every column summed (so its `rss_mb_max` is the sum of those apps' peaks of the step); `null` when every app has a series |

## Not in this contract

A route to run a child's command, to read a secret value, to read the registry directly, or to change a child's `app.yaml`: none exists and none may be added without an ADR (constitution I; [design 8.3](../../../docs/product/v1-design.md#83-secrets-where-the-value-comes-from)).
