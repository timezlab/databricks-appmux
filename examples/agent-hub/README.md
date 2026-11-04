# agent-hub

One App that serves **MCP** (`/mcp`) and **A2A 1.0** (`/`) at the same time, with a free Databricks
foundation model as its agent. Its tools run **as the caller**: they report the identity the hub
received and make a Databricks call with the caller's own token when one arrived. It is the callee
of [agent-console](../agent-console/README.md), and together they answer one question: when an App
calls another App over MCP or A2A, can the called App tell who is behind the call, and act as them?
The general walkthrough (upload, register, Update, roll back, config, share) is in
[../README.md](../README.md#admin-walkthrough-in-order); this file holds what is specific to this
app. Which calls between Apps work, and what each failure looks like, is in
[docs/reference/mcp-and-a2a.md](../../docs/reference/mcp-and-a2a.md).

## What it shows

- **MCP, Streamable HTTP, stateless**: `POST /mcp` with `initialize`, `ping`, `tools/list` and
  `tools/call`. `GET` and `DELETE` answer 405, batches and an unsupported `MCP-Protocol-Version`
  answer 400, a notification answers 202. A client such as the official `mcp` package works.
- **A2A 1.0, JSON-RPC binding**: the card at `/.well-known/agent-card.json` and `POST /` with
  `SendMessage`, `SendStreamingMessage` and `GetTask`. Under appmux the card is served at
  `/<slug>/.well-known/agent-card.json` only.
- **Four tools**, the same through both protocols:

  | Tool | What it returns |
  |---|---|
  | `whoami` | the identity headers the hub received (`X-Forwarded-Email`, `-User`, `-Preferred-Username`, `-Prefix`), the request id, and whether `X-Forwarded-Access-Token` is present (never its value) |
  | `databricks_me` | `GET /api/2.0/preview/scim/v2/Me` made **with the caller's forwarded token**: who Databricks says the caller is. Without a token it says so and names OBO; if the token lacks the scope it says Databricks refused |
  | `add` | the sum of two numbers |
  | `utc_now` | the time |

- **Two skills over A2A**: `ask` (the text goes to the model, which may call the four tools, at most
  4 rounds) and `{"skill": "call-tool", "tool": "...", "arguments": {...}}` (one tool, no model).
- **Two identities in one request.** The model is called with the **App's own** identity (the
  Databricks SDK's `Config()`), because the caller's token is scoped to what the caller delegated.
  The tools run with the **caller's**. Both appear in one `ask`: the answer to "who am I" comes from
  the caller's token while the sentence is written by a model the App paid for.
- **Limits**: the newest 100 tasks are kept in memory, each visible only to the caller that made it
  (a restart loses them), 4,000 characters of text, 64 KiB of request body, and 3 retries (2, 4,
  8 s) when the workspace answers `429 REQUEST_LIMIT_EXCEEDED`.
- **Health**: `GET /` and `GET /healthz` answer 200.

| Setting (`app.yaml`, overridable in the release config) | Default | Meaning |
|---|---|---|
| `AGENT_NAME` | `agent-hub` | the name in the card and in `serverInfo` |
| `LLM_ENDPOINT` | `databricks-gpt-oss-120b` | the serving endpoint of the model; it must support tool calls |
| `A2A_PUBLIC_URL` | `DATABRICKS_APP_URL` | the address in the card; the platform sets the default (`<appmux URL>/<slug>` for a child) |

## Files

| File | Purpose |
|---|---|
| `app.py` | the whole app: Starlette, httpx, and the Databricks SDK only to find credentials |
| `app.yaml` | `command: ["python", "-m", "uvicorn", "app:app"]` and the settings above |
| `requirements.txt` | `starlette`, `uvicorn`, `httpx`, `databricks-sdk`; all already in the Apps environment |

## Run it locally

```sh
python3 -m venv /tmp/appmux-agent-venv
/tmp/appmux-agent-venv/bin/pip install -r examples/agent-hub/requirements.txt
cd examples/agent-hub
PYTHONDONTWRITEBYTECODE=1 DATABRICKS_CONFIG_PROFILE=<your-profile> \
  /tmp/appmux-agent-venv/bin/python -m uvicorn app:app --port 8103
```

Then, in another terminal, the tool that needs no model, as someone the hub can name:

```sh
curl -s localhost:8103/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'X-Forwarded-Email: dana@example.com' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"whoami","arguments":{}}}'
```

Locally nothing sets the identity headers, so you type them; behind the Apps proxy a client cannot
(the proxy replaces them, and appmux drops the client's copies).

## Upload and register

Placeholders as in [../README.md](../README.md#before-you-start):

```sh
databricks workspace import-dir examples/agent-hub \
  /Workspace/Users/<your-email>/appmux-examples/agent-hub \
  --profile <your-profile> --overwrite
```

Then **Platform** > **Register an app**: Slug `hub`, Display name `Agent hub`, Source folder
`/Workspace/Users/<your-email>/appmux-examples/agent-hub`, Memory budget (MB) `256`, Owner
`<your-email>`; then open the app and press **Update**.

What the hub needs from the platform, in order (the API calls are in
[the reference](../../docs/reference/mcp-and-a2a.md#setting-up-the-shared-sp-for-a-child)):

1. **The model.** A child has no credentials, so `ask` fails with "no Databricks credentials" until
   a platform admin turns on `shared_sp_delegation_allowed`, delegates the shared service principal
   to `hub`, and the app is restarted. `whoami`, `add`, `utc_now` and `tools/list` need none.
2. **A caller's token reaching the tools.** `databricks_me` has a token to use only when a platform
   admin delegated **OBO** to `hub` (a per-app flag), and the token carries only the scopes in the
   `user_api_scopes` of the appmux App. Without OBO, `whoami` still shows who called, and
   `access_token_header_present` is `false`.
3. **Who may call.** appmux answers 404 to an identity with no grant on `hub`. Give the people and
   [agent-console](../agent-console/README.md)'s identity a grant: `everyone`, or a
   `service_principal` grant for the console's service principal.

## As an end user check that

- `/<slug>/` loads and **You are** shows your email.
- `/<slug>/.well-known/agent-card.json` returns the card, and its interface URL is under `/<slug>/`.
- `/<slug>/api/status` never shows a token or a secret.

## What was verified

- **Locally** (39 unit tests in `tests/unit/test_example_agent_hub.py`): the card; the MCP handshake
  and its errors; `whoami` and `databricks_me` over both protocols; a task that uses the model's
  answer and the caller's token together; a tool that fails; a tool the model invented; the retry on
  `429`; missing credentials; `GetTask` limited to the caller's own tasks; streaming; no response
  shows a token. The model and the workspace are fakes in those tests.
- **On a workspace** (Free Edition dev App, 2026-11-04, as a child with OBO and the shared service
  principal delegated, called with the CLI's token): the card, MCP `initialize`, `whoami` and
  `databricks_me` over MCP and A2A, and `ask` with the free model, which called both tools; see
  [What was measured](../agent-console/README.md#what-was-measured).
