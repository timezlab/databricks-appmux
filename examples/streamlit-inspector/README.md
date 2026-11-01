# streamlit-inspector

A Streamlit app: a reactive page driven over a long-lived WebSocket. It is the hardest ordinary case
for a path-prefix proxy, because the page loads its scripts by relative path and then talks to the
server over `/_stcore/stream`. The general walkthrough is in
[../README.md](../README.md#admin-walkthrough-in-order); this file holds what is specific to this app.

## What it shows

- **Identity**: the page reports `X-Forwarded-Email`, `X-Forwarded-User`,
  `X-Forwarded-Preferred-Username` and `X-Request-Id`, and whether `X-Forwarded-Access-Token` is
  present (never its value), read with `st.context.headers`. Those are the headers of the session's
  **WebSocket handshake**, so seeing your email here proves that appmux forwards the identity headers
  on the upgrade request as well as on plain requests. Without a header the values are `null`.
- **Release and config**: `VERSION` in `app.py` (`1.0.0`) and the `APP_GREETING` variable
  (`app.yaml` sets `Hello from streamlit-inspector`; the release config can override it).
- **Per-process and per-session state**: `sessions_since_start` counts browser sessions served by
  this process (it resets when the process is replaced); the **Click** counter and the text box
  belong to one browser session (they survive a rerun and reset on a reload).
- **The prefix**: `APPMUX_BASE_PATH` and `X-Forwarded-Prefix` are shown. appmux sets **no** Streamlit
  base-path variable on purpose (it breaks Streamlit when the prefix is stripped), and this app sets
  none either: Streamlit's page uses relative asset paths and works under any prefix.
- **Health**: `GET /` answers 200 (the static shell; the script does not run for a plain GET) and
  `GET /_stcore/health` answers `ok`.

## Files

| File | Purpose |
|---|---|
| `app.py` | the whole app |
| `app.yaml` | `command: ["streamlit", "run", "app.py"]` and `APP_GREETING` |
| `requirements.txt` | `streamlit>=1.37` (`st.context.headers` needs 1.37 or later); already in the Apps environment |

`app.yaml` names no port, address or headless flag: `STREAMLIT_SERVER_PORT`, `_ADDRESS` and
`_HEADLESS` come from the environment, set by a standalone App and by appmux, which also turns off
Streamlit's CORS and XSRF checks for a child (the proxy sits in front of it) and its usage
statistics.

## Run it locally

After the virtual environment in [../README.md](../README.md#run-an-example-locally-first-optional):

```sh
cd examples/streamlit-inspector
PYTHONDONTWRITEBYTECODE=1 STREAMLIT_SERVER_PORT=8000 STREAMLIT_SERVER_ADDRESS=127.0.0.1 \
  STREAMLIT_SERVER_HEADLESS=true STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
  APPMUX_BASE_PATH=/demo APP_GREETING="Hello from my laptop" streamlit run app.py
```

Then open <http://127.0.0.1:8000/>. A plain browser sends no identity headers, so every identity
value reads `null` and the token reads `absent`: that is the picture without a proxy. The health
check works from the shell:

```sh
curl -s -w ' [%{http_code}]\n' localhost:8000/_stcore/health     # ok [200]
curl -s -o /dev/null -w '[%{http_code}]\n' localhost:8000/        # [200]
```

To see the header cells fill in locally, use a browser extension that adds request headers
(`X-Forwarded-Email: dana@example.com`) to `127.0.0.1`. The headers must be on the WebSocket
handshake too; `curl` cannot show this, since the values are read in the WebSocket session. Stop the
server with `Ctrl-C`.

## Upload and register

Placeholders as in [../README.md](../README.md#before-you-start):

```sh
databricks workspace import-dir examples/streamlit-inspector \
  /Workspace/Users/<your-email>/appmux-examples/streamlit-inspector \
  --profile <your-profile> --overwrite
```

Then **Platform** > **Register an app**: Slug `inspector`, Display name `Inspector (Streamlit)`,
Source folder `/Workspace/Users/<your-email>/appmux-examples/streamlit-inspector`, Memory budget
(MB) `512`, Owner `<your-email>`; then open the app and press **Update**.

The memory suggestion comes from a local run: 562 MB virtual and 47 MB resident at peak after one
browser session, and it started under an address-space limit of 2 x 256 MB (peak 498 MB, so 256 MB
is the floor and 512 MB leaves room). The resident figure grows with sessions and with whatever data
an app loads. Not measured in the container.

## As an admin, try

1. **Update** with the folder unchanged: "already up to date", no new release.
2. Open the app in a browser tab, click **Click** a few times, then set `VERSION = "2.0.0"`,
   `import-dir` again and **Update**. The open tab's WebSocket belongs to the old process, which
   drains: reload to reach the new one. The page shows `2.0.0` and `sessions_since_start` is `1`.
3. **Activate** release 1: `1.0.0` again.
4. Override `APP_GREETING` with the config call in the
   [main walkthrough](../README.md#admin-walkthrough-in-order) (step 7): same version, new greeting.
5. **Restart** while a tab is open: the tab shows Streamlit's "connection lost" notice and recovers
   on reload. (Not run; this depends on how the edge closes the relayed WebSocket.)

## As an end user, check that

- `/<slug>/` loads fully: the title, the greeting and the JSON blocks appear, not a blank page or
  "Please wait". A blank page means a script or the WebSocket failed to load through the prefix.
- **Who is calling** shows your own email, `x_forwarded_prefix: "/<slug>"` and
  `appmux_base_path: "/<slug>"`.
- Click **Click** twice: **Clicks: 2**. Type in the box and press Enter: **Echo** repeats it. This
  is the WebSocket carrying events both ways.
- In the browser's developer tools **Network** tab, filtered to **WS**, there is one connection to
  `/<slug>/_stcore/stream` and it stays open.
- Open the app in a second tab: `sessions_since_start` rises by one and the click counter starts at 0.
- The access-token header reads **absent**, unless the admin delegated OBO and opened the app **as a
  user** ([../README.md](../README.md#obo-optional)).
- After an **Update**, **Activate** or **Restart** by the admin, a reload shows a fresh
  `sessions_since_start`.
