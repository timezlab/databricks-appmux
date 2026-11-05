# Spike results — appmux v1

Results of the spikes approved in [spec.md](../spec.md#spikes-for-owner-approval).
One entry per spike, in the order they ran. Each entry gives the date, what was
run, what was observed, pass or fail, and the objects created and removed.

**Where and how.** All runs used the owner's Free Edition workspace only
(`appmux-probe` app, throwaway objects named `appmux-spike-<n>-…`). The owner
approved the spikes on 2026-10-30 and dropped spike 3 (paid workspace): the
company workspace is **not** used, so every container measurement here is
labelled *Free Edition only*. Tokens were read from the CLI inside the process
that needed them and never printed or stored; workspace identifiers and the
owner's address are masked as `<workspace-id>`, `<workspace-host>` and
`<owner-email>` in everything committed here.

**Layout.**

| Path | What it holds |
|---|---|
| `probe-archive/` | Sources of the two 2026-10 probes behind [the platform reference](../../../docs/reference/databricks-free-edition.md), with checksums (run 0) |
| `scripts/` | The harness app and the client scripts that produced the evidence; placeholders replace the host and owner address |
| `evidence/` | Raw observations, one file per spike |

**Reproducing.** `scripts/deploy.sh` syncs a folder and deploys it to a named
app; `scripts/rc.sh` calls the deployed harness with the CLI's bearer token;
`scripts/s08.py` is the spike 8 client (`SPIKE_HOST` names the app host).
`scripts/spike-05/` holds `req.txt` (the pinned Python set from
`uv pip compile --exclude-newer`), `apps/` (the five fixtures),
`prefix_proxy.py` the proxy and `s05.py` the driver (needs a virtual
environment named `venv` beside it and a Chromium headless shell).

## Summary

| Spike | Class | Result | Date |
|---|---|---|---|
| 0 Archive the probe sources | prerequisite | done | 2026-10-30 |
| 8 Databricks proxy behaviour | Gate | **pass** | 2026-10-30 |
| 17 `app.yaml` on the real platform | Adjusts | **pass** (design differs; corrected) | 2026-10-30 |
| 3 Paid workspace | Adjusts | **dropped** by the owner | 2026-10-30 |
| 5 Base path per framework | Gate | **pass** (design differs; corrected) | 2026-10-30 |
| 2 Filesystem exposure | Gate | **pass** (design differs; corrected) | 2026-10-30 |
| 18 Package overlay on the real container | Gate | **pass** (design differs; corrected) | 2026-10-30 |
| 13 Parent-app wheel bootstrap | Adjusts | **pass** (design differs; corrected) | 2026-10-30 |
| 14 Node offline source and wheel coverage | Adjusts | **pass** (design differs; corrected) | 2026-10-30 |
| 1 OBO login to Lakebase | Gate | **fail as written** (login works without the scope; write path passes; the owner accepted the residual on 2026-10-30) | 2026-10-30 |
| 9 Cascade privileges on Lakebase, `slirp4netns` licence | Gate | **pass** (design differs; corrected) | 2026-10-30 |
| 12 Nested groups in the SCIM `Me` response | Adjusts | **pass** (design differs; corrected) | 2026-10-30 |
| 4 Content-store location | Gate | **pass** (design differs; corrected) | 2026-10-30 |
| 19 Next.js admin UI on the platform | Adjusts | **pass** (design differs; corrected) | 2026-10-30 |
| 11 Secrets API from an App | Gate | **pass** (design differs; corrected) | 2026-10-30 |
| 15 Workspace file read with the presser's OBO token | Gate | **pass** (design differs; corrected) | 2026-10-30 |
| 16 Per-type attach checks | Adjusts | **pass** (design differs; corrected) | 2026-10-30 |
| 10 Shared-SP token refresh | Adjusts | **pass** (design differs; corrected) | 2026-10-30 |
| 6 Egress mechanics | Adjusts | **pass** (design differs; corrected) | 2026-10-30 |
| 7 Throughput, hogs, forks and builds | Adjusts | **pass** (design differs; corrected) | 2026-10-30 |

## Run 0 — Archive the probe sources (2026-10-30)

- **Command.** Copied the two probe applications (`appmux-probe`,
  `appmux-probe2`) from the scratch area into `probe-archive/` and wrote
  `SHA256SUMS` for the sources; the two third-party binaries the second probe
  fetched (`slirp4netns`, `pasta`) are **not** stored — only their checksums,
  in `appmux-probe2/binaries.SHA256SUMS`, per
  [ADR 0013](../../../docs/decisions/0013-license-appmux-under-apache-2-0.md).
- **Result.** Done. Nothing was created in the workspace.

## Spike 8 — Behaviour of the Databricks proxy (2026-10-30)

- **Run.** Redeployed `appmux-probe` as `scripts/harness/app.py` (stdlib
  HTTP/1.1 server with echo, SSE, WebSocket, sleep, large-body and header
  endpoints) and drove it with `scripts/s08.py` using the CLI's OAuth bearer
  token (`Authorization: Bearer` from `databricks auth token`). Raw output:
  [evidence/spike-08.json](evidence/spike-08.json).
- **Result: pass.** Every question has an observed answer; sub-paths arrive
  with path and query intact and a client-supplied `X-Forwarded-Email` is
  overwritten.

| Question | Observed |
|---|---|
| Sub-paths forwarded? | Yes. `/echo/a/b?x=1` reaches the app as `/echo/a/b?x=1`; no prefix is stripped. Methods GET, POST, PUT, DELETE and OPTIONS all arrive. |
| Path handling | The **proxy normalises before the app sees the path**: `/echo/a/b/../c` → `/echo/a/c`; `/echo/%2e%2e/echo/x` → `/echo/x`; `//echo/x` and `/echo//x` get a **301** to the single-slash path (the app never sees them). Kept as sent: `%2F` stays encoded, `;` parameters, percent-encoded UTF-8, trailing slash, a 4,000-character query. |
| WebSocket | Works. `101 Switching Protocols`, echo, 60 KB frame; an **idle connection survived 30 s, 120 s and 330 s**. The 300 s request limit does not apply after an upgrade. |
| SSE | Streams chunk by chunk (events arrived 1 s apart). A stream is cut at the request limit: a stream sending every 10 s ended cleanly at **302 s** after 30 events. |
| Timeouts | A request that idles 30, 60 or 120 s succeeds; at **300 s the proxy answers 504 `upstream request timeout`** (300.95 s). The app receives `X-Envoy-Expected-Rq-Timeout-Ms: 300000`. |
| Body size | Requests of 1, 10 and 100 MB: 200, every byte received (about 10 MB/s). Responses of 1, 10 and 100 MB: 200, full length. No limit found up to 100 MB. |
| Header size | One request header of 4, 8, 16 and 32 KB: 200; **64 KB: 431**. Response header of 4–32 KB: passed intact; **64 KB: 503** and the response is lost. |
| `Set-Cookie` | Passed unchanged, including `Domain`, `Path` and a `__Host-` cookie. The proxy **adds its own** `Set-Cookie: __Host-databricks-app-router=; Expires=1970…` to responses. |
| Redirects | A relative `Location: /login` on a 302 is passed unchanged (no rewrite by the proxy). |
| Client-supplied `X-Forwarded-*` | **Overwritten** with the real values: `X-Forwarded-Email`, `X-Forwarded-User`, `X-Forwarded-Access-Token`, `X-Forwarded-Host`, `X-Real-Ip`, `X-Databricks-Org-Id`. **`X-Forwarded-For` is appended to, not replaced**: a client value stays the leftmost entry (`6.6.6.6,<real client>,<hop>,<hop>`). `Cookie` is forwarded intact. `Authorization` is **not** forwarded to the app. |
| HTTP version to the app | `HTTP/1.1`, both to the stdlib server and to a server that supports H2C (hypercorn 0.18.0, installed from `requirements.txt`). No H2C upgrade or prior-knowledge request arrived; the client side of the proxy is TLS, so a prior-knowledge request cannot be sent through it. |
| CLI bearer accepted? | **Yes.** The proxy accepts the CLI's OAuth token and sets `X-Forwarded-Email` to its owner. The app also receives an `X-Forwarded-Access-Token` of a different length (991 against 962 characters for the CLI token), so the proxy mints a token for the app. No bearer, or an invalid one: **302** to the workspace OIDC page, never 401. |
| Identity spoof without a valid bearer | A request with `X-Forwarded-Email` set to the owner's address and a bogus bearer got 302, not the app. |

- **Consequences.**
  - [ADR 0006](../../../docs/decisions/0006-route-by-path-prefix-behind-a-routeselector.md)
    holds: sub-paths arrive intact. appmux still normalises and rejects
    ambiguous paths itself, because it must not depend on the platform doing
    so.
  - The edge may trust `X-Forwarded-Email`, `-User` and `-Access-Token`; it
    must take the client address from `X-Real-Ip`, which the proxy overwrites.
    *(Corrected 2026-10-30: this line first said "the rightmost entry added by
    the proxy", but the table above shows the proxy appends its own hops after
    the real client, so the rightmost entry is a hop, and the leftmost is
    client-controlled.)*
  - The edge needs HTTP/1.1; H2C is optional
    ([ADR 0012](../../../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md)).
  - Platform limits every child inherits: **300 s per request or stream**
    (long SSE must reconnect), **32 KB** of any single request or response
    header (64 KB fails), and no body limit up to 100 MB.
  - `Location` rewriting stays appmux's job; the proxy does none.
  - Load for success criterion 1 can be generated from the CLI with the
    bearer token, and the OBO path (`X-Forwarded-Access-Token`) can be
    exercised the same way — this unblocks spikes 1, 12, 15 and 16 without a
    browser.
- **Created and removed.** Created the throwaway app `appmux-spike-17` for the
  H2C check (removed, see spike 17). The `appmux-probe` app now runs the
  harness and stays until the last spike ends.

- **Correction (2026-11-03).** "Got 302, not the app" holds for a request with no `Accept` header,
  which is what the probe sent. A request whose `Accept` includes `application/json`, as MCP and
  A2A clients send, gets **401 with the body `{}`** for the same causes (a bogus bearer, no
  bearer, or a service principal with no `CAN USE` on the target). Measured with the
  [example MCP and A2A apps](../../docs/reference/mcp-and-a2a.md#what-each-failure-looks-like).

## Spike 17 — `app.yaml` on the real platform (2026-10-30)

- **Run.** Deployed three variants to a throwaway app `appmux-spike-17` and read
  the deployment log and the process's own `argv` and environment
  ([scripts/spike-17/](scripts/spike-17/),
  [evidence/spike-17-deploy-logs.txt](evidence/spike-17-deploy-logs.txt)); the
  environment values of the framework variables were read from the harness
  on `appmux-probe`.
- **Result: pass.** The syntax, the missing-key behaviour and the variable list
  are recorded; the design's assumption about `${NAME}` was wrong.

| Question | Observed |
|---|---|
| `$DATABRICKS_APP_PORT` in a `command` argument | **Substituted**: `command: [python, app.py, --port, $DATABRICKS_APP_PORT]` ran as `python app.py --port 8000`. |
| `${DATABRICKS_APP_PORT}` in a `command` argument | **Not substituted**: the process received the literal `${DATABRICKS_APP_PORT}`. |
| The same strings in an `env` `value` | **Never substituted**, in either form. |
| A `valueFrom` naming no resource | The build log prints `[ERROR] error resolving resource <name> for env <VAR>: resource <name> not found`, but **the deployment still succeeds and starts**; the variable is absent. |
| Variables a real App receives | `DATABRICKS_APP_NAME`, `DATABRICKS_APP_PORT` (8000), `DATABRICKS_APP_URL`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`, `DATABRICKS_HOST`, `DATABRICKS_WORKSPACE_ID`, `PORT` (8000), `HOME=/home/app`, `LC_CTYPE=C.UTF-8`, `PYTHONUNBUFFERED=1`, `UV_PYTHON_INSTALL_DIR=/app/.uv-python`, and the framework defaults `FLASK_RUN_HOST=0.0.0.0` and `FLASK_RUN_PORT`, `GRADIO_ANALYTICS_ENABLED=false`, `GRADIO_SERVER_NAME=0.0.0.0` and `GRADIO_SERVER_PORT`, `STREAMLIT_BROWSER_GATHER_USAGE_STATS=false`, `STREAMLIT_SERVER_ADDRESS=0.0.0.0`, `STREAMLIT_SERVER_ENABLE_CORS=false`, `STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=false`, `STREAMLIT_SERVER_HEADLESS=true`, `STREAMLIT_SERVER_PORT`, `UVICORN_HOST=0.0.0.0`, `UVICORN_PORT` (every `*_PORT` is 8000). No `OTEL_*`. |
| Also seen | The app process is `python app.py` on `/usr/bin/python3.11` (3.11.17) with `PATH=/usr/local/bin:/usr/bin:/bin:.venv/bin:node_modules/.bin`; `python3` without a venv resolves to 3.10.12, so a child must be started with the 3.11 interpreter explicitly. `requirements.txt` is installed from the public index during deployment (`hypercorn==0.18.0` and its dependencies, log line `Requirements installed successfully`). |

- **Consequences.** appmux mirrors the platform: it substitutes `$NAME` only,
  per argument and with no shell, never inside `env` values, so a child that
  runs on a standalone App runs the same under appmux. The design's "unverified"
  note and `${NAME}` rule are corrected; appmux's own list of platform
  variables gains `STREAMLIT_SERVER_ENABLE_CORS`,
  `STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION`, `GRADIO_ANALYTICS_ENABLED`,
  `LC_CTYPE`, `PYTHONUNBUFFERED` and `UV_PYTHON_INSTALL_DIR`. The design
  already withholds `DATABRICKS_CLIENT_ID` and `_SECRET` from a child (a child
  has no service principal of its own, design premise 1), and that stays;
  whether to pass `DATABRICKS_APP_URL` is left to the plan. A missing
  `valueFrom` resource does **not** stop a standalone deploy, so refusing the
  release before anything starts is a deliberate difference that stays in the
  "differs" table.
- **Created and removed.** Created app `appmux-spike-17` and workspace folders
  `spike17-a`, `spike17-b`, `spike17-c`, `spike17-h2`; all deleted at the end of
  the spike.

## Spike 3 — dropped

The owner dropped this spike on 2026-10-30: the company's paid workspace is not
used for any measurement. All container facts in this folder and in the
[platform reference](../../../docs/reference/databricks-free-edition.md) stay
labelled *Free Edition only*.

## Spike 5 — Base path per framework (2026-10-30)

- **Run.** Local, not on the platform. Each framework's smallest app ran
  **unmodified** behind [scripts/spike-05/prefix_proxy.py](scripts/spike-05/prefix_proxy.py),
  a standard-library proxy that serves `/sales/x` to the app as `/x`, adds
  `X-Forwarded-Prefix: /sales`, relays WebSocket upgrades, and answers `404` to
  any request outside `/sales/` while listing it as *stray*. The driver
  [scripts/spike-05/s05.py](scripts/spike-05/s05.py) opened the page in headless
  Chromium and checked the visible result: one asset, one API call and one
  WebSocket (or the framework's own event channel) had to work. Each framework
  also ran **without** the setting as a control, and the platform's own
  framework variables (spike 17) were set for every run. Raw rows:
  [evidence/spike-05.json](evidence/spike-05.json).
- **Environment.** Python 3.12.3 locally (the container has 3.11.17), packages
  pinned in [req.txt](scripts/spike-05/req.txt) with `--exclude-newer
  2026-10-16` (a two-week cooldown): fastapi 0.143.2, uvicorn 0.54.0,
  streamlit 1.66.0, dash 4.4.1, gradio 6.30.0, playwright 1.64.0; Node
  express 5.3.0 and ws 8.22.0 installed with `--ignore-scripts`. OSV.dev: **no
  `MAL-` advisory** for any of the 84 Python pins or the two npm packages;
  known non-malware advisories on `brotli`, `certifi`, `filelock`, `pillow` and
  `setuptools` are irrelevant to a throwaway local rig that serves nothing
  outside loopback.
- **Result: pass.** Every framework has a recorded row (below). The design's
  candidates were partly wrong: Streamlit's `baseUrlPath` **breaks** under a
  proxy that strips the prefix.

| Framework | Setting that works (prefix **stripped**) | Without it | Notes |
|---|---|---|---|
| FastAPI | `UVICORN_ROOT_PATH=/sales` | page loads, `/static/app.js` goes out as a stray request, the API call and WebSocket never run | read by the `uvicorn` command line, which is how the fixture ran; in uvicorn's source only the command-line entry point reads `UVICORN_*`, so `uvicorn.run()` in code would not see it (read, **not** run) |
| Streamlit | **none** — leave it unset | works as is, including a deep link to a second page (`/sales/p2`) and the WebSocket | `STREAMLIT_SERVER_BASE_URL_PATH=sales` **fails** when the prefix is stripped (no WebSocket opens and the title never renders in 30 s) and works only when the prefix is kept. The frontend uses relative URLs |
| Dash | `DASH_REQUESTS_PATHNAME_PREFIX=/sales/` and `DASH_ROUTES_PATHNAME_PREFIX=/` | 9 stray asset requests, no layout | both variables are needed: the first makes the browser's URLs carry the prefix, the second keeps the server's routes unprefixed. Layout and a callback round trip worked |
| Gradio | `GRADIO_ROOT_PATH=/sales` | 3 stray requests (theme, script, `POST /gradio_api/queue/join`), the event never completes | `launch()` called with no `root_path`; the queue round trip worked |
| Node / Express | **none** — relative links only | n/a | an app that writes `/static/app.js` fails whatever the setting, as the next row shows |
| Any framework with hard-coded absolute links | none | — | FastAPI and Express fixtures that write `<script src="/static/app.js">`: the page loads, the stray request is counted and answered `404`, the page stays broken — as ADR 0006 and design §7.2 already say |

- **Scenario counts.** 14 scenarios (the rows above, their controls, a
  Streamlit deep link with and without `baseUrlPath`, and the prefix kept
  instead of stripped for Streamlit). Stray requests in every scenario that
  worked: **0**. WebSocket upgrades reached the app for FastAPI, Streamlit and
  Express; the Dash and Gradio fixtures made none (their events travel over
  plain HTTP).
- **Consequences.**
  - **Hostable in v1** (with the setting): FastAPI, Streamlit, Dash, Gradio, and
    Node apps that use relative links. None of the five failed, so
    `prefix-rescue` stays beyond v1 ([ADR 0006](../../../docs/decisions/0006-route-by-path-prefix-behind-a-routeselector.md)).
  - appmux sets `UVICORN_ROOT_PATH`, `GRADIO_ROOT_PATH` and the two
    `DASH_*` variables for every child, alongside `APPMUX_BASE_PATH` and
    `X-Forwarded-Prefix`; a variable a framework does not read is inert. It must
    **not** set any Streamlit base-path variable. The framework name stays a
    hint, never a gate.
  - The design's candidate list is corrected in
    [design §5.4](../../../docs/product/v1-design.md#54-appyaml-requirements-and-start).
- **Not covered here.** Everything was run locally on Python 3.12, with
  appmux's proxy behaviour imitated by a 120-line relay. It shows the framework
  settings, not the sandbox, the Databricks proxy (spike 8 covers it) or the
  container's Python 3.11. Applications that start the server from code
  (`uvicorn.run(...)`, `app.run_server`) rather than the command line were not
  exercised for FastAPI; the plan's fixtures (spec §Success criteria) must
  cover that form.
- **Created and removed.** Nothing in the workspace. Local: a throwaway virtual
  environment and a `node_modules` directory in the scratch area, outside the
  repository.

## Spike 2 — Filesystem exposure (2026-10-30)

*Free Edition only.*

- **Run.** On the `appmux-probe` harness, as the app user (uid 1000), in two
  phases.
  - **Phase A, baseline.** [scripts/spike-02/fs_walk.py](scripts/spike-02/fs_walk.py)
    walked every readable tree, listed the 43 mounts and probed the candidate
    paths of the spec. It never read the content of a secret-named file
    (names, sizes and modes only; `runtime.cfg` key names were listed, no
    value). Raw: [evidence/spike-02-baseline.json](evidence/spike-02-baseline.json),
    [evidence/spike-02-platform-files.txt](evidence/spike-02-platform-files.txt)
    (names and sizes of the platform's files; JSON shapes by length only).
  - **Phase B, mount table.** [scripts/spike-02/sandbox_fs.py](scripts/spike-02/sandbox_fs.py)
    planted canaries (files with a marker in the app directory, home,
    `/dev/shm`, `/tmp`, `/var/tmp`, a sibling release and run directory, a
    sibling unix socket, a SysV shared-memory segment and message queue), then
    ran `unshare --user --map-root-user --mount --pid --fork [--ipc]`. Stage 1
    built a root on a fresh `tmpfs` from the table below, `pivot_root`ed into
    it, detached the old root, `chroot`ed into a subdirectory, dropped every
    capability and set `no_new_privs`, then executed a checker with the app
    environment's Python 3.11. The checker tried to open, stat, read, write and
    connect to every canary and platform file, listed everything visible and
    writable, probed the escapes, and tried a nested `unshare`. Raw:
    [evidence/spike-02-sandbox.json](evidence/spike-02-sandbox.json). The
    network namespace is not part of this spike (the earlier probe covers it).
  - Earlier runs refined the table (root and `/etc` were first left writable;
    the checker first inspected the wrong process); the table below is from the
    last run, which passed every row.
- **Result: pass.** The pass condition of the spec holds: a written mount table
  exists, and with it every canary and platform file fails with `ENOENT`
  (read, write and connect); the only readable trees are system libraries
  (`/usr`), the app environment (`/app/python/source_code/.venv`, read-only),
  the allow-listed `/etc` files, the release and the 64 MB private `tmpfs`
  mounts. The design is **corrected** (see Consequences): the overmount model
  left most of this exposed.

**Baseline: what an unsandboxed child (uid 1000) could reach.**

| Path | Without the sandbox | With the mount table |
|---|---|---|
| `/databricks/runtime-secrets/` (`runtime.cfg`, 712 B, mode 755; keys include `client_id` and `client_secret`; two `*-auth-secret-expiration` files) | readable | absent |
| `/databricks/dynamic-secrets/` (`shared/configs/deployment.json` with the source path, `deployment-unit-info.json` with pod and node names, `effective-scopes.json`, a fluent-bit config template, `reserved-platform/…`) | readable (read-only mounts) | absent |
| `/databricks/customer-logs/apps-runtime/databricksapps.log` (63 KB; directory mode 777, file 0600 owned by uid 1000) | **readable and writable**: a child can read every other child's platform log lines and forge or truncate them | absent |
| `/dev/termination-log` | mode 0666, writable | absent |
| `/app` (platform static files and **the app's own source tree**, which is appmux's once deployed) | readable; `/app/python` and `/app/python/source_code` writable | only the read-only `.venv` path |
| `/home/app`, `/dev/shm`, `/var/tmp`, `/tmp` | shared with appmux and every sibling, writable | one private `tmpfs` each |
| `/sys/class/dmi/id` (board, chassis and BIOS data), `/etc/hostname`, `/etc/hosts` | readable | absent |
| `/proc/self/cgroup`, `mountinfo`, `mounts`, `mountstats` | name the pod (`/kubepods/burstable/pod<id>`) and the host's overlay paths (`/platform_disk/overlaybd-snapshots/…`) | each overmounted by `/dev/null` |
| SysV shared memory and message queues | visible across processes (shared IPC namespace) | hidden **only with `unshare --ipc`** (without it both stayed `VISIBLE`) |
| Nested `unshare(CLONE_NEWUSER)` | **works even after the capability drop**; inside it a child can `unshare` a mount namespace and mount a `tmpfs` (re-mounting `/usr`, the environment or the release read-write failed with `EPERM`: their flags are locked) | `EPERM` once the process is `chroot`ed into a subdirectory of the new root |

**The mount table that passed.** Root: a fresh `tmpfs`, remounted read-only
after the build. Read-only means a bind followed by a remount that carries the
source mount's locked flags (`nosuid`, `nodev`, `noexec`, `relatime`); without
them the remount fails.

| In the child | Source | Mode |
|---|---|---|
| `/usr` (`/bin`, `/sbin`, `/lib`, `/lib64` are symlinks into it) | host `/usr` | read-only |
| `/app/python/source_code/.venv` | host app environment | read-only |
| `/srv/app` | the release snapshot | read-only |
| `/run/appmux` | the per-child run directory | read-write (the only host-backed writable mount) |
| `/tmp`, `/var/tmp`, `/dev/shm`, `/home/app` | fresh `tmpfs` (64, 16, 64, 64 MB) | read-write |
| `/etc/{ld.so.cache,ld.so.conf,ld.so.conf.d,ssl,ca-certificates,nsswitch.conf,passwd,group,localtime,os-release}` | host files | read-only, file by file |
| `/dev/{null,zero,full,random,urandom}` | host nodes | bind |
| `/proc` | fresh `tmpfs` plus `cpuinfo`, `meminfo` and `/proc/self` | read-only |
| `/proc/self/{cgroup,mountinfo,mounts,mountstats}` | `/dev/null` | overmount |

Not in the table, so absent: `/databricks`, `/sys`, `/mnt`, `/media`, `/opt`,
`/root`, `/boot`, `/etc/hostname`, `/etc/hosts`, `/etc/resolv.conf` and every
other `/etc` entry. The 43 mounts of the container are not visible
(`mountinfo` is empty).

**Observed inside the sandbox.**

- Canaries: 18 of 18 paths and sockets (app directory, home, `/dev/shm`,
  `/tmp`, `/var/tmp`, sibling release, sibling run file, sibling socket, and
  the ten platform files including `/proc/1/environ` and `/proc/1/cmdline`)
  answered `ENOENT` for read, write and connect.
- Writable places: exactly five — `/dev/shm`, `/home/app`, `/run/appmux`,
  `/tmp`, `/var/tmp`. Read-only checks all held: `EROFS` on the root, `/etc`,
  `/usr`, the environment and the release; `EACCES` on editing a file under
  `/usr`.
- Identity: uid 0 inside (mapped to 1000), `CapBnd` and `CapEff` zero,
  `NoNewPrivs` 1. `unshare(CLONE_NEWUSER)`, `mount`, `pivot_root`,
  `chroot(".")` and `open_by_handle_at` all failed with `EPERM`; `..` of the
  root lists the root. The nine setuid binaries of the image sit in the
  read-only `/usr` and cannot raise privilege under `no_new_privs`.
- `/proc` shows `cpuinfo`, `meminfo` and `self` only. The four `/proc/self`
  files that named the host come back empty (the checker confirmed `cgroup`
  and `mountinfo`); the 40 other readable files of `/proc/self` describe the
  child's own process and were not inspected one by one. The environment
  contained only `HOME` and `PATH`.
- The app environment's packages import inside the sandbox (`fastapi`,
  `uvicorn`, `streamlit`, `dash`, `gradio`, `pandas`, `requests`) and Node
  v22.23.4 starts.
- Secret-named files outside the `.venv`: **0**. The baseline found 278 in the
  whole container, 270 of them inside the app environment and 8 outside
  (`/etc/passwd` and the `pam.d` files, and the three `runtime-secrets` files).
  The scan is by name only and says nothing about content.
- Building the table took 3 ms inside the namespace (the launcher's own timing
  of the mount steps; the whole launch was not timed again).
- Inherited descriptors: 3, 4 and 5 (two pipes and a socket) were still open in
  the checker. Their owner was not identified; nothing could be done with them
  in this test.

**Consequences.**

- The overmount model of design §6.1 step 5 (hide `/proc`, `/tmp`, `/var/tmp`,
  `runtime-secrets`) is **replaced by an allow-list**: build a new root, never
  hide paths one by one. The baseline shows why — `dynamic-secrets`, the log,
  `termination-log`, appmux's own source, home and `/dev/shm` were all open.
- `unshare` needs `--ipc` ([design §6.1](../../../docs/product/v1-design.md#61-sandbox)
  command line), and stage 1 ends with `chroot` into a subdirectory of the new
  root, which is what closes nested user namespaces.
- `self_check()` gains one row for each path that mattered (the design §6.4
  table): the platform's `runtime.cfg`, `dynamic-secrets`, the customer log,
  `termination-log`, `/sys/class/dmi`, `/etc/hostname`, `/proc/self/cgroup`
  and `mountinfo`, the app directory, another sandbox's run directory, release
  and socket, SysV IPC, a nested user namespace, and open descriptors above 2.
- Handoffs: the run directory is host-backed, so a child can fill the disk
  there (spike 7); fluent-bit listens on `0.0.0.0:7270` in the container (its
  config template is in the platform files; spike 6 tests reachability from
  the net namespace); the cgroup and UTS namespaces were not tried.
- [ADR 0003](../../../docs/decisions/0003-isolate-children-with-unprivileged-namespaces-and-a-filtering-pump.md)
  keeps its decision and gains the mount table.
- **Not covered here.** One container, one image (Free Edition). The table was
  built by a prototype in Python; the plan decides the implementation language
  and whether `--map-root-user` or `--map-current-user` is used. File content
  was never inspected for secrets, only names. Descriptor inheritance was seen
  but not traced to its owner.
- **Created and removed.** Nothing in the workspace. On the app: canary files
  under `/tmp/spike2`, the app directory, `/home/app`, `/dev/shm` and
  `/var/tmp`, and a SysV segment and queue (key `0x53504b32`); the launcher
  removed each after the run. Local: none.

## Spike 18 — Package overlay on the real container (2026-10-30)

*Free Edition only.*

- **Run.** On the `appmux-probe` harness, in two sandboxes built by the spike 2
  launcher ([scripts/spike-18/sandbox_overlay.py](scripts/spike-18/sandbox_overlay.py),
  which reuses [sandbox_fs.py](scripts/spike-02/sandbox_fs.py)'s stage 1).
  - **Build sandbox.** `/usr` and the app environment read-only, the wheels
    read-only, an empty overlay directory read-write, no network. It ran
    `python -m pip install --no-index --no-deps --require-hashes --find-links
    /srv/wheels --target /srv/build -r lock.txt` with the environment's own
    Python 3.11 and pip 24.0, then three negative controls.
  - **Run sandbox.** The environment and the finished overlay read-only. It
    started `/srv/overlay/bin/uvicorn` with `PYTHONPATH=/srv/overlay` for the
    fixture [app.py](scripts/spike-18/app.py), which answers `/versions` with
    the version and file path of each package it imported, then tried to write
    to the environment and the overlay.
  - **Controls.** The same overlay script **without** `PYTHONPATH`, and the
    environment's own `uvicorn`.
  - The lock was completed over three runs (below). Raw:
    [evidence/spike-18.json](evidence/spike-18.json), the pins and their
    checks [evidence/spike-18-pins.json](evidence/spike-18-pins.json), the
    final lock [scripts/spike-18/lock.txt](scripts/spike-18/lock.txt).
- **Packages.** Exact names and versions checked on PyPI first; every wheel's
  sha256 equals the digest PyPI publishes; OSV.dev lists **no advisory at all**
  for any of the eight pins (so no `MAL-` entry); all were published at least
  24 days before the run (`opentelemetry-api` 1.45.2, released the day before,
  was passed over for 1.45.1). All eight are pure-Python wheels, which run no
  code at install time. The wheels were downloaded on a local machine and
  uploaded to the app; the fetch path is not part of this spike.
- **The environment.** The Environment tab is a browser view and was **not**
  opened; the same environment was enumerated from inside the app with
  `importlib.metadata`: 132 distributions
  ([evidence/spike-18-environment-packages.txt](evidence/spike-18-environment-packages.txt)).
  The platform's own pins are the 17 top-level lines of
  `/databricks/preinstalled-uv-env/pyproject.toml` (among them `fastapi==0.115.0`,
  `uvicorn[standard]==0.30.6`, `streamlit==1.38.0`, `gradio==4.44.0`,
  `dash==2.18.1`, `flask==3.0.3`, `gunicorn==23.0.0`, `databricks-sdk==0.33.0`);
  the rest are their transitive dependencies. The environment is a virtual
  environment at `/app/python/source_code/.venv` (`include-system-site-packages
  = false`, base interpreter `/usr/bin/python3.11`, 3.11.17) that carries
  `pip` 24.0; `uv` is at `/usr/local/bin/uv`. Outside the sandbox the app user
  owns and can write the whole environment.
- **Result: pass.** The spec's condition holds in the third run, and in the
  first two: the app serves, the overlay's version is the one imported, and
  every write returns `EROFS`.

| Question | Observed |
|---|---|
| Does the install work? | **Yes**, offline, as an unprivileged user in the sandbox: 8 wheels, 885 files, 6.8 MB, in about 1 s (5 wheels took 0.7 s). pip compiled 368 `.pyc` files at install, so a read-only overlay needs no write at import time. |
| Does a wrong or missing hash stop it? | **Yes**, and nothing is installed. A wrong hash: `THESE PACKAGES DO NOT MATCH THE HASHES` (expected and got printed). A requirement without a hash: `Hashes are required in --require-hashes mode`. An unpinned one (`>=`): `all requirements must have their versions pinned with ==`. Exit code 1 each, no target directory created. |
| Does `PYTHONPATH` ahead of the environment let the overlay win? | **Yes.** With `PYTHONPATH=/srv/overlay` the app imported `fastapi` 0.142.2, `starlette` 1.7.0, `uvicorn` 0.32.1 and `click` 8.1.8 from `/srv/overlay`, and `humanize` 4.11.0, which the environment lacks; `pydantic` 2.13.4 and `h11` 0.16.0 came from the environment. The environment control imported `fastapi` 0.115.0, `starlette` 0.38.6, `uvicorn` 0.30.6, `click` 8.4.1 and no `humanize`. |
| Do entry-point scripts run from the overlay's `bin`? | **Yes.** `bin/uvicorn` and `bin/fastapi` are mode 755 with the shebang `#!/app/python/source_code/.venv/bin/python` (the interpreter that ran pip), so they run wherever the environment is mounted at that path. `uvicorn --version` from the overlay printed 0.32.1. First response from the app: 0.64 s (a 0.3 s polling step). |
| Does the script alone select the overlay? | **No.** The overlay's `bin/uvicorn` started **without** `PYTHONPATH` imported the environment's `uvicorn` 0.30.6 and `fastapi` 0.115.0: `PYTHONPATH` is what makes the overlay win, not the script's location. Started by absolute path here; lookup through `PATH=O/bin:$PATH` was not tested separately. |
| Does the read-only remount hold? | **Yes.** A new file in the environment's `site-packages` and `bin`, in the overlay's root, a package directory and `bin`, and an append to an overlay module: all `EROFS`. |

**The lock took three runs, and the third is the finding.** The first lock
listed the five packages the fixture asked for (`fastapi`, `starlette`,
`uvicorn`, `click`, `humanize`). The app served, but `pip check` with the
overlay on the path failed: `fastapi 0.142.2 has requirement
opentelemetry-api>=1.44.0, but you have opentelemetry-api 1.42.1` (the
environment's copy). Adding `opentelemetry-api` 1.45.1 fixed that and broke
two **environment** packages: `opentelemetry-sdk 1.42.1` and
`opentelemetry-semantic-conventions 0.63b1` each require
`opentelemetry-api==1.42.1`. Adding `opentelemetry-sdk` 1.45.1 and
`opentelemetry-semantic-conventions` 0.66b1 made `pip check` report **No
broken requirements found**, and the environment alone was clean throughout.
In runs 1 and 2 the fixture still answered normally: the conflict was
latent, not a crash.

- **Consequences.**
  - [ADR 0007](../../../docs/decisions/0007-resolve-packages-environment-first-with-a-wheel-overlay.md)
    holds: no spike reversed it. Its status stays `proposed` until spikes 13
    and 14 are recorded.
  - Overriding an environment package can break **other environment
    packages** that pin it with `==`, so the overlay is sound only when its
    lock covers the whole closure. `pip check` with the overlay on the path
    found each gap and named the exact wheel to add. The design runs it as a
    warning that never fails the build ([design §5.3](../../../docs/product/v1-design.md#53-build-and-package-sources));
    the plan decides whether a conflict that touches an environment package
    should block the Update instead. The Update's message should name the
    conflicting requirement, as `pip check` did.
  - `PYTHONPATH` must be set for the overlay to win; `PATH` alone does not
    select it. Design §5.3 says so.
  - The build needs no network in `wheelhouse` mode, and the environment's
    own pip is enough: no extra tool in the sandbox.
- **Not covered here.** Sources of the wheels (workspace folders, the content
  store) and their transfer; `index` mode and source distributions; a package
  with compiled extensions or a platform-specific wheel (all eight are pure
  Python); a Streamlit, Gradio or Dash app on an overlay; `PATH` lookup;
  concurrent builds; the Environment tab itself; what an environment change at
  redeploy does to a built overlay (design §5.3 "the environment can change").
  Only the build and run sandboxes of spike 2 were used, without a network
  namespace.
- **Created and removed.** Nothing in the workspace. On the app:
  `/tmp/spike18` (wheels, overlay, fixture, run directory) and the two sandbox
  mounts, removed after the last run (the folder is gone, checked). Local: a
  throwaway virtual environment and the downloaded wheels in the scratch area,
  outside the repository.

## Spike 13 — Parent-app wheel bootstrap (2026-10-30)

*Free Edition only.*

- **Run.** Four deployments to a throwaway app `appmux-spike-13`
  ([scripts/spike-13/](scripts/spike-13/), logs in
  [evidence/spike-13-deploy-logs.txt](evidence/spike-13-deploy-logs.txt)).
  - **Marker wheel.** A one-module wheel built by hand with the standard
    library ([make_marker_wheel.py](scripts/spike-13/make_marker_wheel.py)). Its
    name is not on PyPI (HTTP 404 on the day of the run), so installing it proves
    the wheel folder was read. It requires `humanize==4.11.0`, whose wheel (the
    one vetted in spike 18) sits in the same folder.
  - **A.** `requirements.txt` = `--no-index`, `--find-links ./wheels`, the
    marker; the wheels in `wheels/` in the deployment folder.
  - **A1 (control).** The same wheels in `wheels/`, a new marker name, and
    `requirements.txt` holding **only** the requirement.
  - **B (bootstrap).** No `requirements.txt`. `app.yaml` starts
    [start.py](scripts/spike-13/v13b/start.py), which uses only the standard
    library: it gets a token with the app's own client id and secret (read from
    the environment, never printed), lists a workspace folder, downloads its
    wheels to `/tmp`, runs `pip install --no-index --find-links … --target
    /tmp/appmux-deps`, and starts the app with `PYTHONPATH=/tmp/appmux-deps`.
  - A second deployment, **A0**, was meant as the control but reused the app's
    environment and proves nothing about the lines (see the persistence row); the
    **A1** control replaced it.
- **Result: pass.** Both ways work. The spec's condition asks for either.

| Question | Observed |
|---|---|
| Does the platform honour `--no-index` and `--find-links ./wheels`? | **Yes.** Build log: `Looking in links: /app/python/source_code/./wheels`, `Processing ./wheels/appmux_spike13_marker_q7k2x-0.0.1-py3-none-any.whl (from -r requirements.txt (line 3))`, then `Successfully installed appmux-spike13-marker-q7k2x-0.0.1 humanize-4.11.0`. The platform runs `pip` 24.0 (`python3_apps -m pip`) on `-r requirements.txt`, so the file's option lines reach pip. There is no `Looking in indexes` line. The whole deployment took 4.4 s. |
| Where does `./wheels` resolve, and where do the packages land? | Relative to the deployment folder, which is `/app/python/source_code`. The packages went into the app's environment (`/app/python/source_code/.venv/lib/python3.11/site-packages`) and the app imported both from there. |
| Is a wheel in the folder enough without the two lines? | **No.** Control A1: the wheel in `wheels/`, `requirements.txt` naming only the package: `ERROR: Could not find a version that satisfies the requirement appmux-spike13-marker-c4v8n==0.0.1 (from versions: none)`, deployment `FAILED` with `Error installing packages`. |
| What happens to the running app when the install fails? | The failed deployment **becomes the active one** and the app status is `UNAVAILABLE` (`App deployment failed`); the earlier working deployment is not kept serving. |
| Does the environment persist across deployments? | **Yes.** A0, the next deployment of the same app, logged `Requirement already satisfied: appmux-spike13-marker-q7k2x==0.0.1 in ./.venv/lib/python3.11/site-packages` although its `requirements.txt` no longer carried the lines. Packages installed by an earlier deployment stay; removing a line does not remove a package. |
| Does the bootstrap work? | **Yes.** The platform logged `No dependencies file found. Skipping installation.`; `start.py` listed 2 wheels (object type `FILE`, as `workspace import-dir` stores them), downloaded them, `pip install --no-index --find-links /tmp/appmux-wheels --target /tmp/appmux-deps` returned 0, and the app imported the marker and `humanize` from `/tmp/appmux-deps` even though the environment also held `humanize` 4.11.0. From the start of `start.py` to the app serving: about 2 s (12:14:26 to 12:14:28). |
| Can the app's service principal read the folder by default? | **No.** Before any grant the token request succeeded but the listing returned **zero objects with no error** (`wheel_count: 0`); pip then failed with `No matching distribution` and the app crashed (`app crashed unexpectedly`). After the folder's owner ran `databricks permissions update directories <id>` with `CAN_READ` for the app's service principal, the listing returned both wheels. |

- **Consequences.**
  - [Design §13.1](../../../docs/product/v1-design.md#131-what-the-operator-sets-up)
    step 5 keeps the vendored `requirements.txt` as the way: ship the wheels
    in the deployment folder and start `requirements.txt` with the two lines.
    The text no longer says "whether the platform honours those lines is a
    spike". The bootstrap, measured to work, shrinks to the fallback if the
    platform stops honouring the lines; it needs the folder grant, and a
    bootstrap must treat an empty listing as an error, because a missing grant
    looks like an empty folder.
  - With the bootstrap, appmux's packages sit in `/tmp/appmux-deps`, **outside**
    the environment, so the "base layer of every child's packages"
    ([design §5.3](../../../docs/product/v1-design.md#53-build-and-package-sources))
    would need that directory as one more read-only layer on each child's
    `PYTHONPATH`. With the `requirements.txt` way they are in the environment
    and the base layer is automatic. The plan decides if the bootstrap ships.
  - An install that fails when appmux itself is deployed takes the slot
    down until the operator redeploys a good version; this is the platform's
    behaviour and outside appmux's zero-downtime promise (which concerns
    children). The operator reads the build log.
  - The environment keeps packages across deployments, which the platform
    reference now says.
  - [ADR 0007](../../../docs/decisions/0007-resolve-packages-environment-first-with-a-wheel-overlay.md)
    holds; status stays `proposed` until spike 14.
- **Not covered here.** A workspace whose index is unreachable (the reason the
  lines exist; the paid workspace is out of scope): the lines make pip skip the
  index, so they are expected to work there, but this was run only where PyPI is
  reachable, and a workspace-level package configuration on a paid workspace
  could differ. `--require-hashes` or `--hash` lines in `requirements.txt`;
  wheels with compiled extensions; the size of a wheel folder a deployment can
  carry; whether the environment survives an app stop and start; what a revoked
  folder grant does to a running bootstrap; wheels fetched from a workspace
  folder with the visitor's token rather than the app's own.
- **Created and removed.** App `appmux-spike-13` (deleted; `databricks apps
  list` shows only `appmux-probe` afterwards); workspace folders `spike13-a`,
  `spike13-a0`, `spike13-a1`, `spike13-b` and `appmux-spike-13-wheels`
  (deleted, which removed the service principal's `CAN_READ` grant with them).
  Local: the built wheels in the scratch area, outside the repository.

## Spike 14 — Node offline source and wheel coverage (2026-10-30)

*Free Edition only* for the Node run; the Python coverage is computed locally
against the environment's package list from spike 18.

- **Run.** Scripts in [scripts/spike-14/](scripts/spike-14/), raw results in
  [evidence/spike-14.json](evidence/spike-14.json).
  - **Python locks.** For each fixture (FastAPI, Streamlit, Dash, Gradio; one
    top-level requirement each, FastAPI with `uvicorn` and `websockets`),
    `uv pip compile --generate-hashes --no-build --python-version 3.11
    --exclude-newer 2026-10-16T00:00:00Z` (the 14-day cooldown). The `*.lock.txt`
    files are the result.
  - **Coverage, two ways.** (1) *Literal subtraction:* every lock entry compared
    with the 132 packages of
    [spike-18-environment-packages.txt](evidence/spike-18-environment-packages.txt)
    ([coverage.py](scripts/spike-14/coverage.py)). (2) *Environment-first:* a
    metadata-only copy of the environment
    ([stub_env.py](scripts/spike-14/stub_env.py)) and `uv pip install --dry-run`
    of the declaration, once unpinned and once with the top-level pinned to the
    lock's version; then the plan applied to the copy and `uv pip check` run
    ([plan_and_check.py](scripts/spike-14/plan_and_check.py)).
  - **Wheel availability.** `pip download --only-binary=:all: --no-deps
    --require-hashes --python-version 3.11 --platform manylinux2014_x86_64
    --implementation cp --abi cp311` for every entry of the literal-missing set
    (the superset of any plan). Then OSV.dev on the 61 distinct pins.
  - **Node.** Fixture Express 5.3.0 and `ws` 8.22.0 (66 lock entries). The lock
    was regenerated with `npm install --package-lock-only
    --before=2026-10-16T00:00:00Z`, because the spike 5 lock held two packages
    about a day old. The cache was built locally with npm 11.6.2 (`npm ci
    --cache C --ignore-scripts`: 134 files, 2.2 MB, 784 KB as a tarball) and
    copied to `/tmp/spike14` on the app. There, in a build sandbox with its own
    network namespace ([sandbox_npm.py](scripts/spike-14/sandbox_npm.py), the
    spike 2 mount table, DNS failing), `npm ci --offline --ignore-scripts
    --no-audit --no-fund --cache /srv/npm-cache`, with the cache mounted
    **read-only**. A run sandbox then started the app with `/srv/proj` read-only.
- **Result: pass.** The spec asks for a table and a yes or no.
  - **Node offline: yes.**
  - **Python:** the environment covers every requirement of all four fixtures
    when the declaration is unpinned, and a pinned declaration needs 2 to 16
    wheels, all of which exist for this platform. No package lacks a wheel.

| Fixture | Lock entries | Same version in the environment | Other version in the environment | Absent from the environment | Wheels if the whole lock is pinned (compiled, MB) | Wheels if only the top level is pinned (compiled, MB) | `uv pip check` after the second plan |
|---|---|---|---|---|---|---|---|
| FastAPI | 15 | 1 | 14 | 0 | 14 (2, 3.6) | **5** (1, 0.6) | **fails**: `opentelemetry-sdk` and `opentelemetry-semantic-conventions` need `opentelemetry-api==1.42.1`, `1.45.1` installed; `gradio-client` needs `websockets<13`, `17.2` installed |
| Streamlit | 36 | 10 | 26 | 0 | 26 (10, 105.9) | **2** (0, 10.4) | **fails**: `fastapi` needs `starlette<0.39.0`, `1.8.0` installed |
| Dash | 28 | 6 | 20 | 2 | 22 (3, 23.9) | **3** (0, 9.0) | clean (`comm` and `janus` are new) |
| Gradio | 53 | 12 | 34 | 7 | 41 (8, 82.3) | **16** (2, 38.6; 9 replaced, 7 new) | **fails**: the same two `opentelemetry` packages |

  *Unpinned declarations* (`fastapi`, `streamlit`, `dash`, `gradio` alone):
  `uv pip install --dry-run` against the copy printed `Would make no changes` in
  all four, so the environment covers 4 of 4 fixtures with the versions it has
  (fastapi 0.115.0, streamlit 1.38.0, dash 2.18.1, gradio 4.44.0).

| Question | Observed |
|---|---|
| Does any package have no `manylinux2014` Python 3.11 wheel? | **None.** `pip download --only-binary=:all:` succeeded for every entry of the four literal-missing sets (14, 26, 22 and 41 wheels), including the compiled ones (for example `pydantic_core`, `websockets`, `charset_normalizer`, `markupsafe`, `brotli`, `hf_xet`). |
| What do the latest pins of the fixtures carry in OSV.dev? | Of 61 distinct pins, 6 have advisories (`brotli`, `certifi`, `filelock`, `pillow`, `pyarrow`, `setuptools`) and **none has a `MAL-` entry**. The ids are in the evidence file; the fixtures are test inputs and none is shipped. |
| Does the environment-first plan replace what other environment packages depend on? | **Yes, in 3 of 4 fixtures.** Pinning a top-level package to its latest version replaces environment packages (`starlette`, `opentelemetry-api`, `websockets`), and the environment's own reverse-dependents (`fastapi`, `opentelemetry-sdk`, `gradio-client`) then fail `pip check`. This is the spike 18 finding in a second place: the overlay lock must be compiled for the closure including the environment's reverse-dependents. |
| Node: does `npm ci --offline` work from a cache with no index? | **Yes.** In a build sandbox with its own network namespace (name resolution fails, checked), npm 10.9.9 on Node 22.23.4 installed **66 packages in 0.65 s** from the read-only cache (605 files, 2.4 MB). The only complaint was that it could not write its `_logs` folder. The cache had been made by npm 11.6.2 and the older npm read it. |
| Node: does an incomplete or altered cache fail closed? | **Yes.** An empty cache gave `ENOTCACHED`. A lock whose `express` integrity was altered also gave `ENOTCACHED`, not `EINTEGRITY`: npm's cache is keyed by the integrity string, so a wrong value finds nothing. Either way, nothing was installed. |
| Node: does the installed project run with the tree read-only? | **Yes.** In the run sandbox with `/srv/proj` read-only: `GET /api/ping` returned `{"ok":true}`, `GET /` returned 200, a WebSocket echoed `echo:hi`, and all 5 write attempts into the tree returned `EROFS`. |
| Node: vetting of the lock | OSV.dev: one advisory, `body-parser@2.3.0` (GHSA-27r3-jxmh-p828, a denial of service; the fixture does not exercise it), no `MAL-` entry. No package in the lock has an install script (`--ignore-scripts` is on regardless). |

- **Consequences.**
  - **The spec's fallback does not fire.** Node children are not out of scope
    for the wheelhouse modes. [Design §5.3](../../../docs/product/v1-design.md#53-build-and-package-sources)
    changes from "not designed in v1" to a measured recipe: a lock file plus an
    npm cache, installed with `npm ci --offline --ignore-scripts` from a
    read-only cache. How the cache reaches the builder (a folder like the wheel
    sources, or tarballs in the app's source) and whether v1 ships it is the
    plan's decision, so the non-goal in the design and the spec is reworded, not
    removed.
  - **Declare the top level unpinned where possible.** A requirement the
    environment satisfies needs no wheel, which the design already says. The
    measurement adds how often that applies: an unpinned `fastapi`, `streamlit`,
    `dash` or `gradio` is fully covered, and a pin to the newest version costs
    2 to 16 wheels (0.6 to 38.6 MB).
  - **Pinning to the newest version breaks the environment's other packages
    unless the lock covers them.** The design already runs `pip check` after the
    install and shows its output as a warning; it named every gap in this spike.
    Three of four fixtures now show the conflict, which is evidence for the
    plan's open question whether a conflict that touches an environment package
    should fail the Update instead of warning
    ([design §5.3](../../../docs/product/v1-design.md#53-build-and-package-sources)).
  - **Wheel gaps are not a reason for an index mode** for these frameworks: every
    compiled dependency had a `manylinux2014` wheel for Python 3.11. The
    `no wheel for 'foo'` failure stays the answer for the rare package without
    one.
  - ADR 0007 becomes `accepted`: spikes 18, 13 and 14 passed and none reversed
    it.
- **Not covered here.** Other frameworks and packages than the four and Express
  (the table is four samples, not a census). Compiled-from-source Python
  packages (`--only-binary` excludes them by design). Native Node addons and
  packages with install scripts (`--ignore-scripts` was on, so an addon that
  needs `node-gyp` or a downloaded binary would not work offline). A cache
  delivered through a workspace folder or any other path; the cache was copied
  to the app's `/tmp` by the harness. Cache and lock compatibility across npm
  major versions beyond the 11-to-10 pair run here. The stub environment models
  the environment's metadata (names, versions, `Requires-Dist`), not its files,
  so `uv pip check` there matches the real tool's view but not an import-time
  failure. The coverage is for a top-level requirement alone; a real app's
  direct dependencies vary.
- **Created and removed.** Nothing in the workspace (no app, secret scope,
  group or folder was created for this spike). On the app: `/tmp/spike14` (npm
  cache, project, new-root mounts), removed after the last run (the folder is
  gone, checked); `/tmp/spike/sandbox_npm.py` stays with the harness until the
  final cleanup. Local: virtual environments, the stub environments,
  the downloaded wheels and the npm cache in the scratch area, outside the
  repository.

## Spike 1 — OBO login to Lakebase (2026-10-30)

*Free Edition only.* Scripts: [scripts/spike-01/](scripts/spike-01/) (`setup.sql`,
`isolate.sql`, `grant_membership.sql`, `revoke_membership.sql`, `obo_matrix.py`,
`obo_soak.py`, `owner_sql.py`), raw results in
[evidence/spike-01.json](evidence/spike-01.json).

- **Run.**
  - **Setup.** On the Lakebase project `appmux-spike-1` the owner created the
    group role `appmux_platform_writers` and a role `appmux_spike1_owner` that owns
    two tables, `spike1_apps` (INSERT granted to the group role) and `spike1_other`
    (no grant). Lakebase gave the owner's own role inherit-membership of both new
    roles (and of `databricks_superuser`), so a direct `INSERT` worked; both new
    roles were revoked from the owner's role, after which the same `INSERT` was
    refused (`has_table_privilege` false on both tables). That is the starting
    state.
  - **Login as the user.** `appmux-probe` runs `obo_matrix.py` with the caller's
    token from `X-Forwarded-Access-Token` as the Postgres password (psycopg 3.3.6
    in a temporary folder) and the owner's role as the user name, then tries an
    `INSERT`, an `UPDATE` and a `DELETE` on `spike1_apps` and an `INSERT` on
    `spike1_other`. Phases: A before the group role is granted; B after
    `GRANT appmux_platform_writers TO <owner role>`; C with `user_api_scopes` set to
    `[]` and the app stopped and started, so the token carries no `postgres`
    scope; C3 login controls; D after the group role is revoked again.
  - **Soak.** `obo_soak.py` opened one connection with the token, ran `select 1`
    every minute for 65 minutes, and tried a fresh login with the same token
    2 minutes after the token's expiry.
- **Result: fail as written.** The pass condition has three clauses; two held and
  one did not.

| Clause | Observed |
|---|---|
| Login succeeds as the user's role with the `postgres` scope | **Yes.** `current_user` is the owner's role, `rolsuper` false. The token's `scope` claim was `iam.access-control:read iam.current-user:read postgres`. |
| Login fails without the scope | **No: it succeeded.** With `user_api_scopes` cleared and the app restarted, the claim was `iam.access-control:read iam.current-user:read` and the login and the `INSERT` still worked. The token is validated: a random string and a token with one altered signature character were both refused (`not a valid JWT encoding`; `password authentication failed`). Only validity is checked, not the scope. |
| The insert succeeds only through the granted group role | **Yes.** A: all four statements refused (42501). B and C: only the `INSERT` on `spike1_apps` worked; `UPDATE`, `DELETE` and the `INSERT` on `spike1_other` were refused. D, after the revoke: all four refused again. |

| Soak question | Observed |
|---|---|
| Is the token checked after the login? | **No.** The token lived 3047 s and expired 2886 s into the run. The open connection answered 48 queries before and **17 after the expiry, the last 1014 s past it**, none failed. |
| Can an expired token log in again? | **No.** The fresh login at t+3060 s (174 s after the expiry) was refused with an authentication error (the logged message was cut at 100 characters and starts `ERROR:  passwo`). |

- **Consequences.**
  - **The write path works as designed.** A platform admin's own OBO token logs in
    to Lakebase as their own role, and only membership of `appmux_platform_writers`
    lets a write through. The fallback of assumption 1 (the SP writes everything)
    does not fire.
  - **No OAuth scope is needed for the login.** The operator steps and the
    scope list of [design §13.1](../../../docs/product/v1-design.md#131-what-the-operator-sets-up)
    drop `postgres`. The edge needs the visitor's current token for each admin
    write, since a new login with an expired token is refused; a connection kept
    open does not need a new one.
  - **A scope does not limit what a token can do in Lakebase.** The design
    leans on "an OBO child acts as the visitor, within those scopes" and on a
    minimal scope set as the mitigation for a stolen visitor token ([design §12](../../../docs/product/v1-design.md#12-threat-model)).
    For Lakebase that does not hold: any approved OBO child receives a token that
    logs in to the registry as the visitor. When the visitor is a platform admin
    (a member of the writers role), the child can write platform data
    (`apps` rows, the delegation flag, the slug, the budget, egress rules, settings)
    with that person's identity for the token's lifetime, and a connection it
    opened keeps working after the expiry. This is a new residual, not covered
    by the scope set. It goes to the owner in the spec's Questions, with the
    recommendation to accept it and state it (a platform admin should open an
    OBO-delegated app *without token* unless they accept the exposure); the
    alternative, the SP writing everything, gives up the separation between a
    person and the SP that this design exists for.
  - [ADR 0004](../../../docs/decisions/0004-keep-decisions-in-a-nine-table-lakebase-registry-with-split-writers.md)
    was `proposed` at this point: spike 9 passed and spike 1 did not meet its pass
    condition as written. Design §4.2, §12, §13.1, the spec and the brief changed
    in the same change.
  - **Owner decision, 2026-10-30.** Question 5 was answered (a): accept the
    residual and state it, with *without token* kept as a platform admin's default
    view mode. ADR 0004 is `accepted`; the gap is logged as TD-002 in
    [tech-debt](../../../docs/tech-debt.md#td-002-an-obo-childs-token-reaches-the-registry).
- **Also seen.** A change of `user_api_scopes` reaches the tokens only after the app is
  stopped and started (the claim still read `... postgres` right after the scopes
  were cleared). Stop and start empties `/tmp` on the app; a redeploy keeps it, so
  [design §3.3](../../../docs/product/v1-design.md#33-where-state-lives) and the
  lazy-install and shutdown text now say the cache survives a redeploy but not a
  stop and start, which on Free Edition includes the daily auto-stop. Both facts
  are in the [platform reference](../../../docs/reference/databricks-apps-platform.md)
  and the [Free Edition reference](../../../docs/reference/databricks-free-edition.md).
- **Not covered here.** A token from a browser session: these runs used the token
  the proxy forwarded for a request carrying the CLI's bearer, so its `client_id`
  claim was `databricks-cli`; a browser visit gets a token for the app's own OAuth
  client. A paid workspace or a provisioned (non-autoscaling) Lakebase. A role that
  is a member of `databricks_superuser` (it bypasses the grants; the owner's
  membership of it also disappeared from the second membership listing and the
  cause was not investigated). Roles for a Databricks group (only the owner's own
  role was used). The full text of the expired-token refusal.
- **Created and removed.** The Lakebase project `appmux-spike-1` with the two
  roles, two tables and (spike 9) the scratch database; the `user_api_scopes` of
  `appmux-probe` was set to `["postgres"]` and then `[]` (it was unset at the start);
  `/tmp/spike1` on the app. All are removed or restored at the end of the run.

## Spike 9 — Cascade privileges on Lakebase and the slirp4netns licence (2026-10-30)

*Free Edition only* (one Lakebase project, PostgreSQL 17.11). Scripts in
[scripts/spike-09/](scripts/spike-09/), raw results in
[evidence/spike-09.json](evidence/spike-09.json).

- **Run.**
  - **Load as written.** `extract_ddl.py` pulled every `sql` fence of
    [the data model](../../../docs/product/v1-data-model.md) section 4 and the
    grants of section 6, untouched. They ran in a scratch database
    (`appmux_spike9`) with the tables owned by a role of their own
    (`appmux_spike9_owner`), a stand-in `appmux_sp` (`NOLOGIN`) and the group
    role from spike 1.
  - **The platform admin.** The owner's own database role, stripped of the
    table-owner role and of `databricks_superuser` (spike 1) and made a member of
    `appmux_platform_writers` only. It inherits the group's rights without
    `SET ROLE`, as an OBO login would. The check before the tests:
    `has_table_privilege` for the group role and for `appmux_sp` on every table
    and each of `INSERT`, `UPDATE`, `DELETE` equals the table in
    [data model §6](../../../docs/product/v1-data-model.md#6-who-writes-what).
  - **Tests** ([cascade_test.py](scripts/spike-09/cascade_test.py)), each in its
    own transaction: four apps seeded with a row in every child table (the SP
    stand-in writes the children via `SET LOCAL ROLE`); direct writes the group
    role must be refused; `SET LOCAL ROLE`; a rename; a delete; a delete and a
    rename in one transaction; controls; a variant with the composite keys not
    deferrable.
  - **Licence.** The `v1.3.6` source archive of `slirp4netns` from its GitHub
    repository (the version the static binary of the earlier probes reports),
    `COPYING` and the `SPDX-License-Identifier` headers read. The `v1.3.3`
    archive has the same `COPYING` and the same headers.
- **Result: pass.** 28 checks, 0 failed, after one correction to the DDL.

| Question | Observed |
|---|---|
| Does the data-model DDL load as written? | **No: one error.** `CREATE TABLE app_config` failed with `relation "releases" does not exist` (SQLSTATE 42P01): its three composite foreign keys point at `releases` and `app_resources`, which the file creates later. The other eight tables loaded. The text beside the DDL already said such keys are added with `ALTER TABLE` afterwards, but the block did not do it. Fixed in the data model: the keys moved to one `ALTER TABLE` at the end of §4.7, and the same extraction then loaded all nine tables with no error. |
| Do the grants of §6 run as written? | **Yes.** The one error was `CREATE ROLE appmux_platform_writers` (42710, already exists) because spike 1 had created it. Both roles' `INSERT`, `UPDATE` and `DELETE` rights on every table equal the table in §6. |
| Does a platform admin's `DELETE FROM apps` cascade into tables the role cannot write? | **Yes.** The role holds no `DELETE` on `app_config`, `app_grants`, `releases`, `app_resources` or `permission_requests`; deleting an app that had 1, 1, 1, 2 and 1 rows in them left 0 in each. `audit_log` and `metrics` kept the app's rows (no foreign key), as designed. PostgreSQL runs the cascaded writes with the rights of the table owner, so no extra grant is needed. |
| Does `UPDATE apps SET slug` cascade through the deferrable composite keys of `app_config`? | **Yes.** After the rename every child row (including `app_config.target_release_no` and both `own_sp_*_key` values pointing at `releases` and `app_resources`) carried the new slug, none the old, and the keys resolved at commit. |
| In one transaction? | **Yes.** `DELETE` of one app and a rename of another in the same transaction committed, with both results intact. |
| Is a direct write to `app_config` refused? | **Yes**, `UPDATE` and `DELETE` both with 42501 `permission denied for table app_config`. The group role may `INSERT` there (the first row at registration, §6) and that was allowed. Also refused for the group role, all 42501: `INSERT` into `releases` and `metrics`, `UPDATE` on `app_grants` and `audit_log`, `DELETE` on `permission_requests` and `audit_log`. Allowed: updating `permission_requests` to answer a request. |
| Does `SET LOCAL ROLE` work? | **Yes.** To the group role (the identity holds the `SET` option): allowed, and `current_user` returned to the session user at `COMMIT`. To `appmux_sp` and to the table owner (no membership): refused, 42501 `permission denied to set role`. |
| Are the composite keys really enforced, and when? | **Yes, at `COMMIT`.** As the SP stand-in, `UPDATE app_config SET target_release_no = 99` and `SET own_sp_client_id_key = 'nope'` both ran without error and the transaction failed at `COMMIT` with 23503; a rename onto a taken slug failed with 23505 and left the app untouched. |
| Is the deferral needed? | **Not shown.** With the three keys changed to not deferrable, the same rename of an app with a target release and both own-SP keys still succeeded. One fixture; the order PostgreSQL fires the cascade triggers in decides this case, so the data model keeps `DEFERRABLE INITIALLY DEFERRED` as insurance. |
| What does Lakebase add to the new tables? | `databricks_gateway` and `databricks_reader_<n>` can `SELECT` every table; `databricks_superuser` holds every privilege with grant option. The split writers therefore bind the two appmux roles and not a member of `databricks_superuser`. |
| Licence of `slirp4netns`? | **`GPL-2.0-or-later`.** `COPYING` is the GNU GPL version 2 text, the README's licence section says `GPL-2.0-or-later`, and the source headers carry `GPL-2.0-or-later` (ten files), `LGPL-2.1+` (`seccomparch.h`) and `MIT` (the vendored `parson`). The release binary is static; the build recipe links `libslirp` v4.9.5, glib, libcap and libseccomp, whose licences were not read. |

*(Correction, 2026-10-30, found while writing `scripts/fetch-slirp4netns.sh`: the row above lists libcap among the linked libraries, and libcap is not linked; the build only includes its header. The static binary embeds `slirp4netns`, its vendored `parson` (MIT), `libslirp` v4.9.5 (BSD-3-Clause and MIT), glibc 2.35 linked statically (LGPL-2.1-or-later), glib (LGPL-2.1-or-later) and libseccomp 2.5.3 (LGPL-2.1 only). See [ADR 0015](../../../docs/decisions/0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md).)*

- **Consequences.**
  - **The fallbacks do not fire.** The platform role needs no extra grants and
    the child tables are not updated explicitly. A rename stays in v1's
    `settings` request (A5).
  - **The data model is corrected** (§4.4, §4.7, §6 and §9 point 5); nothing
    else in the design changed.
  - **[ADR 0004](../../../docs/decisions/0004-keep-decisions-in-a-nine-table-lakebase-registry-with-split-writers.md)**
    gains the spike 9 result. It was `proposed` until the spike 1 entry was
    written and became `accepted` with the owner's answer on 2026-10-30. **[ADR 0011](../../../docs/decisions/0011-gate-credentials-and-platform-fields-behind-a-request-catalogue.md)**
    no longer depends on spike 9 for the rename. **[ADR 0013](../../../docs/decisions/0013-license-appmux-under-apache-2-0.md)**
    records the licence.
  - **Licence reading.** The program is GPL-2.0-or-later and appmux starts it as
    a separate process and fetches it from the upstream release at install time.
    On that design appmux neither links nor redistributes it, which is what ADR
    0013 assumed; no `NOTICE` file is added. If the plan ever bundles the binary
    (for example inside the deploy bundle), the GPL source-offer duty applies to
    whoever distributes it and a `NOTICE` becomes necessary. This is a
    reading of the licence text, not legal advice.
  - **Operators need to know** that the Lakebase project owner and any member of
    `databricks_superuser` can write every registry table whatever the split.
- **Not covered here.** A platform admin who is not the project owner, logging
  in with an OBO token (spike 1 covers the login; the Postgres role is the
  same). Volume and lock behaviour of a cascade (the fixture is four apps with a
  row per table). A rename racing with the SP's own writes. Whether a `REVOKE`
  of the default `databricks_superuser` grants on the registry tables sticks, or
  survives a new table. The licences of the libraries inside the static binary.
  The owner's role kept administrative attributes (`CREATEROLE`, `CREATEDB`,
  `BYPASSRLS`) and the admin-option rows for roles it created; the tests did not
  use them, and `has_table_privilege` and `pg_has_role` showed no inherited
  rights beyond the group role before each phase.
- **Created and removed.** In the Lakebase project `appmux-spike-1`: database
  `appmux_spike9` (recreated for the final run), roles `appmux_spike9_owner` and
  `appmux_sp` (`NOLOGIN`), and the existing `appmux_platform_writers`. They go
  with the project at the final cleanup. Nothing was created on the app. Local:
  the downloaded `slirp4netns` source archive, in the scratch area outside the
  repository.

## Spike 12 — Nested groups in the SCIM `Me` response (2026-10-30)

*Free Edition only.* Script: [scripts/spike-12/poll_me.py](scripts/spike-12/poll_me.py)
(runs on `appmux-probe`), raw results in [evidence/spike-12.json](evidence/spike-12.json).

- **Run.**
  - **Setup.** With the CLI: workspace groups `appmux-spike-12-child`,
    `-parent`, `-grand` and `-great`, each the member of the next, and the
    owner's user a direct member of `child` only. Nesting is allowed on Free
    Edition.
  - **Calls.** `GET /api/2.0/preview/scim/v2/Me` with the owner's OBO token,
    read inside the probe app, once straight after each creation and then every
    3 s by `poll_me.py`, which prints only the changes of the answer.
  - **Removal.** While the poll ran, the user was removed from `child` with a
    SCIM `PATCH` from the CLI.
- **Result: pass.** The response was recorded with the parent present.

| Question | Observed |
|---|---|
| Does `Me` list groups reached through nesting? | **Yes**, typed `indirect`: `parent` (one level up), `grand` (two) and `great` (three) were listed beside the direct `child`. `users get` with the CLI token showed the same. |
| How soon after a change? | Straight after creating `parent` the response listed direct groups only; a call about 60 s later listed `parent` as `indirect`. For the third level the answers **alternated** between with and without `great` (nine changes in the first 50 s), then stayed with it. |
| And after a removal? | The first answer after the `PATCH` already showed no spike group, then **stale answers returned** (all four groups, `child` now typed `indirect`) four more times, the last about 75 s after the `PATCH`; from then on no spike group. |
| Does the caller's role matter? | Not tested. The owner is in the workspace `admins` group. |

- **Consequences.**
  - **The `Revisit if` trigger of [ADR 0010](../../../docs/decisions/0010-mirror-databricks-permissions-and-give-platform-admins-implicit-access.md)
    fired.** Group matching widens: a grant matches direct and indirect
    memberships, by display name, ignoring the type. Design §9.1, §15, the
    brief, data model §9 point 2 and FR-024 changed in the same change.
  - **The 300 s cache can hold a stale view.** A fetch made inside the lag can
    be cached for the whole TTL. A missed addition denies (fail closed); a missed
    removal still allows, for the TTL plus about 75 s. The design now says so; no
    new mechanism was added. If a tighter revocation bound is needed, that is
    ADR 0010's other `Revisit if` (the TTL), not a nesting question.
- **Not covered here.** More than one user, a caller who is not a workspace
  admin, a workspace other than Free Edition, service principals as members, and
  groups synchronised from an identity provider. One addition and one removal
  were timed. The alternation looks like several servers answering with
  different views; that was not confirmed.
- **Created and removed.** The four groups `appmux-spike-12-child`, `-parent`,
  `-grand` and `-great`; all four deleted, and `databricks groups list` shows
  none named for the spike. `/tmp/poll_me.py` and `/tmp/s12_me.py` on the app
  remain until the final cleanup.

## Spike 4 — Content-store location (2026-10-30)

*Free Edition only.* Script: [scripts/spike-04/store_probe.py](scripts/spike-04/store_probe.py)
(runs on `appmux-probe` as its service principal), raw results in
[evidence/spike-04.json](evidence/spike-04.json).

- **Run.**
  - **As the SP.** A token from `/oidc/v1/token` (`client_credentials`, scope
    `all-apis`) with the client id and secret from the app's environment, read
    inside the process and never printed.
  - **Workspace folder.** `mkdirs` of `/Workspace/Users/<sp-client-id>/appmux-store`,
    then for each size a random file written with
    `POST /api/2.0/workspace-files/import-file/<path>`, read back with
    `GET /api/2.0/workspace/export?path=…&direct_download=true`, compared by
    SHA-256 and deleted: 10, 100, 500 MB, then 501 and 600 MB.
  - **Unity Catalog volume.** The same with `PUT`/`GET`/`DELETE` on
    `/api/2.0/fs/files/Volumes/workspace/default/appmux_spike_4_store/<name>`:
    10, 100, 500 and 1000 MB.
  - **Many small files.** 100 files of 4096 bytes named by their SHA-256 (how the
    content store names them), written, read and deleted at 1, 2, 4 and 8
    threads (workspace) and 1 and 8 threads (volume), each call on a new
    connection.
  - **Access.** The folder's ACL and the volume's owner and grants, read with the
    owner's CLI.
- **Result: pass.** Read and write work at every size up to the limit, and the
  limit is recorded.

| Question | Observed |
|---|---|
| Can the SP create its own store folder? | **Yes**, with `mkdirs` under `/Workspace/Users/<sp-client-id>/`, no grant and no workspace admin. |
| Largest file in the workspace folder? | **500 MB (524,288,000 bytes) works**: write 6.3 s, read 5.3 s, digest equal. **501 MB and 600 MB are refused**: the connection is closed about 1 s into the upload and no HTTP status is returned. 10 MB took 0.6 s to write and 0.4 s to read; 100 MB took 2.3 s and 1.3 s. |
| Can the SP use a Unity Catalog volume? | **Yes, with no grant.** The SP created `workspace.default.appmux_spike_4_store` itself: on Free Edition the group of all workspace users holds `USE_CATALOG` on `workspace` and `USE_SCHEMA` and `CREATE_VOLUME` on `workspace.default`. 1000 MB worked: write 5.6 s, read 10.9 s, digest equal. Larger files were not tried. A paid workspace may grant otherwise (untested). |
| Is a store of many small files fast enough? | **Slow, and different on each route.** Workspace folder: about 9.4 files/s written and 9.5 read sequentially, but parallel calls are rate-limited: at 2 threads 14 of 100 writes got HTTP 429, at 4 and 8 threads 80 of 100 (about 20 calls got through, then 429), and a read of a file whose write had failed returned 404. Volume: 3.3 files/s written and 3.8 read sequentially, **22 to 32 files/s at 8 threads with no 429 in 300 calls**. |
| Who can read the store? | The folder's ACL lists `CAN_MANAGE` for the SP and for the `admins` group and nobody else; the volume is owned by the SP with no grants. **Any workspace admin can read every snapshot and wheel.** |

- **Consequences.**
  - **The location stays the SP's workspace folder**; the volume fallback works
    and takes larger files. [Design §3.3](../../../docs/product/v1-design.md#33-where-state-lives),
    data model §9 point 6 and [ADR 0005](../../../docs/decisions/0005-run-releases-from-immutable-snapshots-and-swap-after-health.md)
    changed in the same change.
  - **A file over 500 MB cannot be stored in the folder.** The plan decides
    between refusing it at Update with the file named (the simplest) and sending
    such files to the volume. No wheel or source file in the spikes so far came
    near the cap; that was not checked for large machine-learning wheels.
  - **Call count, not bytes, sets the cost of a snapshot.** A source tree of
    `N` files is `N` calls at Update and at every cold read. The plan must pick
    one of: a release's small files packed into one archive named by its digest;
    or per-file objects fetched in parallel from the volume; or per-file objects
    in the folder written one at a time with back-off on 429. The design's
    "one copy per distinct file" assumes the last or the second.
  - **Workspace admins are in the trust set of the store.** They already are the
    root of the workspace; the design only has to say it.
- **Not covered here.** Files over 1000 MB on the volume. The `Retry-After`
  value of a 429. Connection reuse (every call opened a new connection, so the
  small-file rates are a floor). Access attempts by principals other than the SP
  and the owner. The Free Edition storage quota. Another workspace.
- **Created and removed.** The folder `appmux-store` (and its `small` subfolder)
  under the SP's home, the volume `appmux_spike_4_store` and `/tmp/spike4` on the
  app; all removed (`workspace list` and `volumes list` confirm). The SP's home
  folder remains: the platform made it with `.db_internal` and `.conversations`.

## Spike 19 — Next.js admin UI on the platform (2026-10-30)

*Local half, then Free Edition only.* Scripts: [scripts/spike-19/](scripts/spike-19/)
(`build.sh`, `check_csp.py`, `check_platform.py`, the two minimal apps and
`platform_app/`), raw results in [evidence/spike-19.json](evidence/spike-19.json).

- **Run.**
  - **Build.** Next.js 16.4.3 (Turbopack), React 19.3.1, exact versions, installed
    with `npm install --ignore-scripts`; the lockfile's 54 packages have no OSV
    advisory. Two minimal apps with `output: 'export'`, `basePath: '/_appmux'`
    and `trailingSlash: true`: one App Router, one Pages Router. Each page polls
    a JSON endpoint every 500 ms and shows the count.
  - **Serve.** A standard-library server under `/_appmux/` with a JSON status
    endpoint; headless Chromium loads the page; a `securitypolicyviolation`
    listener counts every violation. Three policies per app: none (control),
    `script-src 'self'` (strict) and `script-src 'self'` plus the `sha256-…` of
    each executable inline script of the page, computed from the file as served.
    The rest of the policy is the one FR-021 describes (`default-src 'none'`,
    same-origin `connect-src` and `style-src`, `frame-ancestors 'none'`).
  - **Platform half.** A throwaway app `appmux-spike-19` (Python, `app.py` serves
    the export under `/_appmux/` and computes the hash policy per HTML file). First
    deployment: the App Router export built locally, shipped in `export/`, no
    `package.json`. Second deployment: no export; `package.json` (same exact
    versions, `scripts.build = next build`), `package-lock.json`, `pages/` and
    `next.config.mjs`. Headless Chromium loaded `/_appmux/` through the Databricks
    Apps proxy with the owner's CLI token as `Authorization: Bearer` on every
    request, counting CSP violations.
- **Result: the strict policy fails; the hash policy passes, locally and behind the proxy.**

| App | No CSP (control) | `script-src 'self'` | `script-src 'self'` + hashes |
|---|---|---|---|
| App Router | renders, 4 polls, 0 violations | **stuck on "loading", 0 polls, 3 violations** (inline script) | renders, 4 polls, **0 violations** |
| Pages Router | renders, 4 polls, 0 violations | **stuck on "loading", 0 polls, 1 violation** (inline script) | renders, 4 polls, **0 violations** |

- **Behind the platform.**
  - **Prebuilt export.** The proxy passed the page's `Content-Security-Policy`
    header through unchanged (three script hashes) and injected no script: the
    page rendered, polled four times and logged 0 violations. The app saw the
    request headers `X-Forwarded-Access-Token`, `X-Forwarded-Email`,
    `X-Forwarded-User`, `X-Forwarded-For`, `X-Real-Ip` and `X-Databricks-*`.
  - **Build by the platform.** With a `package.json` in a Python app the platform
    runs `npm install` (npm 10.9.9; 27 packages added, `found 0 vulnerabilities`,
    7 s), then `npm run build` (`next build`, 5 s), then starts the app command. The
    export lands in `out/` beside the source and `node_modules/` stays in the app
    directory, both readable by the running app. The built page also rendered,
    polled and logged 0 violations (one hash). With no dependency file the log says
    `No dependencies file found. Skipping installation.`
- **Why.** The export is not script-free. Every App Router page carries three
  executable inline scripts (210 B, 43 B and 4–5 KB that hold the page's data);
  every Pages Router page carries one (about 327 B) plus a JSON data block that
  the browser does not execute. Without them the page never starts. The base path
  and the polling worked in every run that allowed the scripts.
- **Consequences.**
  - **"No inline script" cannot hold for a Next.js export.** The working policy
    is `script-src 'self'` plus the SHA-256 of each page's inline scripts. The
    hashes differ per page and per build (they contain the page's data), so the
    edge computes them from the export files, at start-up or at build, and sends
    the matching policy with each HTML file. This is the fallback the spec names;
    the owner does not need to decide anything yet.
  - **The platform can build the export at deploy,** so the "built before
    deploy" lean in the design is a choice, not a necessity. It still holds: a
    build at deploy runs `npm install` (install scripts included) in the parent
    App, which is the base layer of every child; a prebuilt export keeps that out.
  - **A second way is not tried:** a build step that moves the inline scripts
    into files, which would restore a plain `script-src 'self'`. It is not
    necessary and was not measured.
  - [ADR 0012](../../../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md),
    FR-021 and [design §7.3 and §11](../../../docs/product/v1-design.md) changed in
    the same change (see the ADR's "Spike results").
- **Not covered here.** A browser session behind the proxy (the cookie flow; the
  runs sent a bearer token). Browsers other than Chromium. Pages that POST to the
  JSON API or open a second route. The production export of the real admin UI
  (only a one-page probe was built). A build that needs the registry when it is
  unreachable. Install scripts: the local install ran with `--ignore-scripts`, the
  platform's did not (no problem seen).
- **Created and removed.** Local build directories in the session's scratch
  space. In the workspace: the app `appmux-spike-19` and the workspace folder
  `spike-19-harness` under the owner's home; both are removed at the end of the
  run.

## Spike 11 — Secrets API from an App (2026-10-30)

*Free Edition only.* Scripts: [scripts/spike-11/secrets_probe.py](scripts/spike-11/secrets_probe.py),
[scope_probe.py](scripts/spike-11/scope_probe.py), [acl_poll.py](scripts/spike-11/acl_poll.py) and
[secret_sql_probe.py](scripts/spike-11/secret_sql_probe.py) (all run on `appmux-probe`), raw results
in [evidence/spike-11.json](evidence/spike-11.json).

- **Run.**
  - **Setup.** With the CLI: scopes `appmux-spike-11-secrets` (key `k1`) and
    `-other` (key `k2`), each value random bytes piped to `put-secret` and never
    printed; the app's SP given `READ` with `put-acl`. The expected SHA-256 of
    each value stayed on the workstation.
  - **As the SP.** A token from `/oidc/v1/token`, then `GET /api/2.0/secrets/get`,
    `secrets/list`, `secrets/scopes/list`, and the SDK's `dbutils.secrets.get` and
    `secrets.get_secret`, on both scopes; the probe prints the HTTP status and
    whether the SHA-256 of the value equals the expected one, never the value.
    Then a third scope the SP was never granted, and a scope the SP creates,
    writes, lists and deletes itself.
  - **As the owner's OBO token.** `secrets/list` and `secrets/get` with 12
    other scopes in `user_api_scopes`, and with none. The names that
    `user_api_scopes` accepts were mapped by trying each with `apps update`.
  - **Through SQL.** `SELECT length(secret('<scope>', '<key>'))` with the
    Statement Execution API on the Serverless Starter Warehouse, once with the
    owner's OBO token (app scope `sql` only) and once with the SP token.
  - **Changes of `READ`.** Two scopes had the SP's `READ` removed, one scope was
    granted it; `acl_poll.py` called `get` and `list` every 3 to 5 s.
- **Result: pass.** The SP reads values; the OBO listing the design assumed does
  not exist, and a different check does.

| Question | Observed |
|---|---|
| Can the appmux SP read a secret value from inside an App? | **Yes, three ways**, with `READ` on the scope and nothing else: `GET /api/2.0/secrets/get`, the SDK's `dbutils.secrets.get` and `secrets.get_secret`. Each value's SHA-256 equals the expected one. The SP is not a workspace admin. The documentation's "`DBUtils` only" does not hold here. |
| Does it need `READ` per scope? | **Yes.** A second scope with `READ` read the same way. A scope with no grant answered `get` and `list` with HTTP 403 (`does not have secret-scopes.secrets/get permission on scope …`). |
| Can the SP create a scope of its own? | **Yes**: create, put, list and delete all returned HTTP 200. A human who is not an admin was not tried. |
| Does the attach-time key listing work with the attacher's OBO token? | **No.** `secrets/list` and `secrets/get` answer HTTP 403 `required scopes: secrets` with 12 other scopes and with none, and `secrets` is not a name `user_api_scopes` accepts (`secrets`, `workspace.secrets`, `workspace.secrets:read`, `secrets.secrets`, `secrets:read`, `secret` and `workspace.secret` were refused with `not a valid scope`). |
| Is there another check the attacher's token can run? | **Yes: `SELECT length(secret('<scope>', '<key>'))` through the Statement Execution API, with the `sql` scope.** The owner got `SUCCEEDED`; the SP got `SUCCEEDED` on the scope it can read and `FAILED` (`INVALID_SECRET_LOOKUP`) on the two it cannot; a missing key gave the same error, so the call does not show which keys exist. It returns the length, never the value. The HTTP status is 200 in every case: the outcome is the statement state. It needs `CAN_USE` on a warehouse that is running or will start. |
| How soon does a change of `READ` show? | **A grant at once**: the first call after `put-acl` and all 80 calls over the next 4 minutes answered 200. **A revocation not at once**: one scope kept answering 200 for 163 s and was denied from then on; on another, allowed and denied answers alternated for about 6 min after the revocation (the time of that revocation was not recorded to the second). |

- **Consequences.**
  - **Secret references ship.** [ADR 0009](../../../docs/decisions/0009-attach-resources-with-the-attachers-own-token-and-reference-secrets.md)
    stands; the encrypted fallback store is not planned. [Design §8.3](../../../docs/product/v1-design.md#83-secrets-where-the-value-comes-from)
    and the data model changed in the same change.
  - **The attach check for a secret is the SQL `secret()` call, not a key
    listing.** It runs as the attacher, so the confused-deputy rule holds: an app
    admin cannot attach a scope they cannot read themselves. It needs a
    warehouse the attacher can use; the plan must say which (a setting, or the
    attacher's own choice).
  - **Revoking `READ` from the appmux SP is not immediate.** For a few minutes
    a child may still start with the secret, and a start may fail and then
    succeed. The design now says so.
- **Not covered here.** A human who is not an admin creating or reading a scope;
  a second human; a paid workspace; scopes backed by a key vault; a warehouse
  that is stopped (the warehouse was running); more than one revocation per
  scope.
- **Created and removed.** Scopes `appmux-spike-11-secrets`, `-other` and
  `-nogrant`; all deleted, and `secrets list-scopes` shows none named for the
  spike. The fourth scope (`-spcreated`) was created and deleted by the SP
  itself. `/tmp/spike11` on the app was emptied by the later restart.

## Spike 15 — Workspace file read with the presser's OBO token (2026-10-30)

*Free Edition only.* Script: [scripts/spike-15/workspace_read_probe.py](scripts/spike-15/workspace_read_probe.py)
(runs on `appmux-probe`), raw results in [evidence/spike-15.json](evidence/spike-15.json).

- **Run.** The app's `user_api_scopes` held `workspace.workspace:read` (and 11
  others); after a stop and start, the probe called `workspace/get-status`,
  `workspace/list` and `workspace/export` on a folder in the owner's home with the
  owner's OBO token, then on the SP's home folder, then tried a write
  (`workspace/mkdirs`) with the same token. The same reads ran with the SP's
  token, which has no grant on the owner's folder. Last, the app was restarted
  with no scopes and the calls repeated.
- **Result: pass,** with one difference from the pass condition: a principal with
  no access gets an **empty folder, not an error**.

| Question | Observed |
|---|---|
| Which scope lets appmux list and export a folder as the presser? | **`workspace.workspace:read`**. With it the OBO token got `get-status` 200, `list` 200 (2 files) and `export` 200 (10,220 bytes). The scope name `workspace` is refused; `workspace.workspace` and `workspace.workspace:read` are accepted. |
| Does the scope also allow writing? | **No.** `mkdirs` with the same token: HTTP 403 `Provided OAuth token does not have required scopes: workspace`. |
| Without the scope? | HTTP 403 `Invalid scope, required scopes: workspace` on `get-status`, `list` and the write. |
| What does a principal with no access get? | **No access error.** The SP, with no grant on the owner's folder: `get-status` 200, `list` 200 with **0 objects**, `export` of a file known to exist **404** (`Path … doesn't exist`). |
| Is the scope in the workspace's allowlist? | The platform accepted it with no extra step, by the owner, who is a workspace admin. Whether a non-admin app owner can add it was not tested. |

- **Consequences.**
  - **The scope for the Update's workspace read is `workspace.workspace:read`.**
    [Design §13.1](../../../docs/product/v1-design.md#131-what-the-operator-sets-up)
    lists it. [ADR 0005](../../../docs/decisions/0005-run-releases-from-immutable-snapshots-and-swap-after-health.md)
    and FR-003 stand: Update reads with the presser's token.
  - **Update cannot tell "no access" from "empty" or "missing".** An empty
    listing or a 404 on a file the registry knows must be reported as "empty, or
    you cannot read it", and a snapshot of zero files must be refused. The design
    now says so.
- **Not covered here.** A folder the owner cannot read: the owner is a workspace
  admin and reads every folder, so only the SP stood for "no access". Folders
  shared by a group, a Git folder, files over a few MB, a notebook-type object.
- **Created and removed.** Nothing: the write attempt was refused. `/tmp/spike15`
  on the app was emptied by the later restart.

## Spike 16 — Per-type attach checks (2026-10-30)

*Free Edition only.* Script: [scripts/spike-16/attach_probe.py](scripts/spike-16/attach_probe.py)
(runs on `appmux-probe`), raw results in [evidence/spike-16.json](evidence/spike-16.json).

- **Run.**
  - **Objects.** Made with the CLI: an MLflow experiment, a job (never run), a
    volume, a table and a function in `workspace.default`. Used as they were: the
    Serverless Starter Warehouse, the system endpoint `databricks-gte-large-en`,
    the connection `a`, the sample Genie space, the app `appmux-spike-19`.
  - **Calls.** For each type the candidate read with the owner's OBO token, and
    with the SP's token (a non-admin with no grant on the objects). Two runs:
    with 12 scopes in `user_api_scopes` (`workspace.workspace:read`,
    `files.files:read`, `sql`, `serving.serving-endpoints:read`,
    `catalog.catalogs:read`, `catalog.schemas:read`, `catalog.tables:read`,
    `catalog.connections:read`, `vectorsearch.vector-search-indexes:read`,
    `dashboards.genie:read`, `apps`, `ai-gateway`) and with none, where every
    refusal names the scope the call requires.
  - **Warehouse.** The `users` group's `CAN_USE` was removed for 2 min 23 s, and
    the SP's `GET warehouses/{id}` and `SELECT 1` compared; it was then put back.
- **Result: pass.** 8 of the 14 types have a check with an observed refusal; the
  other 6 stay `unverified`.

| `type` | Read call | Scope the app declares | Attacher (OBO) / no-access SP | Result |
|---|---|---|---|---|
| `secret` | `SELECT length(secret(scope, key))` through the Statement API | `sql` | `SUCCEEDED` / `FAILED` | **verified** ([spike 11](#spike-11--secrets-api-from-an-app-2026-10-30)) |
| `sql_warehouse` | `SELECT 1` through the Statement API (a use test) | `sql` | `SUCCEEDED` / denied | **verified**, but it runs a statement and can start the warehouse. `GET /sql/warehouses/{id}` answered the SP 200 with or without `CAN_USE`, so it proves nothing; `permissions/warehouses/{id}` answers only a `CAN_MANAGE` holder |
| `mlflow_experiment` | `GET /mlflow/experiments/get` | needs `mlflow`: **not accepted** | 403 on scope / 403 | **unverified** |
| `serving_endpoint` | `GET /serving-endpoints/{name}`; the response carries the caller's `permission_level` | `serving.serving-endpoints:read` or `model-serving` | 200 `CAN_MANAGE` / 200 `CAN_QUERY` | **verified for the level**: compare the returned level with the requested one (only system endpoints exist, open to all users) |
| `lakebase_database` | login with the OBO token | none ([spike 1](#spike-1--obo-login-to-lakebase-2026-10-30)) | logs in / not tried | **unverified**: only an expired token was refused |
| `job` | `GET /jobs/get` | needs `jobs`: **not accepted** | 403 on scope / 403 | **unverified** |
| `uc_table` | `GET /unity-catalog/tables/{name}` | `catalog.tables:read` | 200 / 403 (no `SELECT`) | **verified** |
| `uc_volume` | `GET /fs/directories/Volumes/…` (Files API) | `files.files:read` or `files` | 200 / 403 (no `READ VOLUME`) | **verified**; `volumes/get` needs `unity-catalog` and no accepted name covers volumes |
| `uc_function` | `DESCRIBE FUNCTION` through the Statement API | `sql` | `SUCCEEDED` / `FAILED` (no `EXECUTE`) | **verified**; `functions/get` needs `unity-catalog` and no accepted name covers functions |
| `uc_connection` | `GET /unity-catalog/connections/{name}` | `catalog.connections:read` | 200 / 403 | **verified** |
| `genie_space` | `GET /genie/spaces/{id}` | `dashboards.genie:read` or `genie` | 200 / 403 (`Can View` needed) | **verified** |
| `vector_search_index` | `GET /vector-search/indexes/{name}` | `vectorsearch.vector-search-indexes:read` or `vector-search` | — / — | **unverified**: Free Edition has no vector search endpoint, so no index could exist; both answered 404 |
| `databricks_app` | `GET /apps/{name}` | `apps` | 200 / 200 | **unverified**: the app's ACL lists only its owner and `admins`, yet the SP read it, so the call does not carry access |
| `gateway_model_service` | not tried | `ai-gateway` is accepted as a scope name | — / — | **unverified**: no object, API path not confirmed |

| Other question | Observed |
|---|---|
| Which scope names does an app accept? | Coarse: `sql`, `apps`, `genie`, `model-serving`, `vector-search`, `files`, `ai-gateway`. Fine-grained: those listed above. **Refused**: `secrets`, `mlflow`, `jobs`, `access-management`, `unity-catalog`, `workspace`, `catalog`, `catalog.volumes`, `catalog.functions`, `ml.experiments`, `jobs.jobs` and their `:read` forms. The run with no scopes shows the coarse scope each call requires (`sql`, `mlflow`, `model-serving`, `jobs`, `unity-catalog`, `genie`, `vector-search`, `apps`, `files`, `access-management`, `workspace`, `secrets`). |
| Does a read prove the requested permission level? | **Only where the response carries the level** (the serving endpoint). Elsewhere a successful read proves the attacher can see the object, not that they hold `CAN_MANAGE` rather than `CAN_VIEW`. The permissions API shows an ACL only to a manager and needs `access-management`, which no app accepts. |
| How soon does a removed `CAN_USE` bite? | The SP's `SELECT 1` succeeded for about 90 s after the removal, was denied, succeeded once more, and was then denied again: the same alternation as [spike 12](#spike-12--nested-groups-in-the-scim-me-response-2026-10-30) and [spike 11](#spike-11--secrets-api-from-an-app-2026-10-30). |
| Does the Statement API route need anything else? | A warehouse the attacher has `CAN_USE` on, running or able to start. A failed statement returns HTTP 200; the outcome is `status.state`. |

- **Consequences.**
  - **The catalogue of checks is in the data model and [design §8.2](../../../docs/product/v1-design.md#82-resources-and-env-entries).**
    Eight types verified, six `unverified` (these cannot be attached with
    `shared_sp`, as designed).
  - **"At the requested permission or better" overreaches.** The design now says
    the check proves that the attacher can reach the object, and the level only for
    types whose response carries it.
  - **`shared_sp` accepts only `ok`, so it is closed to `mlflow_experiment`, `job`,
    `databricks_app`, `lakebase_database` and the two untestable types** until a
    check exists.
  - **The Statement API route needs a warehouse setting** for the checks of
    `secret`, `uc_function` and `sql_warehouse`.
  - **[ADR 0009](../../../docs/decisions/0009-attach-resources-with-the-attachers-own-token-and-reference-secrets.md)
    is `accepted`**: spikes 11 and 16 are recorded and most types have a check.
- **Not covered here.** A human who is not an admin (the owner is an admin and
  sees every object, so the SP alone stood for "no access"); levels below the
  highest for a type other than the serving endpoint; Lakebase and vector search
  and gateway objects; paid-only types; a stopped warehouse as the attacher's.
- **Created and removed.** The experiment, job, volume, table and function
  `appmux-spike-16-*` / `appmux_spike_16_*`; all removed (each list shows none).
  The warehouse's ACL was changed and restored (`users` `CAN_USE` is back).
  `/tmp/spike16` on the app was emptied by the later restart.

## Spike 10 — Shared-SP token refresh (2026-10-30)

*Free Edition only.* Scripts: [scripts/spike-10/refresh_test.py](scripts/spike-10/refresh_test.py)
(runs on the throwaway app `appmux-spike-10`), [narrow_scope.py](scripts/spike-10/narrow_scope.py)
and [narrow_sql_secret.py](scripts/spike-10/narrow_sql_secret.py) (run on `appmux-probe`), raw
results in [evidence/spike-10.json](evidence/spike-10.json).

- **Run.**
  - **Refresh.** A supervisor process held the SP's client id and secret and
    minted the tokens. It started three plain subprocesses with **no** client id,
    secret or inherited token. Each called `WorkspaceClient().current_user.me()`
    with the **unmodified** Databricks SDK every 300 s for 70 min, so the token's
    real life of 3600 s expired mid-run. **A** got
    `DATABRICKS_METADATA_SERVICE_URL`, a local HTTP endpoint the supervisor
    served. **B** got `DATABRICKS_AUTH_TYPE=databricks-cli` and
    `DATABRICKS_CLI_PATH` naming a stub script that prints a token file the
    supervisor rewrites atomically. **C**, the control, got the token minted at
    start in `DATABRICKS_TOKEN` and nothing else. The supervisor re-minted when
    less than 600 s of life was left. A dry run first advertised a life of 90 s
    for 3 min with A and B only.
  - **Narrow scope.** Token requests to `/oidc/v1/token` with `scope` set to
    `all-apis`, `secrets`, `sql`, `unity-catalog`, `iam.current-user:read` and two
    combinations, each followed by four calls (Secrets API, SCIM `Me`, apps list,
    warehouses list). A last run read a secret through `SELECT length(secret(…))`
    with tokens of scope `sql`, `all-apis` and `secrets`.
- **Result: pass.** Both mechanisms keep an unmodified SDK working across the
  expiry, and a narrower scope is accepted and enforced.

| Question | Observed |
|---|---|
| Does the unmodified SDK survive the expiry with A (metadata endpoint)? | **Yes.** 15 calls, 0 failures. The SDK fetched a token once at start and kept it; when the first one expired it asked the endpoint again (at t+3605 s) and got the token the supervisor had minted at t+3000 s. |
| ... with B (token command)? | **Yes.** 15 calls, 0 failures, the same token sequence. The supervisor rewrote the file at t+3000 s; the SDK kept using the old token until it expired, then ran the stub and read the new one. |
| ... with C (static variable)? | **No.** 12 calls succeeded; every call after t+3600 s failed with `PermissionDenied: unable to parse response` (the 401 body is not JSON). |
| Does the token request accept a scope narrower than `all-apis`? | **Yes.** It echoes the granted scope. With no `scope` parameter the request is refused (HTTP 400 `invalid_scope`, "Request must specify at least one valid scope"); an unknown name is refused (`Invalid scope: bogus-scope-xyz`). |
| Is the narrower scope enforced? | **Yes.** `sql` gets warehouses (200) and is refused the Secrets API (403 `required scopes: secrets`) and apps (403 `required scopes: apps`); `secrets` gets the Secrets API and nothing else; `unity-catalog` and `iam.current-user:read` get neither. SCIM `Me` answers 200 to every token. A combination gets the union (`secrets sql`). |
| Does narrowing keep a child from reading secrets? | **Not if it holds `sql`.** A token with only `sql` is refused by the Secrets API and **still reads a secret the SP can read** through `SELECT length(secret(scope, key))` on a warehouse the SP may use (`SUCCEEDED`); a token with only `secrets` is refused by the warehouse. |

- **Gotchas found.**
  - The SDK's token-command path parses `expiry` as local naive time
    (`2026-10-30T14:39:00`), not UTC.
  - The SDK keeps a token until it expires, so a replacement must exist before
    then; the supervisor re-minted 600 s early and no call saw a gap.
- **Consequences.**
  - **A shared-SP child can run unmodified SDK code.** The mechanism for the plan
    is the token command (B): it needs no network path, and the child's own
    network namespace has no route to the supervisor's loopback, which A would
    need a relay for (A was not tried inside a sandbox). [Constitution I](../../../.specify/memory/constitution.md)
    holds without a helper in the child. [Design §8.5](../../../docs/product/v1-design.md#85-choosing-a-mode-and-delivering-credentials)
    and [ADR 0011](../../../docs/decisions/0011-gate-credentials-and-platform-fields-behind-a-request-catalogue.md)
    say so.
  - **O3 (narrow the lent token) is possible.** [Design §8.4](../../../docs/product/v1-design.md#84-shared-sp-what-it-reaches-and-what-can-be-done)
    records it. It helps only against reaching a service the child does not
    need; it does **not** stop a `sql` child from reading every scope the appmux
    SP can read. Whether v1 narrows the token is a plan decision; the data model
    has no column for a scope list.
- **Not covered here.** The children were plain subprocesses of the supervisor,
  not in the namespaces of [spike 2](#spike-2--filesystem-exposure-2026-10-30);
  the token file's permissions inside the sandbox; a revoked delegation (a copied
  token stays valid until its own expiry); other scope names than those listed;
  tokens for an SP other than the app's own; more than one expiry.
- **Created and removed.** App `appmux-spike-10` and the workspace folder
  `spike-10-harness` (deleted; the folder no longer lists, the app showed
  `DELETING`); scope `appmux-spike-10-secrets` (deleted; `list-scopes` shows
  none). `/tmp/spike10` on `appmux-probe` stays until the final cleanup.

## Spike 6 — Egress mechanics (2026-10-30)

*Free Edition only.* Scripts in [scripts/spike-06/](scripts/spike-06/): `egress_stack.py` builds
the namespaces and starts `slirp4netns`, `egress_pump.py` is the pump prototype (the rule
engine is `decide()`), `egress_child.py` runs inside the sandbox, `egress_lab.py` and
`egress_followup.py` are the drivers, `check_decide.py` holds assertions on the rule engine
(runs anywhere), `neighbour/` is the second App and `neighbour_check.py` its controls. Raw
results are in [evidence/spike-06.json](evidence/spike-06.json) (runs 6a–6d plus the neighbour
controls; the file says which run supersedes which).

- **Run.**
  - **Stack.** The [spike 2](#spike-2--filesystem-exposure-2026-10-30) `unshare` command (user, mount,
    net, pid) started a child that built the veth pair over rtnetlink and handed an `AF_PACKET`
    socket to the parent. `slirp4netns` v1.3.6 (`--netns-type=tapfd`, `--disable-host-loopback`,
    checksum from the probe archive) and the pump (Python, one thread per direction) joined
    them. The destinations were echo servers on the container's own address, so a rule could name
    that address the way the global list does.
  - **Reload.** The rules file was replaced atomically while new connections were attempted every
    few milliseconds, and while one TCP connection exchanged a line every 20 ms.
  - **Ports and fragments.** TCP and UDP rules, ranges and both orders of an allow/deny pair; a
    4000-byte UDP datagram sent whole by the kernel (so fragmented), and raw frames sent before
    the capability drop: whole, two fragments, the second fragment alone, IP options. Each was
    repeated with fragments passed (control) and with no port rule anywhere.
  - **IPv6.** The design arm (pump drops IPv6, `slirp4netns` without `--enable-ipv6`) and a control
    arm (pump passes IPv6, `--enable-ipv6`).
  - **Neighbours.** The spec asks for a sweep of the container's /24. The pod network is a /19 that
    can hold other tenants' pods, so **no sweep was run**. Instead a second App of the same
    workspace (`appmux-spike-6-neighbour`) reported its own address and ran a listener, so there
    was one address known to be ours, tried in both directions and with a control.
  - **Own address.** Every TCP port listening on the container's address (read from
    `/proc/net/tcp`) was tried from the sandbox, with no rule and with the design's
    `deny <container-ip>/32`.
- **Result: pass.** Each behaviour is recorded as designed or as a gap. The design differs in
  three places (below) and is corrected.

| Question | Observed |
|---|---|
| Do rules reload live? | **Yes.** The pump had a replaced file loaded 0.3–10 ms after the write. A *new* connection started after the load followed the new rule: the first failing attempt began 86–91 ms after the load (4 of 4 attempts made while denied timed out), and after the allow the first success began 393–398 ms after the load. |
| ... for an open connection? | **A deny cuts it at once, silently; an allow does not bring it back at once.** Frames of the open connection were dropped (no echo arrived while denied) and neither side saw a reset, an error or a close; every line sent was echoed in all four arms. Recovery waits for the child kernel's TCP retransmit backoff, so it grows with the deny: a 2 s deny, first echo 0.66 s after the allow and the longest echo latency 3.35 s; 6 s: 0.07 s and 6.77 s; 15 s: 12.47 s and 28.12 s; **30 s, watched for 90 s: first echo 24.72 s after the allow, longest latency 55.37 s**. |
| What does a blocked connect look like? | A silent `timeout`: no RST and no ICMP. This is the "timeout, not a refusal" of [design §6.2](../../../docs/product/v1-design.md#62-network-path). |
| Do port rules work? | **Yes.** TCP and UDP rules are protocol-specific (`deny tcp 47102` leaves UDP 47103 and 47104 open); a TCP connect to a port denied only for UDP gets `ECONNREFUSED`, so it reached the host. Ranges work. First match wins in both orders (`allow tcp 47101` then `deny` of the whole address: 47101 works and everything else times out; reversed: everything times out). Each rule counts its hits. |
| Are fragments dropped when port rules exist? | **Yes, as designed.** With a port rule present the 4000-byte datagram (kernel-fragmented) and raw two-fragment and second-only packets are dropped and counted as `fragment`, while a 1000-byte datagram passes. IP options (header of 24 bytes) are parsed and the port still matched. With **no** port rule fragments pass. After the capability drop raw sockets give `EPERM`, so only the kernel's own fragmentation remains. |
| Is dropping them *needed*? | **No: it is conservative.** Control arm: with fragments passed, `slirp4netns` reassembles, a first fragment to the denied port is dropped by the rule, and 0 datagrams reached the denied port; a second fragment alone is harmless. The cost is that **UDP datagrams over the MTU die while port rules exist** (a 4000-byte datagram timed out in the design arm and worked in the control arm). |
| Does an allow-list keep DNS? | **Yes if the child asks `10.0.2.3`.** With a trailing `deny 0.0.0.0/0` and the virtual subnet always allowed, DNS to `10.0.2.3` works and TCP to the resolved address times out; with the virtual subnet subject to the rules, DNS also times out. (A child whose `resolv.conf` names another resolver is a different case: [spike 7](#spike-7--throughput-hogs-forks-and-builds-2026-10-30).) |
| What happens to IPv6? | **Design arm: nothing leaves.** The pump dropped every IPv6 frame (counted), the child has only link-local addresses and no default route, and connects return `ENETUNREACH`. **Control arm** (`--enable-ipv6`, pump passes): the child got an `fd00::` address and a default route through `fe80::2`, a UDP route lookup succeeds, but TCP to a public address and to `fd00::2` fails in 0 ms with `ENETUNREACH`, and DNS to `fd00::3` times out. The probable cause is an ICMPv6 unreachable from `slirp4netns`, because the container has no global IPv6 address (inferred; the packet was not captured). IPv6 NAT on a host that has global IPv6 is **untested**; the design never enables it. |
| Can a child reach a neighbouring pod? | **Not on Free Edition, in either direction, whatever our rules say.** The neighbour is inside the container's /19 (8190 addresses). A plain process on the container timed out (3 s) on the neighbour's ports 47200 and 8000; the sandbox timed out with no rule, with the subnet denied and with only the neighbour denied. Controls: the neighbour connected to its own address on both ports, and the neighbour's attempt to reach a listener on the probe container timed out while the listener counted 0 connections. The platform drops pod-to-pod traffic between two Apps of this workspace. |
| What listens on the container's own address? | **28 TCP ports**: 111, 1095, 1105, 4315–4318, 6000, 7270 (fluent-bit), 7769, 7778, 7781, 7783, 7808, 7850, **8000 (the App port, where the edge listens)**, 8070, 8184, 8686, 8687, 8696, 8697, 9080, 19098, 22005–22007, 22014. With no rule the sandbox connected to **28 of 28**; with `deny <container-ip>/32` it connected to **0 of 28**. |

- **Gotchas found.**
  - A blocked connect and an unreachable host look identical to the child (a timeout), so it cannot tell a rule from an outage.
  - An open connection that crosses a deny does not fail: it stalls, so a client sees a hang up to a minute rather than an error.
  - The pump's own stats file is rewritten every 0.25 s; a reader that wants the drop count for a test waits for that.
- **Consequences.**
  - **The container address must stay on the global default list**, and the edge must not trust
    identity headers because of the port they arrive on: without the deny a child reaches the
    edge's port directly (it can forge `X-Forwarded-*`) and 27 other services, among them
    fluent-bit. [Design §6.3](../../../docs/product/v1-design.md#63-egress-rules) and the
    `self_check()` row say so, and the canary tries every listening port, not one.
  - **Reload semantics are stated in the design**: a new deny takes effect at once and without a
    reset; removing it lets an open connection recover only as fast as the child's TCP retransmit
    allows (about 25 s after a 30 s deny), so an operator should expect long stalls rather than
    errors, and new connections recover in under half a second.
  - **Fragments stay dropped when port rules exist**, with the reason and cost recorded.
  - **Neighbouring pods**: unreachable on Free Edition, so the rule "reachable neighbours add the
    subnet to the default list" does not trigger; the paid workspace is untested and an admin can
    add the subnet to the editable list. The threat-model residual says so.
  - **IPv6** stays unsupported; the design arm is verified, the control arm is a diagnosis.
  - [ADR 0003](../../../docs/decisions/0003-isolate-children-with-unprivileged-namespaces-and-a-filtering-pump.md)
    records the result (it was already `accepted`).
- **Not covered here.** The paid workspace's pod network; the /24 sweep (not run on purpose, see above);
  other tenants' pods; ICMP and protocols other than TCP and UDP; IPv6 NAT on a host with a global
  IPv6 address; DNS tunnelling (a residual); more than a handful of rules; the pump under load
  ([spike 7](#spike-7--throughput-hogs-forks-and-builds-2026-10-30)).
- **Created and removed.** App `appmux-spike-6-neighbour` and the workspace folder
  `spike-6-neighbour` (removed in the final cleanup, recorded at the end of this file);
  `/tmp/spike6` and `/dev/shm/spike6-*` on `appmux-probe` (the `slirp4netns` binary lives there;
  removed in the final cleanup).

## Spike 7 — Throughput, hogs, forks and builds (2026-10-30)

*Free Edition only.* Scripts in [scripts/spike-07/](scripts/spike-07/): `egress_load.py` is the
driver (sections `ceiling`, `bulk`, `connections`, `edge`, `hog`, `idle`, `fork`, `disk`,
`build`), `load_ops.py` runs inside the sandbox, `edge_standin.py`, `relay.py` and
`child_http.py` are the edge stand-in, the relay inside the sandbox and the trivial child,
`http_loadgen.py` and `load_server.py` are the load, and `cpu_ceiling.py` checks how much CPU
the container can use. They run on the [spike 6](#spike-6--egress-mechanics-2026-10-30) stack
(`scripts/spike-06/egress_stack.py` gained `child_nice` and `child_cpus`). Raw results are in
[evidence/spike-07.json](evidence/spike-07.json): three runs, and the file says which one
supersedes which.

- **Run.**
  - **Stand-ins.** There is no edge yet, so the "edge" is a minimal asyncio HTTP/1.1 proxy
    that forwards over a unix socket to a relay inside the sandbox, which connects to a trivial
    child on the sandbox's loopback. It does no identity check, routing, ACL or metrics, so the
    real edge costs more per request. The baseline is the same edge forwarding straight to a
    child with no sandbox and no relay.
  - **Bulk and connections.** A 256 MiB download and upload between the sandbox and a local
    server through `slirp4netns` and the pump; a 45.7 MB HTTPS download of a public
    package-index page; then 1, 10 and 100 ping-pong connections of 1 KiB through the pump,
    10 s each.
  - **Edge.** 1, 10 and 100 keep-alive connections for 10 s, as fast as the path answers (closed
    loop), with one and with two load generators, to see whether the generator was the limit.
    CPU of each process group comes from `/proc`; "machine busy" from `/proc/stat`.
  - **CPU ceiling.** Run 1 showed the machine about 60 % busy while the edge test looked
    saturated, so `cpu_ceiling.py` ran 1, 2, 3, 4 and 8 busy loops for 6 s and 4 for 30 s and
    counted the CPU seconds the loops got from their own accounting.
  - **Hog.** Four busy-loop processes in a second sandbox next to the edge and a victim child,
    which was measured closed loop and paced at 500 requests per second (latency counted from
    when a request fell due, so a stall delays everything waiting behind it). Arms: no hog; nice 0;
    nice 19 set by the launcher; nice 19 and a pin to one CPU; hogs that first widen their own
    pin, without and with a seccomp filter (installed before the hogs start) that refuses
    `sched_setaffinity`. A separate check tries to undo nice and the pin from inside.
  - **Fork.** A sandbox forks 2000 processes and holds them 12 s while the victim runs. The
    driver then ends it by killing the namespace's init, once with the container's PID 1 as the
    orphans' parent and once with the driver as subreaper. The same fork was repeated under
    `RLIMIT_NPROC` 300 set by the launcher.
  - **Idle.** 1 and 10 sandboxes (pump, `slirp4netns` and the sandbox's own process) doing
    nothing: CPU, and proportional set size (PSS) of each part.
  - **Disk.** A sandbox writes to a 64 MiB tmpfs mounted in its mount namespace, then to a host
    directory with no limit and with `RLIMIT_FSIZE` 8 MiB.
  - **Builds.** `pip install` of a generated source distribution with a C extension
    (`Python.h`, built by the container's gcc 11.4.0) inside the sandbox, in five arms: no build
    isolation with the environment's setuptools and all egress denied; build isolation fetching
    `setuptools==84.1.0` through the pump; build isolation with all egress denied; and the
    last two again with a `/etc/resolv.conf` that names slirp's resolver (all egress denied;
    egress allowed). `pip` ran with `--retries 1 --timeout 5` and a 300 s limit.
- **Result: pass.** Numbers are recorded and compared with the load proposed for SC-001 (below).
  Neither fallback is needed: the Python pump is not the limit (ADR 0012 stays), and builds that
  need a compiler work (the wheels-only rule stays an option, not a need). The design differs in
  four places (CPU quota, init and zombies, `/etc/resolv.conf`, disk) and is corrected.

| Question | Observed |
|---|---|
| How much CPU can the container use? | **About 2 CPUs, although 4 are visible.** One busy loop got 100 %, two got 199 %, and 3, 4 and 8 loops together got 200–201 % (30 s with 4 loops: 200 %). `/proc/stat` showed about 40 % idle during that, steal was 0, no cgroup CPU file is visible, and only `/proc/pressure/cpu` rose (some-avg10 5 % after one loop, 70 % after four for 30 s). It looks like a CPU quota (CFS bandwidth) that the container cannot read. A test that reads "machine busy 60 %" and concludes there is headroom is wrong. |
| What does the pump carry? | **Bulk: 100.5 MB/s down, 58.7 MB/s up, 92 MB/s for HTTPS from the Internet.** The pump used 124 % of one CPU on the download (slirp 36 %, the sandbox's reader 30 %) and 91 % on the upload. That is about 12 ms of pump CPU per MB, plus 3.6 ms for slirp. The package downloads of [design §6.2](../../../docs/product/v1-design.md#62-network-path) (11–12 MB/s) cost about 14 % of one CPU in the pump. A child that saturates its egress takes most of the quota. |
| ... for many small exchanges? | **8,032 / 24,700 / 27,555 messages/s** of 1 KiB at 1 / 10 / 100 connections, no failures. p50 0.12 / 0.25 / 2.33 ms, p99 0.26 / 0.75 / 37.5 ms. The pump used 25 / 50 / 49 % of one CPU. |
| What does the edge path sustain? | **5,391 / 10,850 / 14,068 requests/s** at 1 / 10 / 100 connections through edge, relay and child in the sandbox (p95 0.28 / 1.17 / 38.3 ms), no failures. Without the sandbox and relay the same edge did 7,919 / 17,638 / 20,641. A second load generator gained nothing (11,069 and 13,726), so the limit is the CPU quota, not the generator: at 10 and 100 connections the accounted CPU (edge, relay, child and generator) was 196–200 %; at one connection it was 113 %, and the path is bound by round-trip latency instead. **The sandbox path costs about 130 µs of CPU per request** (edge 54, relay 43, child 32) against about 69 µs for the baseline. |
| What load do the rest of the tests use? | **500 requests/s over 10 connections**, paced. It is 4.6 % of the stand-in's capacity at that concurrency (10,850), costs about 6.5 % of one CPU (3 % of the quota) and had p95 1.3 ms. See the first consequence below. |
| What does a CPU hog do to another app? | **The quota is exhausted and every app stalls for about 50 ms at a time.** Paced victim (edge, relay and child; its sandbox is not pinned): p95 1.3 ms without a hog, **50.2 ms** with four busy loops at nice 0 (the hog took 185 % of one CPU). Closed-loop throughput fell from 11,168 to 3,191 requests/s. The closed-loop p95 under the hog (9.3 ms) hides the stalls that the paced p95 shows. No request failed. |
| Does `nice` help? | **No for latency.** At nice 19, set by the launcher, the paced p95 was 47.9 ms (hog still 185 %); closed-loop throughput rose only to 4,343. Throttling stops the whole group, whatever the priorities. The sandbox cannot undo it (`setpriority` back to 0 gives `EACCES`). |
| Does a CPU pin help? | **Yes, if the sandbox cannot widen it.** Nice 19 plus a pin of the hog's sandbox to one CPU: the hog used 100 %, the paced p95 was **1.34 ms**, throughput 8,020 requests/s. But `sched_setaffinity` to every CPU is **allowed** inside the sandbox: a hog that widened its pin first took 186 % and the p95 was 49.5 ms. With a seccomp filter that refuses `sched_setaffinity` (`EPERM`) the pin held: hog 100 %, p95 **1.35 ms**, throughput 7,847, and the pin stayed `[3]` after the attempt. |
| What does a fork test do? | **2000 forks in 1.49 s**, about 1 MiB each (2,011 MB less memory available; the container went from 22 to 2,034 processes). The victim's throughput fell 11 % (11,199 to 9,912 requests/s) and its p99 rose from 3.0 to 8.9 ms. |
| Can tearing down the namespace end it? | **Yes: 2001 processes gone in 0.57 s** by killing the namespace's init. But **1 zombie was left**: PID 1 of the container is `lha-runtime`, which does not reap orphans, and the orphans of a killed pid namespace are reparented outside it. With the driver as a subreaper (`prctl(PR_SET_CHILD_SUBREAPER)`) none was left (0.56 s). |
| Does `RLIMIT_NPROC` bound the sandbox? | **Yes, per sandbox.** With the limit at 300 the fork test stopped with `EAGAIN` after 298 forks (299 processes in the sandbox) while 34 other processes of the same real user ran outside: the limit is counted in the sandbox's user namespace, not against the container. |
| What does an idle sandbox cost? | **27.6 MB PSS for one, 21.8 MB each for ten (218.4 MB):** the pump 5.6 MB, `slirp4netns` 0.3 MB and the stand-in's in-sandbox Python process 15.9 MB. Idle CPU for ten: 3 % of one CPU (all of it the pump, whose rules-reload thread wakes every 10 ms). A linear estimate for 100 is **2.2 GB and 30 % of one CPU**; labelled as an estimate. |
| What does a full run directory do? | **A tmpfs sized in the sandbox's mount namespace is a hard cap: `ENOSPC` at exactly 64 MiB** (0.02 s). A plain host directory has **no cap**: 1,024 MiB were written in 1.5 s and free disk dropped 1,024 MB. `RLIMIT_FSIZE` of 8 MiB bounds each file only: 128 files, 1,024 MiB, free disk dropped 958 MB. The memory reading did not move for the 64 MiB tmpfs (inside its noise); that tmpfs pages count as memory is kernel behaviour, not measured here. |
| Does a source build with a C extension work? | **Yes, with the container's gcc.** (1) No build isolation, environment's setuptools 82.0.1, all egress denied: **0.8 s**, `import` gives 5. (2) Build isolation, `setuptools==84.1.0` fetched through the pump: **2.7 s**, 5. (3) Build isolation, egress denied: **fails closed after 128.3 s** (`Temporary failure in name resolution`, `No matching distribution found`), nothing installed. (4) The same with `resolv.conf` naming `10.0.2.3`: **no result after 300 s** (our limit), nothing installed. (5) Build isolation, egress allowed, `resolv.conf` naming `10.0.2.3`: **2.7 s**, 5. |
| What does the build's DNS depend on? | In the stand-in the sandbox saw the **container's own** `/etc/resolv.conf`, so arm 2 resolved through the container's resolver, which the pump allowed. The design's sandbox has no `resolv.conf` ([spike 2](#spike-2--filesystem-exposure-2026-10-30)), so a name lookup would fail. Arm 5 shows that a launcher-written `nameserver 10.0.2.3` is enough. |

- **Gotchas found.**
  - The container has 4 visible CPUs and a quota of about 2 that it cannot see. Read the CPU
    accounting of the processes, not "machine busy".
  - PID 1 of the container does not reap. Old defunct processes with parent 1 pile up, one per
    killed sandbox, until the app is restarted.
  - Run 1 measured the load generator's CPU after it had been reaped (always 0) and did not know
    about the quota, so its edge, hog, fork and build numbers are superseded by runs 2 and 3;
    its bulk, connection and disk numbers stand.
  - A closed-loop test hides stalls (the hog at nice 0 shows p95 9.3 ms closed loop and 50.2 ms
    paced). A load for SC-001 must be paced and its latency counted from when a request fell due.
  - A denied build fails closed but slowly: 128 s with an unreachable resolver, more than 300 s
    when names resolve but connections are dropped (arm 4; the output says only "timed out", so the
    reason is an inference). `pip`'s own `--retries 1 --timeout 5` did not bound either.
  - `setuptools` is pinned in the sdist's `pyproject.toml`. It was checked on OSV.dev before
    use: 84.1.0 has no advisory; the platform environment's 82.0.1 has three records (two
    issues) and none is a `MAL-` advisory.
- **Consequences.**
  - **A load for SC-001 is proposed here and left to the plan:** 500 requests/s over 10
    connections, paced, plain `GET` with a small body. It stays well below capacity (about 3 % of
    the quota) so the criterion measures disruption, not saturation; a 50 ms stall delays about 25
    requests, which a p95 shows (1.3 ms against 50 ms); and the hog tests ran at the same rate, so the numbers
    compare. The real edge adds identity, routing, ACL and metrics, and the Databricks proxy sits
    in front, so its capacity is lower than 10,850 requests/s and has to be measured by the
    plan. **A heavy build running in parallel must not use the whole quota**, or the run will see
    50 ms stalls it did not cause: run it pinned to one CPU like the hog.
  - **ADR 0012 stays `accepted`.** The Python pump carried 100 MB/s with 124 % of one CPU and 27.5k
    messages/s with 50 %. Neither fallback in the spike is needed. What the numbers add: pump CPU
    scales with bytes (about 12 ms per MB), so the cost of a child's egress is a CPU cost shared
    with every other app, and an idle pump should not poll (the 10 ms reload thread is the whole
    idle cost, and a file-change or signal wake-up removes it).
  - **CPU exhaustion is an accepted gap with a mitigation, not a hole.** One hog stalls every app
    for about 50 ms at a time and no request fails. On Free Edition the launcher can keep it from
    exhausting the quota by pinning the children's sandboxes to one CPU (with nice 19) and refusing
    `sched_setaffinity` with seccomp; nice alone does nothing for latency. What was measured is
    that a pinned hog leaves a victim **outside** the pinned CPU untouched (the edge, the pump
    and the reconciler would be that victim). Children that share the pinned CPU compete by
    scheduler share, which was not measured, so the pin does not isolate one child from another.
    Pinning every child to one CPU also caps all children together at one CPU of the two, so the
    plan decides it. The quota of a paid workspace is not known (spike 3 was dropped).
  - **The launcher must be a subreaper**, or each torn-down sandbox leaves a zombie under
    `lha-runtime`.
  - **`RLIMIT_NPROC` works per sandbox** and bounds a fork bomb at the limit (298 of 300, 0.11 s).
    Without it 2000 processes cost 2 GB. Teardown by killing the init of the pid namespace takes
    0.6 s, so a bounded fork test is also an answer to the accepted gap. The value is the plan's.
  - **Writable directories of a sandbox must be size-capped tmpfs**, not directories on the
    container's disk: the latter have no cap (`RLIMIT_FSIZE` is per file). Memory accounting for
    those tmpfs pages is not measured here.
  - **The launcher writes `/etc/resolv.conf` with `nameserver 10.0.2.3`.** A sandbox built from the
    mount table of spike 2 has none; without it name resolution fails.
  - **Builds with a compiler work, so the wheels-only rule is not needed.** The design already
    allows source builds only in `index` mode, with the sandbox's egress rules; spike 7 confirms
    they run (2.7 s). The build needs its own wall-clock limit, because a denied one takes
    128 s or more to fail.
  - **Per-child idle overhead for SC-003:** [design §5.7](../../../docs/product/v1-design.md#57-memory-and-admission-control)
    counts 13 MB for the pump and slirp; here the same two parts are 5.9 MB PSS (a different
    measure) and the stand-in's in-sandbox process adds 15.9 MB. The real figure is SC-003's.
  - [ADR 0012](../../../docs/decisions/0012-build-services-in-python-and-the-admin-ui-in-nextjs.md)
    and [ADR 0003](../../../docs/decisions/0003-isolate-children-with-unprivileged-namespaces-and-a-filtering-pump.md)
    record the result (both were already `accepted`).
- **Not covered here.** The paid workspace (CPU quota, pod network, node sharing: spike 3 was
  dropped); the real edge (the stand-in leaves out identity, routing, ACL, metrics and the
  Databricks proxy in front of it); response bodies and WebSocket or SSE load through the edge
  (the exchanges were small and short); more than 100 connections; the quota's period (the
  50 ms stalls suggest 100 ms, which was not read); other tenants on the node; a memory hog (the
  watchdog of [design §5.7](../../../docs/product/v1-design.md#57-memory-and-admission-control) is
  not exercised); the pin without nice 19; victims that share the pinned CPU; ICMP and protocols other than TCP and UDP; DNS tunnelling.
- **Created and removed.** Nothing in the workspace: everything ran inside `appmux-probe`.
  On the probe: `/tmp/spike7` (scripts and results), `/tmp/spike7-*` (build, host-directory and
  scratch folders), `/dev/shm/spike7-*`, and pip's cache inside the build folder, which holds
  `setuptools==84.1.0`. All are removed in the final cleanup, recorded at the end of this file.

## Cleanup (2026-10-30)

All spikes ran on the Free Edition workspace only; no other workspace was touched. This is the
final removal of what the spikes created. Every entry's own "Created and removed" line points
here.

| Object | Kind | State at the end |
|---|---|---|
| `appmux-spike-19` | throwaway app | deleted (spike 19) |
| `appmux-spike-6-neighbour` | throwaway app | deleted (spike 6) |
| `appmux-spike-10` | throwaway app | deleted earlier (spike 10) |
| `appmux-spike-1` | Lakebase project | deleted; `list-projects` returns none |
| `spike-harness`, `spike-19-harness`, `spike-6-neighbour` | workspace folders under the owner's home | deleted |
| Secret scopes | throwaway | none exist |
| Groups, roles, service principals named `appmux-spike-…` | throwaway | none exist |
| `appmux-probe` | the existing probe app | restored (below) |

- **`appmux-probe` restored.** Its `user_api_scopes` were cleared (`effective_user_api_scopes` is
  back to the platform default, `iam.access-control:read` and `iam.current-user:read`), its
  original source (`app.py` and `app.yaml` only) was redeployed so the harness endpoint used by
  the spikes is gone, and the app was stopped. A final `apps get` shows compute `STOPPED`, no
  resources and no scopes. `apps update` replaces the whole app, so the name and description were
  sent again with the change and nothing else was set.
- **Apps left.** `apps list` shows only `appmux-probe`.
- **Probe container.** The spikes left `/tmp/spike*`, `/tmp/poll_me.py`, `/tmp/s12_me.py`,
  `/tmp/spike/sandbox_npm.py` and `/dev/shm/spike*` on `appmux-probe`, plus Node's compile cache
  and a Python version marker that could not be removed (permission denied). The app was stopped
  and started, which gives a fresh container with an empty `/tmp` (spike 1) and `/dev/shm`, so
  none of them survive. The new container was not listed afterwards, because the redeploy
  removed the harness endpoint.
- **Lakebase.** The databases and roles the spikes created (`appmux_spike9`,
  `appmux_spike9_owner`, `appmux_sp` and the others named in spikes 1 and 9) went with the project
  `appmux-spike-1`.
- **Local environments.** The virtual environments, stub environments, downloaded wheels, the npm
  cache, the `slirp4netns` source archive and the Next.js build outputs sit in the session's
  scratch area under `/tmp`, outside the repository. They hold no credential and are not part of the record.
- **Left in place on purpose.** The workspace folder `appmux-probe2` is the first probe's source
  from 2026-10-29, not a spike object. It is archived in `probe-archive/`; the scripts of spike 6
  read it from there. The deployment history of `appmux-probe` still lists the spike deployments.
- **Local.** Scratch files that held tokens, the probe's URL or the neighbour's address were
  deleted from the session's scratch area. A scan of `specs/` and `docs/` finds no credential,
  workspace identifier, container or neighbour address, or owner's email: placeholders such as
  `<workspace-id>` and `<sp-client-id>` stand in. The only addresses left are slirp's fixed
  virtual subnet (`10.0.2.0/24`) and the private ranges the egress rules name, which identify
  nothing.

## Container runs from the dev machine (2026-10-31)

*Free Edition only.* These are not spikes: they are the first runs of the integration tests
against appmux itself, deployed as the dev App on the Free Edition workspace
(`scripts/sync-deploy.sh`, then `scripts/run-remote-tests.sh`). The caller is the owner's
Databricks CLI token; the platform admin on the dev App comes from a deploy-time substitution of
the admin address that is never committed. Nothing here touched another workspace.

| Task | Test | Result |
|---|---|---|
| T096 | `tests/integration/test_host_a_child.py` | 10 of 10 passed |
| T109 | `tests/integration/test_crash_and_rollback.py` | 2 of 2 passed |
| T068 | `tests/integration/test_self_check_container.py` | 2 of 2 passed; all 13 checks of `self_check` passed in 7.4 s ([evidence](evidence/container-self-check-2026-10-31.json)) |
| T069 | `tests/integration/test_hostile_fixtures.py` | 6 of 6 passed |

What the runs found:

- **The relay refused nothing before the child bound its port.** The relay listened on the
  child's socket at once and hung up when the child's port refused, so the health gate read
  `connection closed before an answer` where [design 5.5](../../../docs/product/v1-design.md)
  says `connection refused` (T096, `slow_start`). The launcher now binds the socket without
  listening, and the relay listens once the child's port first accepts a connection; an
  AF_UNIX socket that is bound but not listening refuses connects. T096 then passed 10 of 10.
- **`workspace.workspace:read` must be in the dev App's `user_api_scopes`.** Update reads the
  source folder with the presser's token; without the scope the request fails with
  `empty, or you cannot read it`.
- **`databricks apps update` replaces the whole app** and takes no default scopes
  (`iam.current-user:read` and `iam.access-control:read` are refused in `user_api_scopes`), so
  every update sends the full body.
- **A changed scope reaches tokens only after the App is stopped and started**; a redeploy or a
  restart is not enough (as in spike 1).
- **Hostile children, through the registry path (T069).** `env_snooper` saw an empty `/proc`
  and could not open `runtime.cfg`; `pkg_env_writer` got `EROFS` in site-packages and in a
  real overlay built from a pinned wheel; `fork_bomb` stopped with `EAGAIN` after 247 forks
  at the limit of 256 and the app stayed `running`; `network_scanner` timed out on the
  metadata address; a public destination (`1.1.1.1:443`) that the control reached timed out
  after the app's own deny rule was set and a new release started a child that read it
  (the egress path out of the container works, and the pump read the rule at the start); and
  a `light_fastapi` neighbour answered `200` throughout, with a p95 of 236 ms idle and 235 ms
  beside the hostile children.
- **A scanner target aimed at appmux's loopback port proves nothing on this platform.** The
  first T069 run configured `appmux_loopback` and the child reported `connected`. The cause is
  not a hole in the sandbox: every child listens on port 8000 inside its own network namespace
  (`DEFAULT_CHILD_PORT`) and the platform gives appmux port 8000 as well, so the connect
  reached the child itself. The test no longer configures that target; the canary of
  `self_check` (T068, `loopback_and_container_ip`) is the proof of the loopback. The owner
  decided on 2026-11-01 to keep port 8000 for every child: uvicorn's default port and the
  `--port 8000` of the design's `app.yaml` example are 8000, and a child that keeps either
  must run unchanged (constitution I).
- **Two scanner targets are not tried by T069.** `container_ip` (a caller outside the
  container does not know the address) and `appmux_loopback` (above) are covered by the
  canary of `self_check` (T068, `loopback_and_container_ip`). A deny rule that changes while a
  child runs is not tested: the pump's reload is not wired (T066).
- **Not covered.** The tests that need a second identity (T148, T155, T167) cannot run on a
  workspace with one user, and the load and host-port tests are not suited to a remote caller.

The dev App, its Lakebase project and its roles are not spike objects and stay in place;
they are not part of the cleanup above.

## Reduced zero-downtime run from the dev machine (2026-11-01)

**This is not evidence for SC-001, and T108 stays open.** It ran `tests/integration/test_zero_downtime.py`
through `scripts/run-remote-tests.sh` against the Free Edition dev App with
`APPMUX_TEST_RATE_PER_S=20` (the task says 500) and the default 10 connections, 5-second slices
and a 60-second control. The load came from this machine over TLS through the Databricks proxy,
with the CLI's bearer token: `tests/load/http_load.py` gained a `bearer_token` and a `tls`
context for this, and the command line is unchanged (a token on a command line is visible to
other processes). The two runs below used the same settings, one after the other.

Why it is not SC-001: the rate is 4 % of the task's; the workspace has one identity and a small
container; every latency includes the platform's proxy and the network (a trivial child showed a
worst-slice p95 of 740 to 790 ms before any operation ran, so those figures are the path's, not
the edge's); the edge's CPU was not read (its pid is not reachable from outside); and the "heavy
build in parallel" took 5 to 8 seconds because its three apps installed wheels that were already
in the content store, so it was not a heavy build.

Failed requests while each operation ran (the control had none), and the worst single-slice p95
in milliseconds before and during the operation:

| Operation | Run 1: failed / requests | Run 1: p95 before → during | Run 2: failed / requests | Run 2: p95 before → during |
|---|---|---|---|---|
| control (no-op) | 0 / 1200 | 776 → 1282 | 0 / 1200 | 789 → 846 |
| register B | 0 / 200 | 1141 → 770 | 0 / 200 | 778 → 789 |
| update B | 0 / 200 | 748 → 797 | 0 / 300 | 773 → 1256 |
| update A, good release | 0 / 1000 | 789 → 979 | 0 / 1000 | 752 → 844 |
| update A, release that fails health | **1 / 1300** | 743 → 939 | 0 / 1300 | 771 → 946 |
| heavy build in parallel (not heavy, above) | 0 / 300 | 977 → 921 | 0 / 200 | 796 → 801 |
| egress rule edit on A | 0 / 200 | 790 → 740 | 0 / 200 | 757 → 856 |

- **Run 1 failed one of its eight tests; run 2 passed all eight.** The failed request in run 1
  answered `502` during the release that fails its health check, against zero allowed by the
  control. The edge itself made that answer: its own per-minute metrics for app A show one `5xx`
  and one `upstream_errors` in that operation's minute, and none in run 2 (about 5 800 requests
  in run 1 and 4 400 in run 2). It is therefore the hop from the edge to the child, not the
  platform's proxy. **The cause is not known**: the warning the edge logs for it does not say
  which error it was, and the platform keeps about 70 seconds of log, so it was gone by the time
  it was looked for. The rerun was made to see whether the failure repeats; it did not. Logged
  as TD-019.
- **What the two runs do show.** In both, A kept answering `200` at the end of the release that
  failed its health check, the swap to the good release lost no request, and registering,
  updating another app, three parallel Updates and an egress rule edit lost none, at 20
  requests a second on Free Edition. That is a smoke test of the harness against the real
  container, no more.
- **Metrics survive the deletion of an app.** The run deletes its apps afterwards, and the
  per-minute rows of `zd-a-…` were still readable through `GET /metrics?from=…&to=…`; that was
  the way to attribute the `502`.

## Remote integration run from the dev machine (2026-11-04)

**None of this is evidence for SC-001 or SC-003, and no task was ticked because of it.** The branch
head (`3f59069`) was deployed to the Free Edition dev App with `scripts/sync-deploy.sh`, and
`scripts/run-remote-tests.sh` ran the
integration suite from the dev machine as the owner of the CLI's token, a platform admin, through the
platform's proxy. Tests that need the container's own port, or another person's identity, cannot
run that way (the table at the end says which).

| Run | What | Result |
|---|---|---|
| 1 | the whole `tests/integration` (61 tests), the tests' own default of 500 requests a second | 43 min 16 s: 23 passed, 8 failed, 2 skipped, 28 errors |
| 2 | `test_zero_downtime.py` at `APPMUX_TEST_RATE_PER_S=20`, a token with about 50 minutes left | 8 passed, 5 min 33 s |
| 3 | the two tests that run 1 failed for being out of date, after correcting them | 1 passed, 4 skipped |
| 4 | `test_metric_queries_on_lakebase.py` on the dev App's Lakebase (T193) | 4 passed, 27 s |

### What run 1 was

- **It was run wrongly, and its zero-downtime numbers mean nothing.** The tests' default is 500
  requests a second; through the proxy and this machine's network that only queued. Every slice
  from the no-op control on shows a worst-slice p95 of about 52 000 ms, and the 5 000 requests of one
  "before" stage took 125 s (40 a second served). The 2026-11-01 run's rule, 20 a second, is in the test's docstring;
  `scripts/run-remote-tests.sh` now sets 20 unless the environment says otherwise.
- **From the middle of "update A with a good release" every call answered `302`**, the proxy's
  login redirect: 15 708 of 20 000 requests in that operation, all of the later operations' load, and
  the admin's own `GET /apps/…` that sets up each later test. The first operations and the first
  test of the file had passed, so the path worked before and stopped working for good. The token is
  asked for once at the start and lasts at most an hour, and the CLI hands out a cached one until it
  is near expiry, so it may have had much less left. The expiry time of that token was not recorded,
  so "the token ran out" is the explanation that fits and is **not proven**; what is known is that
  run 2, at 20 a second with a token of 50 minutes, had no `302`. The script now prints how many
  minutes the token has left and its header says what a `302` after a long run means.

### The two product tests that failed were out of date, not the product

- `test_a_child_that_exits_at_start…` looked for `status 3` in the release's `status_reason`. Since
  `ab54eb0` appmux writes `the app exited with code 3 after 0.1 s`. The test now looks for `exited with code 3`; it passed in run 3.
- `test_requests_walkthrough` put `memory_budget_mb` into a `register` request. The design says a
  `register` request that names a budget is refused (v1-design 9.4, "no budget"), and the admin API
  does so; the platform sets the default and an app admin asks for more with `settings`. The test
  no longer sends one. After the correction its four tests skip remotely (they act as Grace and
  Carol).
- `test_an_unknown_path_under_the_ui_prefix…` answered `302` instead of `404` because it plays the
  proxy itself with `X-Forwarded-Email`, which only works on the container's own port. It is a
  container-only test, not a fault.

### Zero-downtime, run 2 (20 requests a second, 10 connections, 5-second slices, 60-second control)

Failed requests while each operation ran (the control had none) and the worst single-slice p95 in
milliseconds before and during the operation; the 750 to 830 ms before any operation is the path's
(proxy and network), as on 2026-11-01:

| Operation | Failed / requests during | p95 before → during |
|---|---|---|
| control (no-op) | 0 / 1200 | 782 → 1087 |
| register B | 0 / 200 | 827 → 781 |
| update B | 0 / 200 | 752 → 884 |
| update A, good release | 0 / 1000 | 774 → 985 |
| update A, release that fails health | 0 / 1300 | 784 → 1063 |
| heavy build in parallel | 0 / 400 | 778 → 1261 |
| egress rule edit on A | 0 / 200 | 787 → 1168 |

No request failed in about 5 900, the failed-health release included; the `502` of 2026-11-01
(TD-019) did not recur. The same limits hold as then: 4 % of the task's rate, every latency includes
the proxy, the edge's CPU was not read, and the "heavy" build is not heavy.

### What else passed in run 1

`test_crash_and_rollback` (2), `test_host_a_child` (9 of 10; the tenth is the out-of-date one),
`test_hostile_fixtures` (6: `env_snooper` saw no other process and no `runtime.cfg`; `pkg_env_writer`
made 2 attempts and got `EROFS`; `fork_bomb` made 247 forks of 256 and was refused with `EAGAIN`
after 0.6 s; `network_scanner`'s probe of the metadata address timed out; a deny rule took effect; a
neighbour's p95 was 247 ms idle and 278 ms beside the hostile children, over 21 and 39 requests) and
`test_self_check_container` (2; the container's `self_check` reported 13 of 13 checks `ok`, in 7.2 s).

### What did not run, and why

| Test file (tests) | Task | Why |
|---|---|---|
| `test_edge_capacity` (1) | T086 | needs the container: 500 requests a second without the proxy, and the edge's pid to read its CPU. It ran on 2026-11-05: see [Container runs inside the dev App](#container-runs-inside-the-dev-app-2026-11-05) |
| `test_offline_install` (4), `test_open_modes` (3), `test_resources_container` (4), `test_sc004_walkthrough` (1), `test_ui_on_container` (7) | T137, T155, T129, T167, T177 | written against the edge's own port: they play the proxy with `X-Forwarded-Email` and do not use `ops_session`, so through the real proxy they get `302` or fail on `APPMUX_TEST_BASE_URL` having no port; most also need apps or variables registered first (fixture app slugs, a warehouse, a secret, a group, a second person's token). All five ran on 2026-11-05: see [Container runs inside the dev App](#container-runs-inside-the-dev-app-2026-11-05) |
| `test_requests_walkthrough` (4) | T148 | skipped: acts as two other people |
| `test_credential_modes` (5) | T147 | did not run at first: the throwaway service principal, OAuth secret and scratch secret scopes it needs were refused by the session's permission check and not retried. It ran later the same day, after the owner authorised creating them: see [Credential modes from the dev machine](#credential-modes-from-the-dev-machine-2026-11-04) |
| no test file yet | T070, T162, T180 | `test_self_check_fail_closed.py`, `test_capacity.py` and `test_failure_table.py` did not exist on 2026-11-04. They were written and run on 2026-11-05: see [Container runs inside the dev App](#container-runs-inside-the-dev-app-2026-11-05) |

### Gates on the same head

Python: `ruff format --check` (389 files), `ruff check`, `mypy src` (129 files) clean; `pytest
tests/unit tests/contract tests/load`: 7 836 passed in 91 s, with one third-party deprecation warning
(Starlette's test client, an `anyio` alias). The count includes the tests added for token renewal
and for the snooper labels (below); the first run on this head passed 7 829. The head that adds the
grant tests (see the credential-modes section) passes 7 840 in 95 s, with the same format, lint and
type checks clean.
UI: `typecheck`, `lint`, `format:check` clean; `npm test` 158 files, 2 870 tests; `npm run test:export`
3 files, 63 tests; `npm run build` ok. `npm run test:e2e`: the first full run passed 809 and failed
1 (`csp.spec.ts` `app-deployments` at 1280 px light, "console errors" expected `[]`); the same spec
group passed 42 of 42 when rerun alone, and a second full run passed all 810 (5.5 min each). One
failure in two full runs, not reproduced: the cause is not established (the failing run's
console text was lost when the isolated rerun cleared Playwright's output folder), so this is
recorded as a flake, not as understood. If it comes back, keep the run's `error-context.md`.

## Credential modes from the dev machine (2026-11-04)

T147 (`tests/integration/test_credential_modes.py`, seven tests) ran against the deployed dev App on
the Free Edition workspace, through `scripts/run-remote-tests.sh` as the owner of the CLI token.
The owner authorised creating the test resources on the Free Edition workspace that day ("create the
resources needed to test, such as a service principal, secrets, anything within the Free Edition
limits"). The paid workspace was not used.

### Resources created

| Resource | What it is for |
|---|---|
| one service principal (`appmux-test-own-sp`) and one OAuth secret for it that expires after a day | the identity of the own-SP child |
| secret scope `appmux-t-own-sp`: the principal's client id and client secret, with `READ` granted to the appmux App's service principal | appmux resolves the child's two `secret` resources from it |
| secret scope `appmux-t-foreign`: one random value, granted to nobody | the "other apps' secret" the snooper tries to read |

`attach_check_warehouse_id` already named the Free Edition warehouse. Beyond that one `READ`,
nothing was granted to anyone, and no `CAN USE` on `appmux` was given to the new principal. The principal, the secret and
both scopes still exist after the run; removing them is deleting the two scopes and the principal.

### What the snooper children reported

| Test | Result |
|---|---|
| `test_an_own_sp_child_calls_the_api_as_its_own_service_principal` | passed: the child's token belongs to the throwaway principal |
| `test_a_shared_sp_child_calls_the_api_as_the_appmux_service_principal` | passed: the child's token belongs to the appmux App's service principal |
| `test_an_own_sp_child_reaches_neither_other_apps_secrets_nor_the_registry` | first run failed: every attempt without a token was refused, and reading a secret value with the token was refused (`403`), but `list_scopes` with the token succeeded. Passes once `list_scopes` is accepted by name (below) |
| `test_a_shared_sp_child_reaches_neither_other_apps_secrets_nor_the_registry_beyond_the_residual` | first run failed for the same reason; the registry login with the token succeeded, which is the accepted residual of [TD-002](../../../docs/tech-debt.md#td-002-an-obo-childs-token-reaches-the-registry). After the spelling fix below, a rerun (99 s) named `list_scopes` as the only success beyond the residual. Passes with both accepted |
| `test_an_own_sp_child_reads_a_foreign_secret_exactly_while_its_service_principal_holds_read` | new; passes: refused, `READ` given with the CLI and the read succeeds, `READ` taken back and the read stays refused for 60 s without a break |
| `test_a_shared_sp_child_reads_a_foreign_secret_exactly_while_the_appmux_sp_holds_read` | new; passes the same way for the appmux service principal |
| `test_a_shared_sp_child_keeps_calling_the_api_across_a_token_expiry` | passed in 4 281 s (1 h 11 min): the child asked the API as the appmux service principal at the start and every 300 s after, for longer than the 3 600 s a minted token lives, so the file appmux keeps current was renewed |

The six tests other than the expiry run passed together in 17 min 34 s (the last run, on the head
that carries the grant tests). The expiry test is the 71-minute run above, on the earlier head; its
body has not changed since.

### Listing is visibility, the READ grant is use (the owner's question)

The `list_scopes` success is the platform's behaviour, not a leak through appmux: the throwaway
principal, asked directly with its own OAuth token, lists both scope names (the one it holds `READ`
on and the one nobody granted it) and gets `403` reading a value from either. The owner's question
was whether reading still needs the grant: refuse, give `READ`, see whether it reads. Measured with
the throwaway principal as itself (`acl-control` and `acl-lag` scripts, not committed; they print
statuses and seconds only) and then through the children:

| Step | Result |
|---|---|
| list scopes, no grant | `200`, the scope nobody granted it is in the list |
| read a value, no grant | `403` |
| `READ` granted with `databricks secrets put-acl` | the read answers `200` from the first call after it |
| `READ` taken back with `delete-acl` | the answers alternated between `200` and `403` 22 times in 281 s (first change after 1 s; the longest run of `403` before the end lasted 23 s), then stayed `403` for the remaining 319 s of a 600 s watch |

So the grant alone decides use, and the listing shows names only: accepted as the platform's
behaviour ([TD-027](../../../docs/tech-debt.md#td-027-a-childs-token-lists-the-names-of-every-secret-scope-in-the-workspace)).
The revoke lag is the one design 8.3 recorded (163 s; about six minutes); this watch adds that the
first denial is not the end of it. The run before the last one failed two own-SP tests, which fits that
lag: its first containment test read the foreign secret in a run that started shortly after a
measurement script had revoked `READ` (the minutes between them were not logged), no `READ` was
left on the scope (checked afterwards), and its grant test waited only 60 s for a refusal after its
own revoke. The tests now wait for 60 s of unbroken refusal,
within 600 s, before the grant and after the revoke, and the containment tests wait the same way
before they read the report; `RefusalWatch` carries the rule and has unit tests.

### What was wrong with the test, not the product

* The accepted residual was spelt `with_token: registry login` while the snooper reports
  `with token: registry login`, so the accepted registry login would have counted as a failure.
  `tests/unit/test_credential_modes_labels.py` runs the snooper's own report builder against the
  test's constant and failed first; then the constant was fixed.
* The failure message did not say which attempt had been refused or skipped. It now lists each
  attempt without a token with its outcome and detail, and the names of the attempts that succeeded
  with one. Names are printed without the case prefix because `mask-identifiers.sh` blanks the value
  after `token:`.
* A set-up gap, not a fault: without `APPMUX_TEST_REGISTRY_USER` (the appmux SP's application id)
  the snooper's registry login without a token is *not attempted*, and a skipped attempt fails the
  containment test by design. The remote wrapper now exports it.
* A run longer than one hour outlived the CLI token. `ops_session.TokenRenewal` asks the CLI for a
  fresh token (`databricks auth token --force-refresh`) ten minutes before the token expires, for
  every admin-API call of a run started with `APPMUX_TEST_TOKEN_PROFILE`; the wrapper sets it. The
  load generators of `test_zero_downtime` and the Lakebase test still hold only the first token.

The expiry run started with a CLI token that had 18 minutes left and ended 53 minutes after that
token would have expired, with no `302` and no refused call in the log, so the renewal worked in
practice. The log does not record the renewals themselves; that is inferred from the polls through
the proxy, which need a valid bearer token, still succeeding.

Not as written: `T147` asks the shared-SP child to keep calling "across a token expiry using the
short advertised lifetime of spike 10". The advertised lifetime of the deployed appmux is the real
hour, so the test waited it out instead.

### Other items measured on the same day

* `X-Forwarded-Preferred-Username`: with the CLI's bearer token as the owner, a child receives
  `X-Forwarded-Email` and `X-Forwarded-User` and no `X-Forwarded-Preferred-Username`; the hub's
  "anonymous" is its own placeholder for a missing header. appmux forwards the header as the
  proxy gave it, so the proxy did not send it. Why the second account's call had it was not
  isolated ([reference](../../../docs/reference/mcp-and-a2a.md#the-same-check-from-a-browser)).

## Service principal grant through the Apps proxy (2026-11-04)

ADR 0020's `service_principal` grant, measured through the real Apps proxy on the Free Edition
workspace with a throwaway service principal that is not an App (`appmux-test-own-sp`, created for
the credential-modes run). The owner asked for the measurement and for `CAN USE` on `appmux` to be
given to that one principal and taken back carefully. The paid workspace was not used.

### What ran

`tests/integration/test_service_principal_grant.py` (six tests) through `scripts/run-remote-tests.sh`.
Before the first change the dev App's direct permission list (4 entries) was saved; the only change
was adding `CAN_USE` for the principal, and the test puts the list back with `set-permissions` and
compares it entry by entry. After the run, `measure/acl-snapshot.py compare` found the list
identical to the saved one.

| Test | Result |
|---|---|
| no `CAN_USE` on appmux: refused by the proxy | passed |
| `CAN_USE` given: reaches appmux; a child with no grant is `404`, identical to an unknown app | passed; the proxy let the principal in 8 s after the permission |
| a grant for a different application id does not admit | passed (watched 12 s) |
| a grant for this principal opens the child; the child sees the application id in `X-Forwarded-Email` and no access token | passed; opened in under a second |
| removing the grant closes the child | passed; closed in under a second |
| `CAN_USE` taken back: the proxy refuses the principal again | **failed at the first limit (600 s)**, then measured separately (below) |

### How long the proxy kept admitting the principal after the revoke

The sixth test's limit of 600 s was a guess. Two probes then called the child with the principal's
token every 65 s and every 126 s:

* every answer was `200` for at least 52 minutes after the revoke, except one `502` while the dev
  App was being restarted (the restart was for the container run, not an attempt to clear the
  permission) at about 53 minutes;
* the first refusal (`302`, the sign-in redirect the proxy gives a caller it does not admit) came
  between 59 and 61 minutes after the revoke, and every later probe, up to 2 900 s further, was
  `302`;
* the revoke time itself was not reported by the platform; it was taken as the time of the
  `set-permissions` call, so the width of the interval is the probe spacing (about 2 minutes).

For contrast, a secret-scope `READ` taken back (credential-modes run, same day) flapped between
allowed and refused for about 280 s before it stayed refused. Two permissions, two lags.

### What changed in the test

The sixth test used to fail on a bound the evidence did not support. It now has a bound of 90
minutes (`REVOKE_SETTLES_WITHIN_S`), prints the seconds it measured, and is **opt-in**
(`APPMUX_TEST_WAIT_FOR_REVOKE=1`) because it holds a run for an hour. Without the variable it
still takes the permission back and checks the list, then skips the wait. It was not rerun with the
wait on: the measurement above comes from the probes. The first five tests are unchanged.

### What this does not show

One principal that is not an App, one workspace, one day. A deployed App's service principal, a
group of service principals, and a paid workspace were not tried. The ADR stays `proposed`.


## Container runs inside the dev App (2026-11-05)

Free Edition workspace only; the paid workspace was not used. Until this day every test that needs
a Databricks container had been written but never run on one, because the Apps proxy sits in front
of the service when a test calls it from outside (see
[Remote integration run](#remote-integration-run-from-the-dev-machine-2026-11-04)). These runs put
pytest **inside** the dev App's container, where a test can play the proxy by sending
`X-Forwarded-Email` itself.

### How it ran

`scripts/run-container-tests.sh <test file>` deploys the code with an overlay that adds the chosen
test file, starts pytest as a one-off process next to the service, reads the App log until the
closing line `APPMUX-CONTAINER-TESTS-DONE run=<id> exit=<n>`, and then deploys the normal code
again. Twelve invocations that day; nine produced a pytest result, and every one of those nine
ended with the restore succeeding. The other three produced none: one stopped after the deploy
while waiting for the closing line, one was killed while waiting (exit 137), and one ended with
the restore failing (exit 4; the normal deploy was then run by hand). Why the closing line did not
come is not established; the platform may have stopped the App's compute for fair usage (the dev
App's neighbour `console-solo` was found stopped with that message the same day), and it did not
happen in the nine. The runner was changed along the way: bounded log reads, a stop after ten
unreadable reads that prints the App's state, and a restore that removes the overlay files
(`tests/unit/test_run_container_tests.py`).

Six more invocations followed after the supervisor fix (the row 20 re-run and the five SC-004 ones
below); each produced a pytest result and each ended with the restore succeeding.

Three more followed for T129, T155 and T137 (a run with `test_open_modes` and `test_resources_container`, then one with `test_open_modes` and `test_offline_install`, then `test_offline_install` alone); each produced a pytest result and each ended with the restore succeeding. T177 does not run inside the container; it ran from the dev machine, see its own section.

Two more followed for T108 (`test_zero_downtime` at the full 500 requests a second, five
minutes of pytest each). The first got a pytest result with 7 passed and 1 failed: the egress edit
was refused with `403 registry_role_missing` before it ran, because the test did not send the
admin's token (a fault of the test, see the list at the end); the second passed 8. Each ended with
the restore succeeding.

### Results

| Test file (tests) | Task | Result |
|---|---|---|
| `test_self_check_fail_closed` (4) | T070 | **passed**, see below |
| `test_self_check_container` (2) | — | passed; `self_check` ran 13 checks in 6.8 s and 6.9 s |
| `test_edge_capacity` (1) | T086 | **passed**, see below |
| `test_capacity` (3) | T162 | **passed**, see below |
| `test_requests_walkthrough` (4) | T148 | passed (4) |
| `test_failure_table` (2) | T180 | row 19 passed; row 20 **failed** (the product, TD-030), then **passed** after the supervisor fix, see below |
| `test_sc004_walkthrough` (1) | T167 | **passed** on the fifth invocation, see below |
| `test_resources_container` (4) | T129 | **passed** (4), see below |
| `test_open_modes` (3) | T155 | **passed** (3) on the second invocation, see below |
| `test_offline_install` (4) | T137 | **passed** (4) on the third invocation, see below |
| `test_ui_on_container` (7) | T177 | **passed** (7), from the dev machine through the real proxy, see below |
| `test_run_dir_cap` (2) | T060 | **passed** (2) on the first invocation, from the dev machine through the real proxy, see below |
| `test_zero_downtime` (8) | T108 | **passed** (8) on the second invocation, see below |

### SC-002: a weakened sandbox fails closed (T070)

The control run, with nothing left out, failed no check and the gate let the request for an app
through. Leaving one layer out failed the check that layer guards, and the gate answered `503` for
the app, named the check on the status page, and still let `/_appmux/` through:

| Layer left out | Checks that failed |
|---|---|
| `/proc` overmount | `platform_files` |
| capability drop | `capabilities` |
| pump filter | `loopback_and_container_ip`, `metadata_address` |

### SC-001: the edge at 500 requests a second (T086)

15 000 requests in 30 s at 500 a second over 10 connections, to one sandboxed `light_fastapi` through
the edge: **0 failed requests**. Latency p50 1.77 ms, p95 3.98 ms, p99 44.35 ms, max 122.97 ms. The
edge process used 18.7 s of CPU in the 30 s (**62.3% of one core**); the load generator used 2.4 s.
The test asserts no failures and at least 95% of the rate, and sets no limit on p95 or CPU: whether
62% of a core is "room to spare" for [ADR 0014](../../../docs/decisions/0014-build-the-edge-on-the-environments-http-stack.md)
is the owner's judgement and is not made here. One workload (an empty `GET`), one container, one run.

### SC-001: no disruption under operations (T108)

`test_zero_downtime` inside the dev App, 2026-11-05 (Free Edition only): app A (`light_fastapi`, an
empty `GET`) under 500 requests a second over 10 connections, in 5 s slices. A control run of 60 s
does nothing; then each of six operations is run with 10 s of load before it and the load going on
while it runs and settles. **145 000 requests were sent in all and none failed**, in the control or
in any operation. Edge CPU was read from the edge process. The p95 figures are the worst and the
median of the 5 s slices; the test asserts that no operation raised, that requests were answered and
that failures do not pass the control's, and sets no limit on p95.

| Operation (took) | Failed requests before / during | Worst slice p95, before → during | Median slice p95 during | Edge CPU during |
|---|---|---|---|---|
| control, nothing (60 s) | 0 / 0 | 10.1 → 103.9 ms | 3.7 ms | 40.8 s in 60.0 s (68 % of a core) |
| register B (0.2 s) | 0 / 0 | 3.7 → 38.4 ms | 21.3 ms | 6.8 s in 10.0 s |
| update B (4.2 s) | 0 / 0 | 47.5 → 216.6 ms | 110.1 ms | 7.9 s in 10.0 s |
| update A with a good release (12.9 s) | 0 / 0 | 28.2 → 180.5 ms | 4.9 ms | 36.7 s in 50.4 s (73 %) |
| update A with a release that fails its health check (63.5 s) | 0 / 0 | 36.0 → 346.3 ms | 27.3 ms | 53.3 s in 70.1 s (76 %) |
| a heavy build in parallel (6.1 s; three Updates of `wheels_only`) | 0 / 0 | 15.8 → **1 877.3 ms** | 970.1 ms | 10.2 s in 11.8 s (86 %) |
| an egress rule edit (0.3 s) | 0 / 0 | 7.4 → 25.8 ms | 18.9 ms | 7.6 s in 10.0 s |

What it shows, and what it does not:

* The criterion as the task states it (no more failed requests than the control) holds for every
  operation, the failed-health one included: A kept serving while its new release failed.
* The heavy build is the one operation that costs latency you can see: the median slice p95 went
  from 3.7 ms (control) to 970 ms and the worst to 1.9 s, with the edge using 86 % of a core. Three
  builds installing wheels at once share the container's quota of about 2 CPUs with the edge. The
  first invocation, whose egress edit was refused, measured the same five operations again with no
  failed request and a worst slice p95 of 2 137 ms for the heavy build, so this is not one bad run.
  Whether that is "room to spare" for [ADR 0014](../../../docs/decisions/0014-build-the-edge-on-the-environments-http-stack.md)
  is the owner's judgement; no figure for it is written down. The CPU that builds may take is
  [TD-003](../../../docs/tech-debt.md#td-003-cpu-and-memory-limits-are-best-effort).
* The control's own worst slice (104 ms against a median of 3.7 ms) shows how much one slice of this
  container moves with nothing happening, so a single slice of 40 to 350 ms is within that.
* One workload, one container, two invocations of the same code, the platform's quota as measured
  on Free Edition. The company's paid workspace was not used.

### SC-003: ten apps and an eleventh that is refused (T162)

Ten apps in four kinds, started and loaded at 30 requests a second each, beside five apps that were
already running:

| Kind | Apps | Budget | Start | App RSS idle | Sandbox RSS loaded | CPU idle / loaded (of one core) | p95 loaded |
|---|---|---|---|---|---|---|---|
| `light_fastapi` | 4 | 256 MB | 3.2–5.0 s | 45.6–45.8 MB | 106.5–106.8 MB | 0.1–0.2% / 1.4–1.5% | 2.7–3.3 ms |
| `memory_fastapi` | 2 | 512 MB | 2.0–2.5 s | 110.1–110.2 MB | 171.0–171.2 MB | 0.2% / 2.0% | 3.1–3.3 ms |
| `streamlit_app` | 2 | 512 MB | 2.5–2.9 s | 51.9–52.3 MB | 113.1–113.4 MB | 0.1% / 2.0% | 3.0–3.6 ms |
| `node_app` | 2 | 768 MB | 2.1–2.5 s | 125.0–125.5 MB | 194.5–195.3 MB | 0.1% / 1.5% | 2.5–2.6 ms |

* **Per-sandbox overhead** (the sandbox's process tree minus the app's own RSS): 60.7–61.1 MB for
  every app, whatever its kind. Admission control assumes 13 MB
  (`OVERHEAD_PER_CHILD_MB` in `src/appmux/reconciler/admission.py`, whose comment says this test
  replaces it with the real figure). It is **not changed here**: see the next list.
* **The slot with ten apps loaded**: ten sandboxes 1 385 MB RSS in total; everything appmux runs
  2 151 MB RSS; the container reported 1 905 MB in use; the edge held 138 MB and used 1.8% of a core
  idle and 36.9% loaded. The ledger read limit 8 192 MB, reserve 1 024 MB, committed 6 992 MB,
  free 176 MB.
* **The eleventh app**: with the reserve raised so that 100 MB stayed free, an app needing 269 MB
  was refused, its release `failed` with `not enough memory: needs 269 MB, 100 MB free`.
* **Estimate, not a measurement**: 100 apps at the mean loaded size of these ten would hold about
  13 990 MB RSS, against a limit of 8 192 MB. Linear, and only for the case where all 100 run at
  once; the ledger decides how many may.
* **Reading the overhead gap** (inference): the ledger counts budgets, and these apps used far less
  than their budgets, so the 13 MB did no harm here. If every running app used its whole budget,
  each would also cost about 48 MB more than the ledger counts: 480 MB at ten apps, which the 1 024 MB
  reserve covers; at about twenty-two it would not. Changing the constant moves admission, the
  budget floor and the tests that pin them, so it waits for the owner.
* **Follow-up, 2026-11-05:** the owner asked for the fix, and `OVERHEAD_PER_CHILD_MB` is now 64
  (61.1 MB rounded up to the next multiple of 16 MB). The paragraphs above stay as the record of
  what the run found and what admission assumed at the time; see
  [TD-032](../../../docs/tech-debt.md#td-032-admission-assumes-13-mb-of-sandbox-overhead-and-the-measured-figure-is-61-mb).

### Failure table rows 19 and 20 (T180)

* **Row 19, a child allocating without end**: stopped at 288 MB held under a 512 MB address-space
  limit (a 256 MB budget), by `MemoryError`, not by a watchdog. The RSS watchdog is not started by
  the service at all ([TD-029](../../../docs/tech-debt.md#td-029-the-rss-watchdog-is-written-and-tested-but-the-service-never-starts-it)),
  so a hog that spreads over several processes was still unmeasured at this point; it was measured
  after the watchdog was wired in, the same day (TD-029).
* **Row 20, `slirp4netns` killed**: 90 s later the child was still `running` in the same sandbox
  with no network helper, and the supervisor had noticed nothing. The design says this is a crash of
  that child. **The test failed and was left failing**:
  [TD-030](../../../docs/tech-debt.md#td-030-a-child-whose-slirp4netns-dies-keeps-running-with-no-way-out)
  was confirmed on a real container, not only read from the code.
* **Row 20 after the fix (same day)**: the supervisor now ends a sandbox whose `slirp4netns` or frame
  pump has stopped. In the container the reconciler logged `slirp4netns was killed by SIGKILL, so the
  child has no network`, closed the route and `restarting ... in 1 s`; `1 passed, 1 deselected in
  10.28 s` (Free Edition only). The test's own `noticed=` line was not captured (pytest hides the
  output of a passing test), so no figure for the time to notice is recorded.

### SC-004: the scripted walkthrough (T167)

`1 passed in 79.37 s` (Free Edition only; the platform admin was the token's owner, Grace and the
people she shares with were placeholder addresses sent as `X-Forwarded-Email`). All ten steps of the
criterion ran through the admin API with no SQL typed: register directly; request a new app and
approve it; request and approve OBO delegation, shared-SP delegation and a settings change (display
name and memory budget); rename by request; ask for a slug that was free and was taken when the
admin pressed Approve (closed as denied by itself); an egress rule; open another team's app as
a platform admin and read its snapshot source; share with a user, a Databricks group and an app admin;
attach two of the three offers of a `databricks.yml` (a secret in a scratch scope and a SQL warehouse),
leaving the third `missing`; update, tag, label, lock, unlock, activate the older release and delete
the newer; restart and stop. The final state, read through the API, matched: one release left, named
`first`, tagged `stable`, unlocked; the owner and the raised budget as approved; every request ended
in the status the script expected; and the audit rows the steps must write appear in the log in
order. The test deleted its apps afterwards and put the platform setting back; no walkthrough app was
left on the dev App.

It took five invocations; the first four each found a defect in the **test**, none in the product:

| Invocation | Stopped at | Cause |
|---|---|---|
| 1 | register (step 1), `403 user_token_missing` | the script sent the admin's token only on Update and attach |
| 2 | the platform setting (step 3), `403 registry_role_missing` | same: the row kind is writable only by the admin's database role |
| 3 | the app's egress rule (step 5), `403 registry_role_missing` | same; `apps` rows are admin-role writes |
| 4 | all ten steps passed, then the clean-up raised `ReadTimeout` | deleting a running app takes about 33 s ([TD-031](../../../docs/tech-debt.md#td-031-deleting-a-running-renamed-app-took-33-seconds)), the call allowed 30 s; the app after it stayed (deleted by exact slug) |
| 5 | **passed**, no error, no leftover app | the token now goes with every call of the admin (as the real proxy does); the delete waits 180 s and a timeout is a warning |

What this does not show: `attach` answered `ok` or `unverified` for each attached resource (the test
accepts both and does not print which), so whether the attach check reached the warehouse in this run
is not recorded. The walkthrough ran once on one container; it is a pass of a script, not a count of
runs.

### Request walkthrough (T148)

Four tests passed: a request approved into an app owned by the requester, stopped with no release;
an OBO request; a taken slug at approval; and the rename of a running app (restart under the new
prefix, old URL `404`). One observation without a cause: deleting the **running, renamed** app took
33.0 s, while deleting the stopped ones took 0.1 s each, and the stop grace is 10 s. The test's
cleanup now allows 180 s for a delete and carries on when one times out.

### Resources, open modes and offline install (T129, T155, T137)

Free Edition only; the scratch apps were registered by name on the dev App and deleted at the end.

* `test_resources_container` (T129): 4 passed on the first invocation, with a scratch secret scope,
  a SQL warehouse and the CLI token's owner as the person. Only its docstring changed.
* `test_open_modes` (T155): 3 failed on the first invocation and 3 passed on the second. The
  failures were in the test (see the list below). The scratch app was owned by someone else and
  delegated OBO, so the dev App's own delegation was not touched.
* `test_offline_install` (T137): the second invocation passed one of four tests and failed three;
  the third passed all four (`package_mode` `wheelhouse`, four scratch apps). One failure was the
  runner (the fixtures' `requirements.txt` was not in the overlay) and one was a defect in the
  product, below. No platform setting was changed: `wheelhouse` is the default.

**A product defect: a signal killed the supervisor before it was ready.** A `release` of
`newer_wheel` failed with `the sandbox supervisor ended with status -10 before the sandbox
started`. `SandboxHandle` (`sandbox/launcher.py`) sent `SIGUSR1` (the metrics sampler reads the pump's
counters with it) and `SIGHUP` (reload the egress rules) to a supervisor that had not yet reported
`started`; before that it has not installed its handlers, and the default action of both signals ends
the process (status -10 and -1). The sampler asks every child that is coming up, so any release that
was starting during a sample could hit it. Why it first showed on this run is not established. The fix: `read_stats` answers `None` and `reload_rules` keeps the
request until the supervisor reports `started`, two tests first
(`tests/unit/test_sandbox_launcher.py`). The race depends on timing, so one pass after the fix is
not proof of its absence; the unit tests are what pins the signalling rule. Run C passed 4 of 4
after it.

### UI pages through the real proxy (T177)

`7 passed in 174.60 s`, from the dev machine through the Apps proxy with
`scripts/run-remote-tests.sh tests/integration/test_ui_on_container.py -s` (Free Edition only; the
browser is the dev machine's own Chromium driven by `ui_browser_check.mjs`, and it sends the token's
owner's bearer token on same-origin requests only, because from outside the proxy adds the person's
identity itself). The seven tests: every page's CSP allows exactly the inline scripts it has; the
isolation headers (COOP on pages, `nosniff`, no CORS header) are present; no page names another origin
to fetch and every asset it names is served; an unknown path under the UI prefix is the 404 page with
a policy; the browser loaded every page with no CSP violation, page error or console error; the
browser sent no request to another origin; and the launcher, the platform page and the dashboard poll
their API every few seconds (at least three refreshes, each 1 to 15 s apart).

What the first runs found, all in the **test**:

* COOP was asserted on API answers too. The spec asks for it on the UI (FR-021, design 7.3), and the
  API conventions pin only `Cache-Control: no-store` and `nosniff`. COOP is now asserted on pages.
* The dashboard polled "3 ms apart": one refresh reads its window and the window before it with
  `Promise.all` (`web/src/components/dashboard/metrics-panel.tsx`), so every refresh is two
  `/metrics` calls started together. Calls within 100 ms of the first call of a refresh now count as
  one poll (`SAME_REFRESH_WINDOW_MS`; the window runs from the first call so a loop asking every few
  tens of ms is still a burst). The example of a burst in the unit test moved from 50 ms to 300 ms
  for that reason.
* Chromium cancels requests in flight with `net::ERR_NETWORK_CHANGED` whenever the machine's network
  interfaces change. The dev machine is a Docker host where another project's container restarts
  all day and creates and deletes interfaces, so 3 pages failed to load whole on the first
  run and 5 of 18 on the second even after retries. This is not a fault of the page or the proxy.
  The test now retries a page whose load was cut that way up to five times and, if it is cut every
  time, reports **no verdict** for the page and does not pass it. The passing run was made with a
  local, uncommitted `LD_PRELOAD` shim that refuses Chromium's `NETLINK_ROUTE` socket so it assumes a
  steady network. A reader who repeats the run on a quiet machine needs no shim; one on a machine like
  that one needs it or a fix of the churn.

What this does not show: Chromium only (no Firefox or WebKit), the pages of the export as it was
built that day, one workspace. The test does not run the pages of an app's own UI, which belong to the
child.

### The run directory's cap (T060)

Free Edition only; two scratch apps of the `disk_filler` fixture were registered on the dev App and
deleted at the end. The run was `scripts/run-remote-tests.sh tests/integration/test_run_dir_cap.py`
from the dev machine (350 s, most of it waiting for per-minute figures), against the code of
`55269a5`.

* The cap holds. `disk_filler` wrote into `/run/appmux` until the filesystem refused: 67 108 864
  bytes, exactly 64 MiB, in 0.018 s, refused with `ENOSPC`; `statvfs` read a 64 MiB filesystem; the
  app answered `200` afterwards. (Spike 7's host-backed directory took 1 GiB in 1.5 s.)
* The pages are memory, in the container and not in the child. With the 64 MiB kept in the file, the
  `_platform` row's `mem_used_mb` peak went from 1 021 to 1 089 MiB (+68) in the next whole minutes,
  and the child's `rss_mb_max` from 72 to 73 MiB. The test asserts that answer (the container grows
  by at least half of what was written; the child's figure by at most 16 MiB), so a platform change
  that moves it fails the test. One run; the container's figure includes whatever else ran in it.
* Not run: the size watchdog stopping a shared-service-principal child that overfills its
  host-backed directory (that directory has carried the child's token file, T147), and the other
  four `tmpfs` mounts of a sandbox. What this means for the memory budget is in
  [TD-004](../../../docs/tech-debt.md#td-004-a-childs-run-directory-size-cap-is-unproven).

### What was wrong with the tests, not the product

Each of these showed up as a failure on the first container run and is fixed:

* In a container a test plays the proxy, and the proxy adds the person's access token only when the
  test sends one. Registering an app, deleting one and changing a platform setting need that token
  (`403 user_token_missing` without it), and the shared session did not send it
  (`ops_session.py`, `test_capacity.py`, `test_requests_walkthrough.py`). From outside the container
  the real proxy always adds the token, which is why the remote run could not show this.
* `test_self_check_fail_closed` expected only the app path to reach the application in the control
  run; the harness also asks for the admin path, and with the self-check passing both get in.
* The first capacity run was refused with `not enough memory`: five apps left over from 2026-11-04
  held 538 MB of the ledger. They were deleted by exact slug and the run was repeated.
* A delete that timed out aborted the walkthrough's cleanup and left three apps behind; they were
  deleted by exact slug and the cleanup is now best-effort.
* `test_open_modes` asked the child straight after the restart and got `503` (the child was still
  starting); it now waits for a `200` first. It also sent a made-up user token, which appmux's group
  lookup rightly refused (`GROUPS_UNKNOWN`, `403`); it now uses the CLI token's owner's real token.
* The runner's overlay left out the fixtures' `requirements.txt` files, which
  `test_offline_install` compares with what a release recorded (`scripts/container_test_run.py`;
  the overlay still carries none of the fixtures' code).
* The SC-004 script sent the admin's token only on some calls (see the table above); it now sends it
  on every call made as the admin, and never as anybody else (`tests/unit/test_sc004_script.py`
  pins both). Its clean-up now waits 180 s for a delete and reports a timeout as a warning.
* The egress edit of `test_zero_downtime` sent no token, so the first full-rate invocation got
  `403 registry_role_missing` (a rule list is written under the admin's own database role) and the
  operation never ran; the other five had passed in the same invocation. The step now sends the
  admin's token and the second invocation passed all eight.

### What did not run

Every container-only test file has now run at least once, so no file waits on a missing set-up. What
the runs still do not show:

* The dev App's own OBO delegation was never changed (the open-modes test uses a scratch app), and the
  offline-install test used the default `package_mode` `wheelhouse`, so the other package modes have
  no container result.
* `test_failure_table` rows other than 19 and 20 are covered elsewhere (its docstring lists where).
* Everything above is the Free Edition workspace on one day; the company's paid workspace was not
  used.

### Clean-up

At the end of the day the throwaway service principal (`appmux-test-own-sp`) and the two scratch
secret scopes (`appmux-t-own-sp`, `appmux-t-foreign`) made for the credential-modes and
service-principal-grant runs were deleted, and the workspace was listed again: no scope and no such
principal left. Before that, the dev App's own permission list was compared with the one saved before
the grant run: 4 entries, identical. The dev App runs the normal code.

## Plan checks: the quickstart run (2026-11-05)

Free Edition workspace only, one container, one day; the company's paid workspace was not used.
This is [quickstart.md](../quickstart.md) run once, in order, on one head (T181): `4733026`. Its
`src/`, `web/`, `scripts/` and lock files are those of `c2f143e` (the commits after it change docs
and `tests/integration/test_capacity.py` only), so a figure below was not measured on an older
build than the one that is deployed. The order and what each part was:

1. **Local checks** on the head: `ruff format --check` and `ruff check`, `mypy src`, the Python
   tests, then the seven UI checks.
2. **Deploy and `self_check`, capacity of the real edge, SC-001, SC-002, SC-003, SC-004:** one
   invocation of `scripts/run-container-tests.sh` with the six container files in quickstart order.
   It put the tests over the code on the dev App (one restart: every hosted app was down for the
   minutes a start takes), ran them inside the container, and put the normal code back
   (`SUCCEEDED`). **19 passed in 532.55 s, runner exit 0**, and because every test passed the runner
   printed the whole output, which is where the figures below come from.
3. **Hostile fixtures,** from the dev machine through the Apps proxy as the CLI token's owner, in
   three invocations of `scripts/run-remote-tests.sh`, and one more container run for the test
   that kills `slirp4netns`; see [hostile fixtures on this head](#hostile-fixtures-on-this-head).

The invocation before it, on the head `30c7157` the same afternoon, ended `16 passed, 3 errors`: the
three SC-003 tests failed in setup with `not enough memory: needs 832 MB, 192 MB free`. That was the
test's precondition, not the product; see [the ledger needed room](#sc-003-on-this-head-the-ledger-needed-room).
Its output was not kept (a failing runner prints only the tail), which is why the whole set was run
again.

| Quickstart step | Result (Free Edition only) | Where |
|---|---|---|
| 1 Local checks | `ruff format --check` (404 files) and `ruff check` clean; `mypy src` clean (129 files); `pytest tests/unit tests/contract tests/load`: **7 997 passed**, 1 warning, 132 s. UI: typecheck, lint and format clean; 2 886 unit and component tests (158 files); build ok; 63 export tests; 810 end-to-end tests (5.5 min) | this section |
| 2 Deploy | the runner's overlay deploy and its restore both ended `SUCCEEDED` (the restore after 21 of 60 checks); the startup gate was seen answering during a `sync-deploy.sh` earlier the same day, see below | [below](#the-startup-gate-during-a-deploy) |
| 2 `self_check` | all 13 checks `ok`: 7.0 s and 6.9 s inside the App (the two tests of `test_self_check_container.py`) | [T068](#container-runs-from-the-dev-machine-2026-10-31), [container runs](#results) |
| 3 Capacity of the real edge | 15 000 requests in 30.2 s at 496.9 a second: 0 failed; p50 1.97 ms, **p95 247 ms**, p99 411 ms, max 595 ms; the edge used 74.9 % of one core. [T086](#sc-001-the-edge-at-500-requests-a-second-t086) measured p95 3.98 ms and 62.3 % with the same test; see [the edge at 500 a second, twice](#the-edge-at-500-a-second-twice) | this section |
| SC-001 No disruption | 145 000 requests at 500 a second across a 60 s control and six operations: **0 failed in every one**. Worst slice p95 during: control 44.5 ms, register B 27.9, update B 223, good release of A 511, failed-health release of A 364, **heavy build 2 240 ms** (median slice 1 144 ms, edge at 85 % of a core), egress edit 9.6 | [T108](#sc-001-no-disruption-under-operations-t108), [TD-003](../../../docs/tech-debt.md#td-003-cpu-and-memory-limits-are-best-effort) |
| SC-002 Fail-closed isolation | control (healthy sandbox): no check failed. Each layer left out failed the check it guards: `platform_files`, `capabilities`, `loopback_and_container_ip` with `metadata_address`; the test also asserts the app's route answers `503` with the check named | [T070](#sc-002-a-weakened-sandbox-fails-closed-t070) |
| SC-003 Capacity | ten apps in four kinds started in 1.7–5.6 s each and were loaded; sandbox overhead 60.8–61.2 MB each; the eleventh refused with `not enough memory: needs 320 MB, 100 MB free`; 100 apps is an estimate, not a measurement | [below](#sc-003-on-this-head-the-ledger-needed-room), [T162](#sc-003-ten-apps-and-an-eleventh-that-is-refused-t162), [TD-032](../../../docs/tech-debt.md#td-032-admission-assumes-13-mb-of-sandbox-overhead-and-the-measured-figure-is-61-mb) |
| SC-004 No hand-edited database | the ten-step walkthrough passed through the admin API, no SQL typed (it passed in this invocation and in the one before it) | [T167](#sc-004-the-scripted-walkthrough-t167) |
| 5 Hostile fixtures | **31 tests passed** in four runs, none skipped: the environment snooper, the package-environment writer (`EROFS`), the fork bomb (247 of 256 forks, then `EAGAIN` after 0.3 s), the network scanner and the metadata address, a deny rule and its live reload, a start-up crash, a crash loop with a rollback, an always-500 health check, slow starts, a memory hog stopped by `MemoryError` at 224 MB and, over several processes, at 632 MB against a limit of 384 MB, a 64 MiB run directory refused with `ENOSPC`, a child whose `slirp4netns` is killed, and the secret-and-registry snooper in both credential modes. Neighbour p95 237 ms idle, 248 ms beside the hostile children | [below](#hostile-fixtures-on-this-head) |

### Hostile fixtures on this head

Each fixture is registered through the normal path and has to be contained without touching a
neighbour. Free Edition only; the figures that come through the Apps proxy include that path.

| Run | Where | Result |
|---|---|---|
| `test_hostile_fixtures.py` | dev machine, through the proxy | 7 passed in 219 s |
| `test_host_a_child.py` (10), `test_crash_and_rollback.py` (2), `test_failure_table.py` (2), `test_run_dir_cap.py` (2) | dev machine, through the proxy | 16 passed, 8 errors, 825 s; the errors are set-up errors, below |
| the `slirp4netns` kill (`test_failure_table.py`) | inside the dev App, `run-container-tests.sh` | 1 passed in 10.68 s, runner exit 0; the child was reported as killed by SIGKILL and restarted, and the neighbour went on answering |
| `test_credential_modes.py` | dev machine, with two throwaway secret scopes and a throwaway service principal | 7 passed in 5 276 s (88 min, of which the token-expiry test is about 71) |

The 8 errors of the second run were not product failures: all eight ended in *setup*. One was the
`slirp4netns` test, which needs `APPMUX_TEST_EDGE_PID` and so can only run inside the container (the
third row ran it there). Seven were the credential-mode tests, which need the throwaway scopes and
service principal that `APPMUX_TEST_OWN_SP_*` and the registry variables name (the fourth row ran
them with those). Nothing was counted as passed that did not run.

What the figures say, one line each:

* **Environment snooper:** `/proc` readable, pid 1 `ENOENT`, `runtime.cfg` `ENOENT`, 0 other pids visible.
* **Package-environment writer:** both attempts `EROFS`.
* **Fork bomb:** refused with `EAGAIN` after 247 of 256 forks (the platform's per-child process limit), in 0.319 s, and the child itself stayed running.
* **Network scanner:** the metadata address timed out; a deny rule removed the destination it named
  (before `1.1.1.1:443`, after nothing), and so did the same rule pushed to a running pump (19 drops
  counted, by rule: `app:1` 2, `global:2` 4).
* **Memory:** an allocation past the budget stopped at 224 MB with `MemoryError` and the neighbour kept
  answering; workers holding 600 MB between them were stopped by the watchdog when it read 632 MB,
  over a limit of 384 MB (256 MB budget plus 64 MB for the sandbox; [TD-029](../../../docs/tech-debt.md#td-029-the-rss-watchdog-is-written-and-tested-but-the-service-never-starts-it)).
* **Run directory:** 67 108 864 bytes written in 0.021 s, then `ENOSPC`; the container's memory rose by about
  as much and the child's own figure did not ([TD-004](../../../docs/tech-debt.md#td-004-a-childs-run-directory-size-cap-is-unproven)). Only bytes were tested, not files.
* **Failed releases:** a child that exits at start, one that answers 500 and one that binds its port
  after the deadline each failed the release and kept the route closed; one that binds within the
  deadline became active; a crash loop ended `crashed` with only its own route at 503; a rollback to
  release 3 of 5 ran from the stored snapshot.
* **Secret-and-registry snooper:** in both credential modes the test passes only when nothing succeeds
  beyond the one attempt the design accepts for a shared-service-principal child; a shared-SP child kept
  calling the API across a token expiry.
* **Neighbour latency:** with the fork bomb and the scanner running beside it, every answer was `200`
  and p95 was 248 ms against 237 ms idle, read from the dev machine through the proxy, so the two numbers say how the neighbour changed and the 240 ms says how far the proxy is.

### The startup gate during a deploy

[Quickstart step 2](../quickstart.md#2-deploy-and-self_check-databricks-container) expects `/` to
answer `200` "starting" and every other path `503` while the service starts. Until this run that
was contract-tested against the gate in process (`tests/contract/test_startup_gate.py`) and never
seen on a container.

How: `scripts/sync-deploy.sh` redeployed the same code to the dev App while a read-only script (kept
in the session's scratch folder, not committed) fetched `/` and `/_appmux/api/v1/me` through the Apps
proxy every 2 s with the CLI's token and logged each change in the pair of answers. The script
labelled a body by the words it held; no body, header or address was kept.

| Seconds into the poll | `/` | `/_appmux/api/v1/me` | Who answered |
|---|---|---|---|
| 2.1 | 200 | 200 | appmux, open (before the deploy restarted anything) |
| 200.9 | 502 | 502 | the Apps proxy's "not available" page (labelled by its words) |
| 277.5 | **200**, body says "starting" | **503**, body says "starting" | appmux's startup gate |
| 287.9 | 200 | 200 | appmux, open |

What it shows:

* The expected answers held for the two paths polled: `200` on `/` and `503` on the API path while
  the service starts, then both `200`.
* "From the first seconds of the start" holds for the process, not for the restart as a whole: the
  proxy answered `502` for about 77 s (give or take the 2 s step) before appmux could answer at all,
  and the gate was visible for about 10 s. appmux does not control the first of those.
* No `502` came between the `starting` answer and the open one, so the platform did not restart the
  App in that window. The poll was stopped as soon as the gate opened, so the log says nothing about
  the minutes after it.

What it does not show: the `Retry-After` header, the other paths and methods (the contract test
covers them), and a `blocked` phase on a container beyond what the SC-002 run recorded. One deploy,
one poll.

### SC-003 on this head: the ledger needed room

`test_capacity.py` registers ten apps and expects the ledger to admit all of them. Since
`OVERHEAD_PER_CHILD_MB` became 64 ([TD-032](../../../docs/tech-debt.md#td-032-admission-assumes-13-mb-of-sandbox-overhead-and-the-measured-figure-is-61-mb)),
the ten cost 5 248 MB of the ledger (4 x 320, 2 x 576, 2 x 576, 2 x 832). The dev App was hosting
six other apps that held 2 560 MB, and the default reserve is 1 024 MB, so 4 608 MB were free: the
last node app was refused (`needs 832 MB, 192 MB free`) and the three tests errored in setup. The
ledger did what it is for; the test had assumed room the dev App no longer has.

The test now reads the ledger first, lowers `memory_reserve_mb` by the shortfall (plus the 100 MB it
leaves free after the ten), prints that it did so, and puts the original value back when the module
ends. In this run it went from 1 024 MB to 284 MB. If even a reserve of 1 MB could not make room it
fails with the numbers instead of dropping an app that was already there.

A fresh review of the branch found two weaknesses in that clean-up, both now handled and tested in
`tests/unit/test_capacity_reserve.py`: if putting the reserve back failed (an expired token), the
ten apps were not deleted, and they are now deleted either way; and the arithmetic of the shortfall
had no test of its own. What stays: a run killed before it gets to the clean-up leaves the reserve
lowered, and the `reserve_lowered_for_the_run` line of its output names the value to put back.

Ten apps in four kinds, started and loaded at 30 requests a second each, beside six running apps:

| Kind | Apps | Budget | Start | App RSS idle | Sandbox RSS loaded | CPU idle / loaded (of one core) | p95 loaded |
|---|---|---|---|---|---|---|---|
| `light_fastapi` | 4 | 256 MB | 3.7–5.6 s | 45.5–45.7 MB | 106.4–106.7 MB | 0.3–0.4% / 2.1–2.2% | 3.8–6.0 ms |
| `memory_fastapi` | 2 | 512 MB | 2.4–3.0 s | 110.0–110.5 MB | 170.9–171.3 MB | 0.3–0.4% / 2.8–3.0% | 5.0–6.6 ms |
| `streamlit_app` | 2 | 512 MB | 1.7–3.1 s | 52.2–52.3 MB | 113.6–113.7 MB | 0.2% / 2.7–2.8% | 5.0–5.5 ms |
| `node_app` | 2 | 768 MB | 2.1–2.6 s | 122.9–124.5 MB | 192.8–194.3 MB | 0.2–0.3% / 2.1–2.2% | 4.6–4.7 ms |

* **Per-sandbox overhead:** 60.8–61.2 MB for every app, the same as in T162, against the 64 MB
  admission now charges.
* **The slot with ten apps loaded:** ten sandboxes 1 383 MB RSS in total; everything appmux runs
  2 159 MB RSS (a sum of RSS, so shared pages count more than once); the container reported 1 956 MB
  in use; the edge held 149 MB and used 6.2% of a core idle and 44.7% loaded. The ledger read limit
  8 192 MB, reserve 284 MB, committed 7 808 MB, free 100 MB.
* **The eleventh app:** its release `failed` with `not enough memory: needs 320 MB, 100 MB free`,
  its route stayed closed and the ten kept answering (the test asserts all three).
* **Estimate, not a measurement:** 100 apps at the mean loaded size of these ten would hold about
  13 977 MB RSS against 8 192 MB; linear, and only for the case where all 100 run at once.
* **Against T162:** the sandbox figures agree to within a few MB and the p95 is 1 to 4 ms higher.
  CPU is higher on every line (idle 0.2–0.4% against 0.1–0.2%, the edge 6.2% idle against 1.8%),
  which fits a container still busy after its restart; that is a reading of the numbers, not
  something this run tested.

### The edge at 500 a second, twice

`test_edge_capacity.py` (500 requests a second over 10 connections for 30 s, one `light_fastapi`
through the edge) has now run in two invocations. Both served the whole load with no failed request.

| | T086 | the in-order run |
|---|---|---|
| Requests, failed | 15 000, 0 | 15 000, 0 |
| p50 / p95 / p99 / max | 1.77 / 3.98 / 44.35 / 122.97 ms | 1.97 / **247.49** / 410.72 / 595.2 ms |
| Edge CPU | 62.3% of one core | 74.9% of one core |

The medians agree and the tail does not. About six minutes later in the same container the SC-001
control put the same load on the same kind of app and read a median 5 s-slice p95 of 5.2 ms and a
worst of 44.5 ms, so the edge can serve the rate with a p95 of a few milliseconds. The in-order run
read its 247 ms in the first minutes after the App's restart, when the six other apps were also
starting; that is a candidate, not a finding, because nothing here sampled the container during those
minutes. The test asserts the rate and no failures and sets no limit on p95, so it passed both times.
Whether the edge has "room to spare" under [ADR 0014](../../../docs/decisions/0014-build-the-edge-on-the-environments-http-stack.md)
stays the owner's judgement; this is a second figure for it, not an answer.

### The admin UI by hand through the real proxy (T205)

Free Edition only, 2026-11-05. Chromium (Playwright) was driven from the dev machine against the
deployed dev App through the Apps proxy, signed in as the CLI token's owner, who is a platform
admin. Two scripted passes covered the eleven points of the owner's feedback on the overhaul, in
the light and the dark theme and at a mobile width (390 px), and the screenshots were read. Across
both passes: **0 CSP violations, 0 page errors, 0
console errors.** The screenshots stay outside the repository because they show e-mail addresses.

| Point | What was checked | Result |
|---|---|---|
| 1 Principal picker | the combobox suggests principals as it is typed in | ok |
| 2 Folder browser | it opens beside the source-folder field | ok |
| 3 Motion | the spinner and the skeleton animate with `prefers-reduced-motion` set to `reduce` and to `no-preference`, which is what [ADR 0021](../../../docs/decisions/0021-ignore-prefers-reduced-motion-in-the-admin-ui.md) decided | ok |
| 4 Logs | toolbar and lines; every button has an accessible name (the copy button is icon-only: "copy the shown lines") | ok |
| 5 Console-style layout | ten app sections as a sidebar, one page each (10 of 10 loaded); five platform areas (5 of 5); the app switcher in the breadcrumb; the launcher and an app overview at 390 px in both themes | ok |
| 6 Resources | the Add resource panel asks type, object, permission, mode and key | ok |
| 7 Theme | dark, light and system; the choice survives a reload | ok |
| 8 One bar geometry | top navigation, sidebar items and tabs share one `bar-item` rule: same font size (14 px), weight (500) and 12 px side padding, no underline; the vertical padding is 10 px for the top bar and the tabs and 8 px for the sidebar | ok, but two padding sets, not literally one geometry |
| 9 Select | no native `select` on any page; the level menu opens as a popover list | ok |
| 10, 11 Dashboard | full page at 1 hour and at 7 days, and in the dark theme | ok; the first scripted check read 0 chart groups with a wrong selector, which was the script and not the page |

What this does not show:

* There are no "before" screenshots, so nothing here compares the new pages with the old ones.
* The signed-in user was a platform admin. A plain visitor, an app admin, and a browser session with
  its own token (the folder browser and the pickers read with it) were not driven.
* The dashboard showed a notice of three minutes without data at one point; it came from the
  container restarts of the same day. A week of real data has not been seen.
* The 7-day view draws 3-hour columns.

### What the quickstart still leaves open

* The paid workspace was not used, so every figure above is Free Edition's quota and platform.
* The 100-app figure is a linear estimate from ten apps.
* A fast allocator that reaches the container's memory limit between two one-second readings of
  the RSS watchdog is not measured; a hog over several processes was stopped by the watchdog at 491 MB
  in the earlier run and at 632 MB in this one, against its limit of 384 MB, so the overshoot is not a fixed
  figure (two readings, not a distribution) ([TD-029](../../../docs/tech-debt.md#td-029-the-rss-watchdog-is-written-and-tested-but-the-service-never-starts-it),
  [TD-003](../../../docs/tech-debt.md#td-003-cpu-and-memory-limits-are-best-effort)).
* Fairness among children that share the pinned CPU, and pinning without `nice`, were not measured
  (TD-003).
* Whether 62 % to 86 % of one core is "room to spare" for
  [ADR 0014](../../../docs/decisions/0014-build-the-edge-on-the-environments-http-stack.md) is the
  owner's judgement; the revisit trigger written there did not fire. The tail of the edge's latency
  moved between two invocations (p95 3.98 ms, then 247 ms, [above](#the-edge-at-500-a-second-twice))
  and the cause was not isolated; a run that samples the container during the first minutes after a
  restart would say whether children starting up are it.
* The hostile fixtures' "neighbour p95" is read from the dev machine through the Apps proxy and the
  network, so it measures that path (about 240 ms idle) more than the container; the difference
  between idle and beside hostile children is the figure to read.
* The run directory's cap was tested for bytes only: a writer was refused at 64 MiB, but no test
  created files, and the mount sets no `nr_inodes` ([TD-004](../../../docs/tech-debt.md#td-004-a-childs-run-directory-size-cap-is-unproven),
  T219).
