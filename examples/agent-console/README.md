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
    service principal. With OBO delegated to the hub, that service principal's own token is
    forwarded too, and `databricks_me` acts as it. It is the one shared by every delegated child,
    so the hub cannot tell which child called.
- **The identity check**: `POST /api/identity-check` calls the hub eight times (MCP and A2A, each
  as me and as this App, each with `whoami` and `databricks_me`) and the page draws a 2×2 table:
  who the hub saw, whether a token arrived, and whether it is you. "Who the hub saw" is the owner of
  the token, as Databricks reports it to the hub (`whoami`'s `token_owner`), marked "from the
  token". Only when no token came does the table fall back to the proxy's header, marked "from the
  header"; a token Databricks refused names nobody and the header is not used in its place.
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

On 2026-11-04, on the Free Edition dev App, with `hub` and `console` registered as children and OBO
and the shared service principal delegated to both, the Databricks CLI's token (a person, no browser)
as the caller: **me** over MCP and over A2A, for `whoami` and `databricks_me`, all matched the caller
(the hub saw the person, received their token, and `databricks_me` returned their user name); `ask`
over A2A as **me** made the model call both tools and name the person; **this App** over both
protocols answered `404` because the hub had no grant for it. The table is in
[the reference](../../docs/reference/mcp-and-a2a.md#who-the-target-sees-when-the-caller-is-an-app).

Later the same day the owner pressed **Run the identity check** in a browser, as the owner of both
apps and as a second account:

- **me** matched the person in every cell, for both accounts, over both protocols.
- With only the console shared with the second account, every cell was `404`: appmux checks the
  hub's grants against the person behind the forwarded token. Sharing the hub too turned all of them
  into successes, so being let into the console gives no access to the hub.
- **this App** was `404` until the hub had a grant for it. With an `everyone` grant the hub saw
  appmux's shared service principal, a token was present, and `databricks_me` returned that service
  principal.

The full table, and one value no one has explained yet (`X-Forwarded-Preferred-Username` was
`anonymous` for the owner), are in the reference.

**Not measured**: **this App** with a `service_principal` grant (ADR 0020) in place of `everyone`.

## What was verified

- **Locally** (31 unit tests in `tests/unit/test_example_agent_console.py`): the console against the
  real hub behind a fake Databricks proxy that strips the slug, sets the identity headers and hands
  the hub the user's token only when OBO is delegated; every cell of the table in both protocols
  and both identities; no user token, no hub address, a `302`, a `401`, a `404` and missing
  credentials each reported plainly; validation; a cross-site `POST` refused; no token in any
  response; the page escapes the email and uses relative URLs.
