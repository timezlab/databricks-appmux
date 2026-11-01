# Example child apps

Three small apps, each a **standard Databricks App** (`app.yaml`, source, `requirements.txt`),
written in different stacks so you can register them in appmux by hand, once as an **app admin**
(register, update, roll back, change config, share) and once as an **end user** (open `/<slug>/`).

Nothing in these folders knows about appmux, knows its slug, or needs a change to `app.yaml` or
source to be hosted by it ([constitution I](../.specify/memory/constitution.md)). Each one also runs
on a standalone Databricks App unchanged. They exist for hand testing; they are not part of the
product, they are not shipped in the deploy folder (`scripts/assemble-deploy.sh` copies an
allow-list that does not include `examples/`), and the Python gate does not run them (it does lint
them).

> **Verification status.** Run **locally only**, in a scratch virtual environment whose versions
> match the Apps environment recorded in [design 5.3](../docs/product/v1-design.md) (fastapi 0.115.0,
> uvicorn 0.30.6, flask 3.0.3, gunicorn 23.0.0, streamlit 1.38.0), on Python 3.12 (the Apps
> environment is 3.11). They have **not** been run in the appmux container, registered on any appmux
> instance, or uploaded to a workspace. What the local runs showed is in
> [What was verified](#what-was-verified); the rest of the behaviour described below (cookie `Path`
> rewrite, `Location` rewrite, WebSocket relay, the token header) is what the design says appmux does,
> not something observed.

## The apps

| Folder | Framework | Start command | Suggested slug / budget | What to try |
|---|---|---|---|---|
| [fastapi-notes](fastapi-notes/README.md) | FastAPI on uvicorn (async API) | `python -m uvicorn app:app` | `notes` / 128 MB | JSON identity, in-memory notes that reset on restart, `/openapi.json` carrying the prefix |
| [flask-session](flask-session/README.md) | Flask on gunicorn (sync WSGI, server-rendered forms) | `python -m gunicorn --workers 1 --threads 4 app:app` | `session` / 128 MB | a session cookie and a form redirect, which are the two things appmux rewrites on a response |
| [streamlit-inspector](streamlit-inspector/README.md) | Streamlit (reactive UI over a WebSocket) | `streamlit run app.py` | `inspector` / 512 MB | the WebSocket relay carrying the identity headers, per-session state |

Every app listens on the port appmux gives it (`8000` in this repo's setup; locally you pick it), and
every app shows the same three things so you can tell a release from a config change:

- **who is calling**: `X-Forwarded-Email`, `X-Forwarded-User`, `X-Forwarded-Preferred-Username`, and
  whether `X-Forwarded-Access-Token` is present (never its value);
- **which release is running**: a `VERSION` constant in the source (change it, Update, see it change)
  and the `APP_GREETING` environment variable (declared in `app.yaml`, overridable from the release
  config without touching the source);
- **health**: `GET /` answers 200 (appmux's default health path) and each app has its own
  health URL (`/healthz`, or `/_stcore/health` for Streamlit).

Why these three: FastAPI is the framework appmux's own fixtures and docs lean on, so it is the
baseline; Flask on gunicorn is the synchronous WSGI path and gives the cookie and redirect rewrite
something to act on; Streamlit is the app that needs a long-lived WebSocket and relative asset
paths, the hardest ordinary case. Not included, and why: a Node app (appmux has a Node fixture at
[tests/fixtures/node_app](../tests/fixtures/node_app), and a Node app with dependencies needs
`index` mode, which v1 does not ship an offline cache for), and Dash or Gradio (they would exercise
the framework prefix variables appmux sets, `DASH_*` and `GRADIO_ROOT_PATH`; they are the natural
next examples). Both omissions are easy to reverse.

Packages: every requirement is already in the Apps environment, so no wheel folder, index or
`wheel_sources` is needed ([design 5.3](../docs/product/v1-design.md)). The `requirements.txt` files
use lower bounds, not `==` pins, so the environment's version satisfies them.

No example uses a resource or a secret, and none calls an API with the visitor's token. A resource
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
`__pycache__` before you upload: it would land in your workspace folder.

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
a platform admin presses **Request a new app** on the launcher with the same fields plus a reason,
**Send request**; a platform admin approves it from the inbox on **Platform**.)

**3. Update: make release 1.** Open the app (**Apps** > the app) and press **Update** in the header
of the **Status** section. appmux reads the folder with your token, snapshots it and creates release
1. Pressing Update on an app that was stopped also starts it. Watch the release on the **Releases**
section: it builds, starts, passes the health check and becomes the active release, and only then
does `/<slug>/` open. A failed release (a typo in `app.yaml`, for example) stays visible with its
reason and changes nothing for a running release.

**4. Open it.** `https://<your-appmux-url>/<slug>/`, with the trailing slash. The launcher's
**Open** button goes there.

**5. Update again: release 2.** Edit `VERSION` in `app.py` (`"1.0.0"` to `"2.0.0"`), run the
`import-dir` command from step 1 again, press **Update**. Release 2 starts beside release 1 on a
private route, must pass the health check, and only then does the route swap; release 1 drains. Keep
`/<slug>/` open in a tab and reload it through the update: it should never error. The page then shows
`2.0.0`, and anything the app kept in memory (notes, the visit counter, the Flask session key, the
Streamlit session count) is gone, because a new process replaced the old one. Pressing **Update**
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

All three apps report whether `X-Forwarded-Access-Token` arrives. appmux passes it only for an app a
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
   live widgets (Streamlit).

## What was verified

Run on 2026-11-01, locally, from this folder, with the environment variables appmux sets
(`UVICORN_*`, `PORT`, `STREAMLIT_SERVER_*`, `APPMUX_BASE_PATH`):

- `ruff format --check` and `ruff check` pass on all three `app.py` files.
- appmux's own `read_manifest` reads each `app.yaml` (runtime `python`, no warnings) and its
  requirements check finds every `requirements.txt` line satisfied by the environment.
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
- Peak memory over the process tree, and a start under `RLIMIT_AS` of 2 x the budget (the
  sandbox's factor, [watchdog](../src/appmux/sandbox/watchdog.py)): fastapi-notes peaked at 53 MB
  virtual and 41 MB resident, flask-session at 177 MB and 30 MB, streamlit-inspector at 562 MB and
  47 MB. All three started and answered under their suggested budgets (the fastapi and flask apps also
  under 64 MB). Streamlit also started under 256 MB, at 498 MB of its 512 MB limit, so 256 MB is its
  floor and the suggested 512 MB leaves room for more sessions. These are workstation numbers, not
  container numbers.

## What was not verified

Not run in the appmux container, and not run behind the appmux edge at all: the `Path=/<slug>/`
rewrite of `Set-Cookie`, the `Location` rewrite, the WebSocket relay carrying Streamlit, the
`X-Forwarded-Access-Token` header in either open mode, the health-gated swap with these apps, and
every UI label above as rendered (they are quoted from the web source and the admin API contract).
`import-dir`, Register, Update and the config call have not been run against a workspace. If
something here disagrees with what you see, trust what you see and correct this file.
