# Databricks Free Edition — limits and measured behaviour

appmux is developed and integration-tested on a Free Edition workspace. This page records what that costs us so nobody researches it twice. Two kinds of fact, kept apart: **documented** (vendor page, re-read on the date shown) and **measured** (probe app on the maintainer's workspace). Anything in neither is listed under [Not confirmed](#not-confirmed) — do not assume it.

## Documented limits

Source: [Free Edition limitations](https://docs.databricks.com/aws/en/getting-started/free-edition-limitations), page dated 2026-10-18, read 2026-10-29. Re-read the page before relying on a number; limits change.

| Area | Limit |
|---|---|
| Apps | Up to **3 apps per account**. An app runs up to **24 hours** after it is started, updated or redeployed, then stops automatically; it can be restarted any time. |
| Lakebase | **One project per account**, with scale-to-zero compute. |
| Compute | Serverless only; no custom compute. Outbound internet access is limited to a set of trusted domains (the list is not on the page). |
| SQL warehouses | One, `2X-Small`. |
| Jobs | At most 5 concurrent job tasks per account. |
| Workspace / account | One workspace and one metastore per account. No account console or account-level APIs. No private networking, compliance or security customisation. |
| Sign-in | Email OTP, Google or Microsoft only; no SSO or SCIM. |
| Fair usage | Exceeding the quota shuts workspace compute down for the rest of the day (extreme cases: the month). Data and settings are kept. |
| Terms | No SLA or support. Non-commercial use only. Inactive accounts may be deleted. |
| Unsupported | R and Scala, custom workspace storage locations, online tables, clean rooms, legacy features, Knowledge Assistant. |

LinkedIn identity verification raises some limits (page says "some"; which ones is unspecified).

## Measured on the container (2026-10-29)

Probe app deployed to a Free Edition workspace; numbers are from running code there, not from docs. The probe sources are archived with checksums in [`probe-archive/`](../../specs/001-appmux-v1/spikes/probe-archive/); the one-page summary of what the platform does is [Platform at a glance](databricks-apps-platform.md#platform-at-a-glance).

| Fact | Value |
|---|---|
| Identity | uid 1000, no sudo, no seccomp |
| Kernel | `6.1.188-233.393.amzn2023.x86_64` (Amazon Linux 2023), Python 3.11.17, gcc 11.4.0 with `Python.h` (spikes 6 and 7) |
| Process visibility | `ptrace_scope` 0: same-uid processes read each other's `/proc/<pid>/environ`, PID 1 included |
| Platform secrets | `/databricks/runtime-secrets/runtime.cfg` openable by any same-uid process (see [the filesystem table](#filesystem-seen-by-a-same-uid-process-spike-2-2026-10-30)) |
| Limits | memory 8 GiB (cgroup v1), 4 CPUs visible but CPU time capped at about 2 CPUs by a quota the container cannot read (spike 7: busy loops on 1, 2, 3, 4 and 8 CPUs got 100, 199, 200, 201 and 199 %; `/proc/stat` shows about 40 % idle, no cgroup CPU file exists), `NPROC` unlimited, `/dev/shm` 64 MB; cgroup writes and `oom_score_adj` writes denied; only rlimits work (`RLIMIT_AS` is enforced) |
| Persistence | `/tmp` survives a redeploy; files written to the app directory did not (2026-10-29), but the `.venv` kept the packages installed by an earlier deployment of the same app (spike 13: `Requirement already satisfied … in ./.venv`); an app stop and start empties `/tmp` (spike 1), so after the daily auto-stop the first start has no cache |
| Runtimes | Python 3.11.17 (`/usr/bin/python3.11`), Node 22.23.4 and npm 10.9.9 (under `/usr/local`; spike 14) |
| Namespaces | user, mount, net and pid namespaces work unprivileged through `unshare` (launch 0.1–0.2 s) |
| TUN/TAP | `/dev/net/tun` absent, so `slirp4netns` in TAP mode and `pasta` fail |
| Init | PID 1 is `lha-runtime` and does not reap orphans: a process whose parent exits stays defunct under PID 1 (spike 7); a launcher needs `prctl(PR_SET_CHILD_SUBREAPER)` |
| Plain sibling process | reaches 77 of 89 localhost ports, the container IP and `169.254.169.254:80` |
| Egress | PyPI reachable from inside a sandbox at 11–12 MB/s; the workspace host resolves to a private address |
| Deployments | modes `AUTO_SYNC` and `SNAPSHOT`; `SNAPSHOT` copies the source to `/Workspace/Users/<app SP id>/src/<deployment id>` and keeps one copy per deployment |

The sandbox design these numbers led to is in the [v1 design](../product/v1-design.md#6-isolation-and-network).

### Filesystem seen by a same-uid process (spike 2, 2026-10-30)

An unsandboxed process of uid 1000 on the same container. Details and the mount table that hides all of it: [spike 2](../../specs/001-appmux-v1/spikes/README.md#spike-2--filesystem-exposure-2026-10-30).

| Fact | Value |
|---|---|
| `/databricks/runtime-secrets/` | `runtime.cfg` (712 B, mode 755, keys include `client_id` and `client_secret`) and two `*-auth-secret-expiration` files, readable |
| `/databricks/dynamic-secrets/` | readable read-only mounts: `shared/configs/deployment.json` (source path), `deployment-unit-info.json` (pod and node names), `effective-scopes.json`, a fluent-bit config template, `reserved-platform/…` |
| `/databricks/customer-logs/apps-runtime/databricksapps.log` | directory mode 777, file 0600 owned by uid 1000: readable **and writable** |
| `/dev/termination-log` | mode 0666 |
| App directory | `/app/python/source_code` (the app's source and its `.venv`) is writable by the app user; `/app/static` holds the platform's log page |
| `/home/app`, `/dev/shm`, `/var/tmp` | writable and shared by every process in the container |
| `/sys/class/dmi/id` | board, chassis and BIOS data readable |
| `/proc/self/cgroup`, `mountinfo` | name the pod (`/kubepods/burstable/pod<id>`) and host overlay paths (`/platform_disk/overlaybd-snapshots/…`) |
| Mounts | 43; the root is an `overlay` mounted read-write |
| fluent-bit | its template configures `http_listen: 0.0.0.0` and `http_port: 7270` (reachability from a sandbox is spike 6) |
| Nested user namespaces | `unshare(CLONE_NEWUSER)` still works after the capability drop, unless the process is `chroot`ed into a subdirectory of its root |
| SysV IPC | shared memory and message queues are visible to every process unless `unshare --ipc` is used |
| Interpreters | `python3` is 3.10.12; the app runs on `/usr/bin/python3.11` (3.11.17) with the environment at `/app/python/source_code/.venv` |
| App environment (spike 18) | `/app/python/source_code/.venv`, 132 distributions on the probe, `include-system-site-packages = false`, `pip` 24.0 inside, `uv` at `/usr/local/bin/uv`; owned and writable by the app user. The platform's own pins are the 17 top-level lines of `/databricks/preinstalled-uv-env/pyproject.toml` (for example `fastapi==0.115.0`, `uvicorn[standard]==0.30.6`, `streamlit==1.38.0`, `gradio==4.44.0`, `dash==2.18.1`, `flask==3.0.3`); the full list is in [the spike evidence](../../specs/001-appmux-v1/spikes/evidence/spike-18-environment-packages.txt) |
| `requirements.txt` install (spike 13) | `pip` 24.0 runs `-r requirements.txt` in the app environment during the deployment. `--no-index` and `--find-links ./wheels` are honoured (`./wheels` is relative to the deployment folder; no index is asked). A failed install makes that deployment the active one and the app `UNAVAILABLE`. No `requirements.txt`: `No dependencies file found. Skipping installation.` |
| App service principal and a workspace folder (spike 13) | Gets a token with its client id and secret (`/oidc/v1/token`, `client_credentials`, scope `all-apis`). Without a grant, listing a folder in the owner's home returns no objects and no error; after `CAN_READ` for the SP on the folder, the list and the raw download of `.whl` files (stored as `FILE`) work |
| Node offline install (spike 14) | `npm ci --offline --ignore-scripts --no-audit --no-fund --cache <read-only folder>` on npm 10.9.9 installed a 66-package lock from a cache made by npm 11.6.2, in a sandbox with its own network namespace (name resolution failing); npm only warned that it could not write its `_logs` folder. An empty cache, or a lock with an altered integrity, fails with `ENOTCACHED` |
| Secrets from an App (spike 11) | The app's service principal (not a workspace admin) reads a secret value with `READ` on the scope (REST and SDK) and can create, write and delete a scope of its own. A never-granted scope answers HTTP 403. A grant shows at once; after a revocation the old answer persisted for 163 s on one scope and alternated for about 6 min on another |
| Permission changes (spikes 11, 12, 16) | Eventually consistent: a removed secret `READ` or warehouse `CAN_USE` kept working for 1.5 to 6 min, with answers alternating before settling; group membership showed after 60 to 75 s |
| Workspace read with an OBO token (spike 15) | Scope `workspace.workspace:read` lets the token get the status of, list and export the owner's folder, not write; with no access the API returns an empty listing or a 404, not an error |

## What it means for appmux development

- **Only 3 apps.** appmux itself plus the probe plus one spare. The 10 + 8 simulated apps are children *inside* appmux, so they do not use app slots; only the appmux app does.
- **24-hour auto-stop.** Every day appmux restarts, so the restart path (`self_check()`, the active release, route re-opening) is exercised for free. A stop and start empties `/tmp`, so the environment cache is rebuilt each day. Anything that needs more than 24 hours of uptime (7-day metrics retention) cannot be observed on one run and needs a deploy-time or unit-level test.
- **One Lakebase project.** Dev and tests share it. Use a separate database or schema for tests, and a branch if branching exists on Free Edition (not confirmed). Scale-to-zero means the first connection after idle is slow, so reconnect and timeout logic is on the dev path.
- **Trusted-domain egress.** Sandboxed builds only reach what the platform allows. PyPI works (measured); npm and other indexes were not measured. A failed install on Free Edition may be the platform, not appmux.
- **Fair-usage shutdown.** Load tests for criteria 1 and 3 count against the quota; keep runs short and do not loop them.
- **No account-level APIs.** Creating service principals, account groups or account-level settings may be impossible; own-SP mode and OBO scope configuration need checking here.
- **Non-commercial.** Fine for developing an open-source tool; real use of appmux needs a paid workspace. The owner dropped the paid-workspace spike (no paid workspace to experiment on), so the first deploy there must re-run `self_check()` and the load run ([TD-005](../tech-debt.md#td-005-platform-measurements-come-from-free-edition-only)).
- **Measurements are Free Edition only.** A paid workspace may differ (compute size, seccomp, `ptrace_scope`, network). `self_check()` is the guard; the numbers above are not a contract.

## Not confirmed

Neither documented nor measured. Check before depending on any of these.

- Lakebase on Free Edition: OAuth Postgres roles, the `databricks_auth` extension, branching, column-level `GRANT`, `SET LOCAL ROLE`, and logging in with a user's token as the password (spike 1: it works with or without the `postgres` scope, the token is checked only at login, and an open connection kept working for at least 17 minutes after the token expired). The workspace had no Lakebase project on 2026-10-29; the spikes used a throwaway one, deleted at the end ([cleanup record](../../specs/001-appmux-v1/spikes/README.md#cleanup-2026-10-30)).
- Whether a human who is not a workspace admin can create a secret scope, add a scope to an App's `user_api_scopes`, or read a folder the admin cannot (the owner of this workspace is an admin; a service principal stood in as the principal with no access).
- The full list of trusted egress domains, and egress from the Apps container to anything other than PyPI.
- Service principal and secret-scope limits (the limits page does not mention them).
- Sizes an Apps container can have other than the measured 8 GiB / 4 CPUs, and whether the CPU quota of about 2 CPUs is the same on a paid workspace (spike 7 ran on Free Edition only).

## Re-checking

With the Databricks CLI logged in: `databricks apps list`, `databricks apps get <name>`, `databricks postgres list-projects`. The limits page above is the source for numbers; this file's tables must be updated in the same change when it differs.
