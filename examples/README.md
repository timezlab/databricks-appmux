# Example child apps

Eight small apps, each a **standard Databricks App** (`app.yaml` and source, plus `requirements.txt`
for the Python ones), written in different stacks so you can register them in appmux by hand, once
as an **app admin** (register, update, roll back, change config, share) and once as an **end user**
(open `/<slug>/`). Four of them are for checking calls **between** Apps, inside appmux and outside
it: an MCP server, an A2A agent that uses a free Databricks model, and a hub and a console that
ask whether the called App can tell who is behind the call.

Nothing in these folders knows about appmux, knows its slug, or needs a change to `app.yaml` or
source to be hosted by it ([constitution I](../.specify/memory/constitution.md)). Each one also runs
on a standalone Databricks App unchanged. They exist for hand testing; they are not part of the
product, they are not shipped in the deploy folder (`scripts/assemble-deploy.sh` copies an
allow-list that does not include `examples/`), and the Python gate lints all seven Python apps and
tests `mcp-tools`, `a2a-agent`, `agent-hub` and `agent-console` (`tests/unit/test_example_*.py`). The Node app is checked by hand.

> **Verification status.** `fastapi-notes`, `flask-session`, `streamlit-inspector` and `node-stream`
> were run **locally only**. The three Python ones ran in a scratch virtual environment whose
> versions match the Apps environment recorded in [design 5.3](../docs/product/v1-design.md)
> (fastapi 0.115.0, uvicorn 0.30.6, flask 3.0.3, gunicorn 23.0.0, streamlit 1.38.0), on Python 3.12
> (the Apps environment is 3.11). The Node app ran on Node v24.12.0 (the Apps environment has
> Node 22) and has no dependency. None of these four has been run in the appmux container,
> registered on any appmux instance, or uploaded to a workspace. The rest of the behaviour described
> below for them (cookie `Path` rewrite, `Location` rewrite, WebSocket relay, the token header) is
> what the design says appmux does, not something observed.
>
> `mcp-tools` and `a2a-agent` were also run on 2026-11-03 as children of an appmux App and as
> standalone Apps on the Free Edition workspace, calling each other and a free foundation model; the
> results are in [What was verified](#what-was-verified) and in
> [docs/reference/mcp-and-a2a.md](../docs/reference/mcp-and-a2a.md).
>
> `agent-hub` and `agent-console` were tested locally against fakes (a fake Databricks proxy in
> front of the real hub) and then run on 2026-11-04 as children of the dev App with OBO and the
> shared service principal delegated, called with the CLI's token and then from a browser by two
> accounts, with the console's own identity admitted by an `everyone` grant on the hub. Not yet
> measured: a `service_principal` grant in place of `everyone`; see
> [the reference](../docs/reference/mcp-and-a2a.md#who-the-target-sees-when-the-caller-is-an-app).

## The apps

| Folder | Framework | Start command | Suggested slug / budget | What to try |
|---|---|---|---|---|
| [fastapi-notes](fastapi-notes/README.md) | FastAPI on uvicorn (async API) | `python -m uvicorn app:app` | `notes` / 128 MB | JSON identity, in-memory notes that reset on restart, `/openapi.json` carrying the prefix |
| [flask-session](flask-session/README.md) | Flask on gunicorn (sync WSGI, server-rendered forms) | `python -m gunicorn --workers 1 --threads 4 app:app` | `session` / 128 MB | a session cookie and a form redirect, which are the two things appmux rewrites on a response |
| [streamlit-inspector](streamlit-inspector/README.md) | Streamlit (reactive UI over a WebSocket) | `streamlit run app.py` | `inspector` / 512 MB | the WebSocket relay carrying the identity headers, per-session state |
| [node-stream](node-stream/README.md) | Node, `node:http` only, no dependency | `node server.js` | `stream` / **768 MB** | the Node runtime, a Server-Sent Events stream that stays open, relative asset and stream URLs under the prefix |
| [mcp-tools](mcp-tools/README.md) | Starlette on uvicorn, an MCP server (stateless Streamable HTTP) | `python -m uvicorn app:app` | `tools` / 128 MB | four tools, `whoami` showing who the server sees at the end of a chain of Apps, being called by another App |
| [a2a-agent](a2a-agent/README.md) | Starlette on uvicorn, an A2A 1.0 agent whose model is a free Databricks endpoint | `python -m uvicorn app:app` | `agent` / 256 MB | calling a model, an MCP server and another agent with the App's own identity; the shared-SP delegation and `CAN USE` they need |
| [agent-hub](agent-hub/README.md) | Starlette on uvicorn, MCP and A2A in one App, a free Databricks model as the agent | `python -m uvicorn app:app` | `hub` / 256 MB | tools that run as the caller: `whoami` and `databricks_me`; what the hub sees for a person's call and for an App's |
| [agent-console](agent-console/README.md) | Starlette on uvicorn, a page that calls the hub | `python -m uvicorn app:app` | `console` / 256 MB | the identity check: MCP and A2A, each sent as you (OBO) and as the App, and what the hub saw |

Every app listens on the port appmux gives it (`8000` in this repo's setup; locally you pick it), and
every app shows the same three things so you can tell a release from a config change:

- **who is calling**: `X-Forwarded-Email`, `X-Forwarded-User`, `X-Forwarded-Preferred-Username`, and
  whether `X-Forwarded-Access-Token` is present (never its value);
- **which release is running**: a `VERSION` constant in the source (change it, Update, see it change)
  and the `APP_GREETING` environment variable (declared in `app.yaml`, overridable from the release
  config without touching the source);
- **health**: `GET /` answers 200 (appmux's default health path) and each app has its own
  health URL (`/healthz`, or `/_stcore/health` for Streamlit).

Why the first four: FastAPI is the framework appmux's own fixtures and docs lean on, so it is the
baseline; Flask on gunicorn is the synchronous WSGI path and gives the cookie and redirect rewrite
something to act on; Streamlit is the app that needs a long-lived WebSocket and relative asset
paths, the hardest ordinary case; the Node app is the only one on the Node runtime and the only one
that holds a plain HTTP response open (Server-Sent Events). It uses Node's own `http` module because
v1 has no `npm install` step, so a Node app with dependencies is not something this release can host
([research D8](../specs/001-appmux-v1/research.md); appmux's Node fixture is
[tests/fixtures/node_app](../tests/fixtures/node_app)), and it needs a **768 MB budget**, not the
128 MB of the small Python apps, because V8 does not start under about 1.1 GB of address space (see
its README). Not included, and why: Dash and Gradio (they would exercise the framework prefix
variables appmux sets, `DASH_*` and `GRADIO_ROOT_PATH`; they are the natural next examples). The
omission is easy to reverse.

The last two exist for one question: **can app A call app B** when either is a child of appmux or a
standalone App. MCP and A2A are plain JSON-RPC over HTTP, so the answer is in three hops (network, a
bearer the Apps proxy accepts, `CAN USE`), plus appmux's own grants for a child. `mcp-tools` is the
callee; `a2a-agent` is the caller and also a callee, and it talks to a free foundation-model
endpoint, so nothing needs a paid model. What was measured, and the setup each combination needs,
is in [docs/reference/mcp-and-a2a.md](../docs/reference/mcp-and-a2a.md). Both depend only on
packages the Apps environment already holds (Starlette, uvicorn, httpx, the Databricks SDK).

`agent-hub` and `agent-console` ask the next question: when A calls B, **does B know who is behind
the call, and can it act as them?** The hub serves MCP and A2A and its tools run as the caller; the console
calls it with the visitor's own token (needs OBO delegated to the console) or with its own identity
(needs the shared service principal), and draws what the hub saw for each. They are the only examples
that use the visitor's token.

Packages: every Python requirement is already in the Apps environment, so no wheel folder, index or
`wheel_sources` is needed ([design 5.3](../docs/product/v1-design.md)). The `requirements.txt` files
use lower bounds, not `==` pins, so the environment's version satisfies them. The Node app has no
dependency and no `requirements.txt` (that file would make appmux treat it as Python).

No example uses a resource or a secret, and only `agent-hub` and `agent-console` use the visitor's
token (`a2a-agent` calls with the App's **own** identity). A resource
example needs an object in your workspace and exact attach steps that have not been run in the
container (tasks T129 and T147); an OBO call needs a scope on the appmux App. The apps do show
whether the token header arrives, which is the part appmux controls (see
[OBO, optional](#obo-optional)).

## Before you start

You need an appmux instance you can sign in to, a Databricks CLI profile that can write to your
workspace folder, and `jq` for the API step. Placeholders used below:

| Placeholder | Meaning |
|---|---|
| `<your-appmux-url>` | the URL of the appmux App, such as the one shown on its Databricks Apps page |
| `<your-email>` | your workspace email, the owner you register |
| `<your-profile>` | your `~/.databrickscfg` profile for the workspace |

Roles, as appmux defines them ([design 1](../docs/product/v1-design.md#1-overview)): a **platform admin**
registers apps directly; an **app admin** (the owner, or someone with `CAN_MANAGE`) updates and
configures one app; a **user** holds `CAN_USE`. With one account you play the first two and a
visitor in turn. To see what a plain `CAN_USE` visitor sees, share an app with a second account or a
colleague (step 8) and open it from there.

The appmux App must be allowed to read your folder with your token on Update (it needs
`workspace.workspace:read` among its `user_api_scopes`; see "Deploy scripts" in
[AGENTS.md](../AGENTS.md)).

## Run an example locally first (optional)

```sh
python3 -m venv /tmp/appmux-examples-venv      # outside examples/: import-dir uploads everything it finds
/tmp/appmux-examples-venv/bin/pip install \
  -r examples/fastapi-notes/requirements.txt \
  -r examples/flask-session/requirements.txt \
  -r examples/streamlit-inspector/requirements.txt
. /tmp/appmux-examples-venv/bin/activate
```

Then the commands in each app's README. Use `PYTHONDONTWRITEBYTECODE=1` as they show, or delete any
`__pycache__` before you upload: it would land in your workspace folder. `node-stream` needs no
virtual environment, only Node 22 or later ([its README](node-stream/README.md#run-it-locally)).

## Admin walkthrough, in order

Do this for one app first (`fastapi-notes` is the simplest), then repeat for the others.

**1. Upload the source.** From the repository root:

```sh
databricks workspace import-dir examples/fastapi-notes \
  /Workspace/Users/<your-email>/appmux-examples/fastapi-notes \
  --profile <your-profile> --overwrite
```

The target must be a workspace folder starting with `/Workspace/`. That path is what appmux calls
the **Source folder**.

**2. Register the app.** Sign in as a platform admin and open `https://<your-appmux-url>/_appmux/`,
then **Platform** > **Register an app** and fill in:

| Field | Value (fastapi-notes) |
|---|---|
| Slug | `notes` (lower-case letters, digits, `-`; at most 31 characters; `_appmux` is reserved) |
| Display name | `Notes (FastAPI)` |
| Source folder | `/Workspace/Users/<your-email>/appmux-examples/fastapi-notes` |
| Memory budget (MB) | `128` |
| Owner | `<your-email>` |

The app now exists in the **Platform** > **Apps** list, stopped, with no release. (A user who is not
a platform admin presses **Request a new app** on the launcher with the same fields except the memory
budget, plus a reason, **Send request**; a platform admin approves it from the inbox on **Platform**, and the
app starts with the platform's default memory budget, 512 MB unless changed in **Platform** > **Settings**.)

**3. Update: make release 1.** Open the app (**Apps** > the app) and press **Update** in the header
of the **Status** section. appmux reads the folder with your token, snapshots it and creates release
1. Pressing Update on an app that was stopped also starts it. Watch the release on the **Releases**
section: it builds, starts, passes the health check and becomes the active release, and only then
does `/<slug>/` open. A failed release (a typo in `app.yaml`, for example) stays visible with its
reason and changes nothing for a running release.

**4. Open it.** `https://<your-appmux-url>/<slug>/`, with the trailing slash. The launcher's
**Open** button goes there.

**5. Update again: release 2.** Edit `VERSION` in `app.py` (`server.js` for the Node app; `"1.0.0"` to `"2.0.0"`), run the
`import-dir` command from step 1 again, press **Update**. Release 2 starts beside release 1 on a
private route, must pass the health check, and only then does the route swap; release 1 drains. Keep
`/<slug>/` open in a tab and reload it through the update: it should never error. The page then shows
`2.0.0`, and anything the app kept in memory (notes, the visit counter, the Flask session key, the
Streamlit session count, the Node stream count) is gone, because a new process replaced the old one. Pressing **Update**
with no change to the folder answers "already up to date" and creates no release.

**6. Roll back.** On **Releases**, press **Activate** on release 1. It goes through the same
health-gated swap with release 1's stored snapshot and config; no folder or token is read. The page
shows `1.0.0` again. (There is no separate Rollback button: activating an older release is the
rollback.) **View source** on a release shows the files it stored.

**7. Change config without changing source.** The admin UI has no editor for environment entries,
so this step is the API ([contract](../specs/001-appmux-v1/contracts/admin-api.md): `PUT
/apps/{app}/config`). It overrides `APP_GREETING` for the next release; the source and `app.yaml`
stay as they are:

```sh
API=https://<your-appmux-url>/_appmux/api/v1
SLUG=notes
TOKEN=$(databricks auth token --profile <your-profile> | jq -r .access_token)

# The write must send back the config version it read, and replaces the whole editable config.
BODY=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/apps/$SLUG" | jq '.config | {
  version,
  env: ([.env[] | select(.name != "APP_GREETING")] + [{name: "APP_GREETING", value: "Hello from the release config"}]),
  wheel_sources, health_path, health_deadline_s, own_sp_client_id_key, own_sp_client_secret_key}')

curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "$BODY" "$API/apps/$SLUG/config"
```

The answer is `{"config_version": N, "release_no": M}`: a config change makes a **new release with
the same snapshot**, so it appears on **Releases**, goes through the health-gated swap, and the page
shows the new greeting with the same `VERSION`. A later **Update** copies the target release's
config, so the override stays; **Activate** on release 1 brings back the greeting from `app.yaml`.
Do not paste the token anywhere: it is a bearer credential for your account.

**8. Sharing and control.** On the app's **Sharing** section, add an **Email address** with **Can
use** (or **Everyone**: anyone who signs in to the workspace) to let someone open it; **Can manage**
also lets them Update and configure it. In the header, **Stop** and **Start** change whether it runs
and **Restart** replaces the process with a new copy beside the old one. After a Restart the
in-memory state resets, with no change of release.

### OBO, optional

All four apps report whether `X-Forwarded-Access-Token` arrives. appmux passes it only for an app a
platform admin has delegated OBO to, and only to an allowed visitor ([design 7.1](../docs/product/v1-design.md)).
By default it is **absent**. To see it **present**, a platform admin delegates OBO with a reason
(takes effect at the app's next start), using the same `API`, `SLUG` and `TOKEN` as above:

```sh
VERSION=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/apps/$SLUG" | jq .version)
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"reason\": \"trying the OBO header in an example app\", \"version\": $VERSION}" \
  "$API/apps/$SLUG/delegation/obo"
```

Then **Restart** the app. A platform admin now gets two buttons on the launcher, **Open without
token** (the header stays absent; the default) and **Open as a user** (the header is present, and the
choice is written to the audit log). This step has not been run.

## End-user walkthrough

Open `https://<your-appmux-url>/_appmux/` (or `/`): the launcher lists the apps you may use. Press
**Open** on an app, which goes to `/<slug>/`. Check, for each app:

1. The address bar ends in `/<slug>/` and the page loads fully (styles, scripts, links).
2. **You are** shows your own workspace email, not `anonymous`. That email is the Databricks proxy's
   `X-Forwarded-Email`, forwarded by appmux; a header you set yourself is dropped.
3. The greeting and `VERSION` match what the admin did: reload after each Update, Activate or config
   change and see them move.
4. The links on the page (`api/whoami`, `healthz`, and so on) stay under `/<slug>/`. They are
   relative on purpose, because appmux never rewrites a response body.
5. Open `https://<your-appmux-url>/<slug>` **without** the trailing slash. appmux routes it to the
   same app, but the relative links then resolve one level too high; this is the documented limit
   of body-unrewritten routing, not a defect in the example. The launcher always uses the slash.
6. As a visitor with no grant (a second account), the app is not in your launcher and `/<slug>/`
   does not serve you the app.
7. While the admin presses **Stop**, `/<slug>/` shows a status page instead of the app; after
   **Start** it serves again.
8. The per-app checks in each README: cookie and redirect (Flask), notes and OpenAPI (FastAPI),
   live widgets (Streamlit), the 15 second stream (Node).

## What was verified

Run on 2026-11-01, locally, from this folder, with the environment variables appmux sets
(`UVICORN_*`, `PORT`, `STREAMLIT_SERVER_*`, `DATABRICKS_APP_PORT`, `APPMUX_BASE_PATH`):

- `ruff format --check` and `ruff check` pass on all three `app.py` files. The Node app has no
  linter or formatter run over it; `node --check server.js` passes.
- appmux's own `read_manifest` reads each `app.yaml` (runtime `python` for the three Python apps and
  `node` for `node-stream`, no warnings) and its requirements check finds every `requirements.txt`
  line satisfied by the environment.
- **fastapi-notes**: pages and JSON answered with and without identity headers; the token header is
  reported as present and its value never appears; notes are stored, HTML-escaped, and refused with
  `409` at 50; `/openapi.json` lists the prefix under `UVICORN_ROOT_PATH`.
- **flask-session**: gunicorn bound `0.0.0.0:$PORT` with only `PORT` set; with a cookie jar, the
  visit count rose, `POST /nickname` answered `302` with `Location: /`, `POST /forget` cleared the
  session, the nickname is escaped and cut to 40 characters, and the cookie carries `Secure` unless
  `ALLOW_INSECURE_COOKIES=1`.
- **streamlit-inspector**: `/_stcore/health` answered `ok`; `GET /` answered 200 with relative asset
  paths; in headless Chromium with identity headers on every request, the WebSocket opened, the
  headers showed up in the page, two button clicks and a typed echo worked, and a second session
  raised the session count.
- **node-stream** (Node v24.12.0, under an address-space limit of 1,536 MB): pages and JSON answered
  with and without identity headers; the token header is reported as present and its value never
  appears in any response; the email header and the greeting are HTML-escaped; unknown paths answer
  `404` and other methods `405` with `Allow: GET`; `/events` sends 15 ticks and `event: done`; with a
  hook that counts `setInterval` calls, one timer is live while a stream is open and none after the
  client leaves; `SIGTERM` with a stream open ended the process in about 0.1 s (and exits 0 without
  one); a missing or invalid `DATABRICKS_APP_PORT` exits 1 with a message; `npm run start` serves.
  In headless Chromium, behind a throwaway proxy that strips `/stream` and adds
  `X-Forwarded-Prefix`, the page's script ran from `/stream/`: 15 ticks, `stream finished`, the
  button locked and then unlocked again, no failed request. That proxy is not appmux.
- Peak memory over the process tree, and a start under `RLIMIT_AS` of 2 x the budget (the
  sandbox's factor, [watchdog](../src/appmux/sandbox/watchdog.py)): fastapi-notes peaked at 53 MB
  virtual and 41 MB resident, flask-session at 177 MB and 30 MB, streamlit-inspector at 562 MB and
  47 MB. All three started and answered under their suggested budgets (the fastapi and flask apps also
  under 64 MB). Streamlit also started under 256 MB, at 498 MB of its 512 MB limit, so 256 MB is its
  floor and the suggested 512 MB leaves room for more sessions. node-stream peaked at 1.4 GB virtual
  and 50 MB resident; it started at a limit of 1,120 MB and **did not start at 1,100 MB or below**
  (`Failed to reserve virtual memory for CodeRange`), so a budget of 512 MB (limit 1,024 MB) fails and
  the suggested 768 MB is the tested one. These are workstation numbers, not container numbers.

- **mcp-tools** and **a2a-agent** (Python 3.11, Starlette 0.38.6, uvicorn 0.30.6): 27 and 62 unit
  tests pass; the official `mcp` 1.30.0 and `a2a-sdk` 1.3.0 clients ran against them locally. On the
  Free Edition workspace (2026-11-03) both ran as children of an appmux App (`tools` at 128 MB,
  `agent` at 256 MB) and as standalone Apps. The calls in
  [the table of the reference](../docs/reference/mcp-and-a2a.md#calls-between-apps-measured) were
  run: a person with a user token, a child with the shared service principal, and a standalone App
  with its own, calling an MCP server and an agent inside and outside appmux. The agent answered
  with the free model `databricks-gpt-oss-120b` using a tool of the MCP server, and two agents
  relayed to each other both ways. A caller whose service principal lacked `CAN USE` got `401 {}`,
  one the proxy admitted but no grant admitted on the child got `404`, and a laptop request with
  no bearer got `302` without an `Accept` header and `401 {}` with one.

## What was not verified

Not run in the appmux container, and not run behind the appmux edge at all: the `Path=/<slug>/`
rewrite of `Set-Cookie`, the `Location` rewrite, the WebSocket relay carrying Streamlit, the
`X-Forwarded-Access-Token` header in either open mode, the health-gated swap with these apps, and
every UI label above as rendered (they are quoted from the web source and the admin API contract).
For `node-stream` also: Node 22 itself (it ran on v24.12.0 and uses no API newer than 22, which is a
reading, not a run), the Server-Sent Events stream through the edge (the 120 second idle wait, the
64 places per app that an open stream holds, and any buffering), what happens to a stream that is open during an Update or a
Restart, and the release failing to start under a 512 MB budget (shown here only as a local `ulimit`).
`import-dir`, Register, Update and the config call have not been run for the first four apps. For
`mcp-tools` and `a2a-agent`, Register, Update, the config call, the delegation and the grants ran
through the admin API with the CLI's token, not through the UI, so the UI labels in their READMEs
are quoted, not seen; the reference lists what else was not measured under
[Not measured](../docs/reference/mcp-and-a2a.md#not-measured) (a call near the 300 s limit, OBO, load,
`group` grants for a service principal). If
something here disagrees with what you see, trust what you see and correct this file.
