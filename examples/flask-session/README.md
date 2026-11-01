# flask-session

A Flask app on gunicorn: server-rendered pages, a signed session cookie and two forms that answer
with a redirect. It is the example for the two things appmux rewrites on a response: the cookie's
`Path` and a redirect's `Location`. The general walkthrough is in
[../README.md](../README.md#admin-walkthrough-in-order); this file holds what is specific to this app.

## What it shows

- **Identity**: the page and `GET /api/whoami` report `X-Forwarded-Email`, `X-Forwarded-User`,
  `X-Forwarded-Preferred-Username` and `X-Request-Id`, and whether `X-Forwarded-Access-Token` is
  present (never its value). Without a header the caller is `anonymous`.
- **Release and config**: `VERSION` in `app.py` (`1.0.0`) and the `APP_GREETING` variable
  (`app.yaml` sets `Hello from flask-session`; the release config can override it).
- **A session cookie**: each load of `/` raises a visit counter kept in Flask's signed session
  cookie, and **Save** keeps a nickname there. Flask writes `Set-Cookie: session=...; Path=/`;
  [design 7.1](../../docs/product/v1-design.md) says appmux gives it `Path=/<slug>/` and drops any
  `Domain`, so two apps on the one origin do not see each other's cookie.
- **A redirect**: `POST /nickname` and `POST /forget` answer `302` with `Location: /` on purpose
  (an absolute path, as an app that believes it lives at `/` would write it). appmux makes it
  `/<slug>/`. The forms post to relative URLs (`nickname`, `forget`), so they work under the prefix.
- **State that dies with the process**: the signing key is random per process and gunicorn runs one
  worker (`--workers 1`), so an Update, an Activate, a config change or a Restart invalidates every
  session: the visit count and nickname reset.
- **Health**: `GET /` and `GET /healthz` answer 200.

## Files

| File | Purpose |
|---|---|
| `app.py` | the whole app (one inline HTML template) |
| `app.yaml` | `command: ["python", "-m", "gunicorn", "--workers", "1", "--threads", "4", "app:app"]` and `APP_GREETING` |
| `requirements.txt` | `flask>=3.0`, `gunicorn>=22`; both are already in the Apps environment |

gunicorn binds `0.0.0.0:$PORT`, and both a standalone App and appmux set `PORT`, so `app.yaml`
names no port. One worker is deliberate: with several, each would have its own key and a session
would break between requests. The cookie is `Secure`, `HttpOnly` and `SameSite=Lax`; set
`ALLOW_INSECURE_COOKIES=1` only for a local run over plain `http`.

## Run it locally

After the virtual environment in [../README.md](../README.md#run-an-example-locally-first-optional):

```sh
cd examples/flask-session
PYTHONDONTWRITEBYTECODE=1 PORT=8000 APPMUX_BASE_PATH=/demo ALLOW_INSECURE_COOKIES=1 \
  APP_GREETING="Hello from my laptop" python -m gunicorn --workers 1 --threads 4 app:app
```

In another terminal, a cookie jar plays the browser:

```sh
curl -s -c jar.txt -b jar.txt -H 'X-Forwarded-Email: dana@example.com' localhost:8000/ | grep -E 'You are|loaded'
curl -s -i -c jar.txt -b jar.txt -d 'nickname=Dee' localhost:8000/nickname | grep -iE '^HTTP|^location'
curl -s -c jar.txt -b jar.txt -H 'X-Forwarded-Email: dana@example.com' localhost:8000/ | grep -E 'nicknamed|loaded'
curl -s -H 'X-Forwarded-Email: dana@example.com' -H 'X-Forwarded-Access-Token: placeholder' \
  localhost:8000/api/whoami
rm jar.txt
```

The second command shows `302` and `Location: /`. Stop the server with `Ctrl-C`.

## Upload and register

Placeholders as in [../README.md](../README.md#before-you-start):

```sh
databricks workspace import-dir examples/flask-session \
  /Workspace/Users/<your-email>/appmux-examples/flask-session \
  --profile <your-profile> --overwrite
```

Then **Platform** > **Register an app**: Slug `session`, Display name `Session (Flask)`, Source
folder `/Workspace/Users/<your-email>/appmux-examples/flask-session`, Memory budget (MB) `128`,
Owner `<your-email>`; then open the app and press **Update**.

The memory suggestion comes from a local run: the gunicorn master and its one worker peaked at 33 MB
and 177 MB virtual and 23 MB and 30 MB resident, and the app started under an address-space limit of
2 x 64 MB. Not measured in the container.

## As an admin, try

1. **Update** with the folder unchanged: "already up to date", no new release.
2. Load the page twice, then set `VERSION = "2.0.0"`, `import-dir` again and **Update**. Reload: the
   version moves and the visit count and nickname start over.
3. **Activate** release 1: version `1.0.0`, sessions reset again (a new process, a new key).
4. Override `APP_GREETING` with the config call in the
   [main walkthrough](../README.md#admin-walkthrough-in-order) (step 7): same version, new greeting.
5. **Stop** the app and open `/<slug>/`: a status page instead of the app. **Start** it again.

## As an end user, check that

- `/<slug>/` loads and **You are** shows your email.
- The visit count rises on each reload.
- **Save** a nickname: the page comes back at `/<slug>/` (not at `/`, which would be the launcher)
  with **nicknamed** and your nickname. This is the `Location` rewrite working.
- In the browser's developer tools (**Application** > **Cookies**, or the **Network** tab's
  `Set-Cookie`), the `session` cookie's **Path** is `/<slug>/`, it is `Secure` and `HttpOnly`, and it
  has no **Domain**. This is the cookie rewrite working. Open a second app (for example
  `/<other-slug>/`): it does not receive this cookie.
- **Forget me** clears the nickname and restarts the count at 1, and returns to `/<slug>/`.
- The access-token header reads **absent**, unless the admin delegated OBO and opened the app **as a
  user** ([../README.md](../README.md#obo-optional)).
- After the admin presses **Restart**, your next load starts a new session even though your browser
  still sends the old cookie: the new process cannot verify it, so it quietly replaces it.
