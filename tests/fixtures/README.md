# Fixtures

The child apps that tests register through the normal path or start through the launcher
([design §14](../../docs/product/v1-design.md#14-testing), task T179). Each folder is a
complete, standalone Databricks-style app (`python app.py`, port in `DATABRICKS_APP_PORT`),
except `node_app`. The module docstring of each `app.py` states the expected outcome in full; this
table is only the map.

**Run a hostile or failing fixture only inside an appmux sandbox, through the launcher.**
`fork_bomb`, `memory_hog` and the snoopers exist to prove the sandbox holds; on a workstation they
do what their names say.

## The well-behaved apps (four kinds; SC-003 registers ten, a mix of these)

| Folder | Purpose |
|---|---|
| `light_fastapi` | Answers fast and holds almost no memory: the load fixture of SC-001 and the neighbour whose p95 the hostile fixtures must not disturb. |
| `memory_fastapi` | Holds a configurable amount of touched memory, for the budget and admission-control tests (design 5.7). |
| `streamlit_app` | A minimal Streamlit app: routing, framework variables and memory under a real framework. |
| `node_app` | A Node HTTP server with no dependency and no `app.yaml`: runtime detection and the default start command. |

## Hostile apps (the sandbox must hold)

| Folder | Purpose |
|---|---|
| `env_snooper` | Tries to read other processes' environments and platform secrets (FR-011). |
| `pkg_env_writer` | Tries to write into the shared package environment or an overlay; every attempt must fail with `EROFS`. It pins one wheel (`make_wheels.py`) so its release has an overlay to probe. |
| `network_scanner` | Tries `localhost`, the container IP, the metadata endpoint and other destinations a child must not reach. |
| `fork_bomb` | Forks until refused: proves `RLIMIT_NPROC` (`max_processes_per_child`). |
| `memory_hog` | Allocates until stopped: proves the memory watchdog and address-space limit (TD-003). |
| `secret_registry_snooper` | Tries other apps' secrets and the registry, with and without the shared-SP token (design 8.3, 8.4). |

## Failing apps (a release must fail or an app must stop in the way the design says)

| Folder | Purpose |
|---|---|
| `startup_crash` | Exits with status 3 before binding a port: the release ends `failed`, its route never opens (US1 scenario 3). |
| `always_500` | Listens and answers 500 everywhere: health never passes (US1 scenario 4). |
| `slow_start` | Binds its port after a configurable delay: healthy inside `health_deadline_s`, `failed` past it (US1 scenario 5). |
| `crash_loop` | Passes health, then exits shortly after every start: the app ends `crashed`, only its route answers 503 (US2). |

## Routing fixtures

| Folder | Purpose |
|---|---|
| `absolute_links` | Hard-codes `/static/x.css`: the page loads, the stray request gets the edge's `404`, never a misroute (design 7.2). |
| `base_path_aware` | Builds every link from `X-Forwarded-Prefix` and `APPMUX_BASE_PATH`. |

## Offline-install fixtures (the package index is unreachable)

| Folder | Purpose |
|---|---|
| `wheels_only` | Two dependencies that exist only as wheel files in the release. |
| `wheel_reuse` | Needs a wheel `wheels_only` already put in the store: nothing is fetched again (US7 scenario 4). |
| `env_covered` | Every requirement is met by the App environment: no wheel fetched, no overlay built (US7 scenario 1). |
| `newer_wheel` | Needs a newer version of a package the environment holds: its wheel wins in the overlay (US7 scenario 2). |

`make_wheels.py` in each of these builds its stand-in wheels offline.

## `app.yaml` and `databricks.yml` fixtures (US6)

| Folder | Purpose |
|---|---|
| `appyaml_missing_resource` | A valid `app.yaml` whose two `valueFrom` keys have no resource: the release fails before any build, listing both. |
| `appyaml_invalid` | Both `app.yaml` and `app.yml`, and an `env` entry with both `value` and `valueFrom`: Update refuses it. |
| `bundle_offer` | A `databricks.yml` declaring six resources: one offer each, nothing attached until confirmed. |
