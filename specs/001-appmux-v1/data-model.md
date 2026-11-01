# Data model: appmux v1

The registry's nine Lakebase tables are specified in [docs/product/v1-data-model.md](../../docs/product/v1-data-model.md); this file does not repeat them. It records what the plan adds.

## What the plan fixes

- **Names are final for v1.** Table and column names, types, constraints and the privilege grants of [section 6](../../docs/product/v1-data-model.md#6-who-writes-what) become the baseline migration, `migrations/0001_baseline.sql`. A later change is a new numbered migration.
- **Seven new `platform_settings` keys** (no DDL, the code owns the key list): `request_timeout_s`, `max_request_body_mb`, `max_concurrent_requests`, `build_timeout_s`, `max_processes_per_child`, `child_cpu_pin`, `attach_check_warehouse_id`. Defaults and level of effect are in [data model 4.1](../../docs/product/v1-data-model.md#41-platform_settings) and the reasoning in [research D3, D10–D12](research.md).
- **One schema source.** The Pydantic models in `src/appmux/registry/` are the shape the code uses for rows, the `payload` of each request kind and the release `config`; the SQL baseline and these models are checked against each other by a test that applies the migration to a scratch Postgres and compares the columns.
- **Ordering note.** The baseline keeps the DDL order of the data model: the three composite keys of `app_config` are added by the `ALTER TABLE` after `releases` and `app_resources` exist ([4.7](../../docs/product/v1-data-model.md#47-app_resources)) and are deferred at cascade ([section 6](../../docs/product/v1-data-model.md#6-who-writes-what)).

## Entities and where they live

| Entity (spec Key Entities) | Table | Module that owns the access |
|---|---|---|
| Platform settings, platform admins | `platform_settings` | `registry`, read by `authz` through an in-memory snapshot |
| App, owner, egress rules | `apps`, `app_config` | `registry`, snapshot read by `routing` and `reconciler` |
| Release, snapshot digest, wheel list, detected manifest | `releases` | `reconciler` |
| Grant (`CAN_USE`, `CAN_MANAGE`) | `app_grants` | `authz` (pure over the snapshot) |
| Resource, secret reference | `app_resources` | `appyaml`, `secrets` |
| Request | `permission_requests` | `admin` (never read by `authz` or the reconciler, FR-045) |
| Metric row | `metrics` | `metrics` |
| Audit row | `audit_log` | every writer through one helper |

## State transitions

Release states and their order are in [design 5.1](../../docs/product/v1-design.md#51-states) and FR-005. The reconciler owns every transition; a test table lists each allowed pair, and any other is an error.
