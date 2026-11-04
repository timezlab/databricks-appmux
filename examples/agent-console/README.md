# agent-console

A web page that calls [agent-hub](../agent-hub/README.md) over **MCP** and over **A2A**, once with
**your own token** and once with **its own identity**, and shows what the hub saw each time. It is
the example for one question: **when an App calls another App, does the called App know who is
behind the call?** The general walkthrough (upload, register, Update, roll back, config, share) is
in [../README.md](../README.md#admin-walkthrough-in-order); this file holds what is specific to this
app. Which calls between Apps work, and what each failure looks like, is in
[docs/reference/mcp-and-a2a.md](../../docs/reference/mcp-and-a2a.md).

## What it shows

- **Two ways to sign a call.**
  - **me**: the Databricks proxy gives an App that was delegated OBO the visitor's token in
    `X-Forwarded-Access-Token`. The console sends it on as the bearer of the call to the hub, so
    the hub receives the visitor as its caller and can act as them.
  - **this App**: the console's own identity, found by the Databricks SDK the way it finds it for
    any App. Under appmux that needs the shared service principal delegated. The hub then sees a
    service principal, and `databricks_me` has no user token to use.
- **The identity check**: `POST /api/identity-check` calls the hub eight times (MCP and A2A, each
  as me and as this App, each with `whoami` and `databricks_me`) and the page draws a 2×2 table:
  who the hub saw, whether a user token arrived, whether Databricks agrees, and whether it is you.
- **One call**: pick a protocol, an identity and an action (`whoami`, `databricks_me`, `discover`,
  and over A2A `ask`, which sends a sentence to the hub's model) and see the wire steps with their
  HTTP status, the answer, and the task details.
- **Plain refusals**: a `302` or `401` from the Databricks proxy is reported as "it did not accept
  this token as a caller, or this identity lacks CAN USE"; a `404` as "wrong address, or appmux
  hides the hub from this identity". Nothing is retried and no redirect is followed.
- **Safe by construction**: the two `POST` routes act with your token, so they take only a JSON body
  from this page (`Sec-Fetch-Site` absent or `same-origin`); the page draws answers with
  `textContent`; no token is ever echoed, logged or returned.
- **Health**: `GET /` and `GET /healthz` answer 200.

| Setting (`app.yaml`, overridable in the release config) | Default | Meaning |
|---|---|---|
| `PROVIDER_URL` | empty | the address of the hub as the caller reaches it, `https://<your-appmux-url>/<hub-slug>`; empty makes every call say it is not set |

## Files

| File | Purpose |
|---|---|
| `app.py` | the whole app: Starlette, httpx, a vanilla-JS page, and the Databricks SDK only to find credentials |
| `app.yaml` | `command: ["python", "-m", "uvicorn", "app:app"]` and `PROVIDER_URL` |
| `requirements.txt` | `starlette`, `uvicorn`, `httpx`, `databricks-sdk`; all already in the Apps environment |

## Run it locally

Run the hub first ([its README](../agent-hub/README.md#run-it-locally)) on port 8103, then:

```sh
cd examples/agent-console
PYTHONDONTWRITEBYTECODE=1 DATABRICKS_CONFIG_PROFILE=<your-profile> \
  PROVIDER_URL=http://localhost:8103 \
  /tmp/appmux-agent-venv/bin/python -m uvicorn app:app --port 8104
```

Locally no proxy sets `X-Forwarded-Access-Token`, so **me** answers "no user token"; **this App**
works with the profile. The behind-the-proxy case needs a deployment.

## Upload and register

```sh
databricks workspace import-dir examples/agent-console \
  /Workspace/Users/<your-email>/appmux-examples/agent-console \
  --profile <your-profile> --overwrite
```

**Platform** > **Register an app**: Slug `console`, Display name `Agent console`, Source folder
`/Workspace/Users/<your-email>/appmux-examples/agent-console`, Memory budget (MB) `256`, Owner
`<your-email>`; **Update**. Then, in order:

1. Register and Update [agent-hub](../agent-hub/README.md#upload-and-register) as `hub`.
2. Set the console's `PROVIDER_URL` to `https://<your-appmux-url>/hub` with the config call in the
   [main walkthrough](../README.md#admin-walkthrough-in-order) (step 7). The release it creates
   restarts the console.
3. Delegate the shared service principal to **both** apps and restart them (**this App** and the
   hub's model both need it).
4. Delegate **OBO** to **both** apps (a platform admin, per app), and make sure the appmux App
   declares the scopes the hub's `databricks_me` needs in its `user_api_scopes`. A changed scope
   reaches tokens only after the appmux App is stopped and started.
5. Give the console's identities a grant on `hub`: the visitor needs one to reach the console and
   to be admitted by appmux as a caller of the hub; the shared service principal needs `everyone`
   or a `service_principal` grant.

## As an end user check that

- `/<slug>/` loads and **You are** shows your email.
- The links and requests stay under `/<slug>/` (the page uses relative URLs only).
- Open the page in a browser as yourself, press **Run the identity check** and read the table.

## What was measured

On 2026-11-04, on the Free Edition dev App, with `hub` and `console` registered as children and
released, **no delegation given**: the hub answered its card, MCP `initialize` and `whoami` over MCP
and A2A to the Databricks CLI's bearer token, and saw the person's e-mail but no user token. The
table is in [the reference](../../docs/reference/mcp-and-a2a.md#who-the-target-sees-when-the-caller-is-an-app).

**Not measured**: the console's own table on the dev App (it needs the delegations of step 3 and 4
above and a browser), the shared service principal as the console's identity, the visitor's OBO token
as the bearer of a call to the hub, `databricks_me` with that token, and a `service_principal` grant
admitting the console. Until those run, which of the four cells shows "The hub sees you" is a
prediction, not a result.

## What was verified

- **Locally** (31 unit tests in `tests/unit/test_example_agent_console.py`): the console against the
  real hub behind a fake Databricks proxy that strips the slug, sets the identity headers and hands
  the hub the user's token only when OBO is delegated; every cell of the table in both protocols
  and both identities; no user token, no hub address, a `302`, a `401`, a `404` and missing
  credentials each reported plainly; validation; a cross-site `POST` refused; no token in any
  response; the page escapes the email and uses relative URLs.
