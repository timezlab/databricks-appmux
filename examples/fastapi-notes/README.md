# fastapi-notes

A FastAPI app on uvicorn: an async JSON API plus one HTML page. It is the baseline example, and the
one to register first. The general walkthrough (upload, register, Update, roll back, config, share,
and what an end user checks) is in [../README.md](../README.md#admin-walkthrough-in-order); this file
holds what is specific to this app.

## What it shows

- **Identity**: the page and `GET /api/whoami` report `X-Forwarded-Email`, `X-Forwarded-User`,
  `X-Forwarded-Preferred-Username` and `X-Request-Id` as they arrive, and whether
  `X-Forwarded-Access-Token` is present (never its value). Without a header the caller is
  `anonymous`.
- **Release and config**: `VERSION` in `app.py` (`1.0.0`) and the `APP_GREETING` variable
  (`app.yaml` sets `Hello from fastapi-notes`; the release config can override it).
- **State that dies with the process**: `POST /api/notes` stores notes in memory, so an Update, an
  Activate, a config change or a Restart empties them. Notes show the author's email.
- **The prefix**: `X-Forwarded-Prefix` and `APPMUX_BASE_PATH` are shown, and `/openapi.json` carries
  the prefix in its `servers` entry because appmux sets `UVICORN_ROOT_PATH`.
- **Health**: `GET /` and `GET /healthz` answer 200.

## Files

| File | Purpose |
|---|---|
| `app.py` | the whole app |
| `app.yaml` | `command: ["python", "-m", "uvicorn", "app:app"]` and `APP_GREETING` |
| `requirements.txt` | `fastapi>=0.110`, `uvicorn>=0.30`; both are already in the Apps environment |

`python -m uvicorn` takes its host and port from `UVICORN_HOST` and `UVICORN_PORT`, which both a
standalone App and appmux set, so `app.yaml` names no port.

## Run it locally

After the virtual environment in [../README.md](../README.md#run-an-example-locally-first-optional):

```sh
cd examples/fastapi-notes
PYTHONDONTWRITEBYTECODE=1 UVICORN_HOST=127.0.0.1 UVICORN_PORT=8000 \
  UVICORN_ROOT_PATH=/demo APPMUX_BASE_PATH=/demo APP_GREETING="Hello from my laptop" \
  python -m uvicorn app:app
```

The last three variables copy what appmux sets; leave them out to see it as a standalone app. In
another terminal, with and without the headers appmux would forward (the token is a fake value):

```sh
curl -s localhost:8000/api/whoami
curl -s -H 'X-Forwarded-Email: dana@example.com' -H 'X-Forwarded-Preferred-Username: dana@example.com' \
  -H 'X-Forwarded-Prefix: /demo' -H 'X-Forwarded-Access-Token: placeholder' localhost:8000/api/whoami
curl -s -H 'X-Forwarded-Email: dana@example.com' -H 'Content-Type: application/json' \
  -d '{"text": "first note"}' localhost:8000/api/notes
curl -s localhost:8000/api/notes
```

Stop it with `Ctrl-C`.

## Upload and register

Placeholders as in [../README.md](../README.md#before-you-start):

```sh
databricks workspace import-dir examples/fastapi-notes \
  /Workspace/Users/<your-email>/appmux-examples/fastapi-notes \
  --profile <your-profile> --overwrite
```

Then **Platform** > **Register an app**: Slug `notes`, Display name `Notes (FastAPI)`, Source folder
`/Workspace/Users/<your-email>/appmux-examples/fastapi-notes`, Memory budget (MB) `128`, Owner
`<your-email>`; then open the app and press **Update**.

The memory suggestion comes from a local run: 53 MB virtual and 41 MB resident at peak, and it
started under an address-space limit of 2 x 64 MB. Not measured in the container.

## As an admin, try

1. **Update** with the folder unchanged: "already up to date", no new release.
2. Add a note, then set `VERSION = "2.0.0"`, `import-dir` again and **Update**. Reload: the version
   moves and the notes are gone (a new process).
3. **Activate** release 1: version `1.0.0` again.
4. Override `APP_GREETING` with the config call in the
   [main walkthrough](../README.md#admin-walkthrough-in-order) (step 7): same version, new greeting,
   a new release number.
5. Break it on purpose: change `app.yaml`'s command to `["python", "-m", "uvicorn", "nope:app"]`,
   **Update**. The release should fail its health check and the running release should keep serving.
   (Not run.) Put it back and Update again.

## As an end user, check that

- `/<slug>/` loads and **You are** shows your email.
- The links **api/whoami**, **api/notes** and **healthz** work and stay under `/<slug>/`.
- `api/whoami` shows `x_forwarded_prefix: "/<slug>"` and `appmux_base_path: "/<slug>"`.
- `openapi.json` has `"servers": [{"url": "/<slug>"}]`.
- **docs** (Swagger UI) loads, then **POST /api/notes** > **Try it out** > **Execute** adds a note
  under your email, and **api/notes** lists it. This works under the prefix because the docs page
  asks for `/<slug>/openapi.json` and builds its requests from the `servers` entry, both of which
  come from `UVICORN_ROOT_PATH`. Swagger UI's scripts come from a CDN, so your browser needs
  internet access; the edge's security policy applies to the admin UI, not to apps.
- The access-token header reads **absent**, unless the admin delegated OBO and opened the app **as a
  user** ([../README.md](../README.md#obo-optional)).
- After the admin presses **Restart**, the notes list is empty again.
