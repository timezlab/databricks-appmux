# a2a-agent

An **Agent2Agent (A2A) server** on Starlette whose agent is a **free Databricks foundation model**
that can call the tools of an MCP server such as [mcp-tools](../mcp-tools/README.md). It is the
example for an app that makes calls to other Apps and to a serving endpoint with its **own**
identity, and for checking MCP and A2A between Apps inside appmux and outside it. The general
walkthrough (upload, register, Update, roll back, config, share, and what an end user checks) is in
[../README.md](../README.md#admin-walkthrough-in-order); this file holds what is specific to this
app. Which calls between Apps work, and what each failure looks like, is in
[docs/reference/mcp-and-a2a.md](../../docs/reference/mcp-and-a2a.md).

## What it shows

- **A2A 1.0, JSON-RPC binding**: the card at `/.well-known/agent-card.json` and `POST /` with
  `SendMessage`, `SendStreamingMessage` (Server-Sent Events: the task, a status update per step, the
  answer, the end) and `GetTask`. Other A2A methods answer "unsupported". A client written for
  that version, such as the official `a2a-sdk`, works against it. Under appmux the card is served at
  `/<slug>/.well-known/agent-card.json` only: appmux routes nothing at the root of its host.
- **Three skills**, chosen by a `data` part of the message (no part: `ask`):
  - `ask`: the text goes to the model (`LLM_ENDPOINT`). If `MCP_SERVER_URL` is set, the model is
    offered that server's tools and may call them (at most 4 rounds), so one answer crosses three
    hops: a caller, this App, an MCP App.
  - `{"skill": "call-tool", "tool": "...", "arguments": {...}}`: calls one MCP tool and returns its
    text, with no model, to test the MCP hop alone.
  - `{"skill": "relay", "text": "..."}`: sends the text to another A2A agent (`A2A_PEER_URL`) and
    returns its answer, to test the agent-to-agent hop alone.
- **Its own identity**: every outgoing call is signed by the Databricks SDK's `Config()`, never with
  the caller's token. A standalone App gets its service principal from the platform; a child under
  appmux gets none unless a platform admin delegates the shared service principal
  ([design 8.1](../../docs/product/v1-design.md)). `GET /api/status` says whether credentials are
  found and which auth type, never a token.
- **A refusal, said plainly**: a peer that answers `302` (or `401` with `{}`) did not accept this
  App's token or this identity lacks `CAN USE` on it. The agent reports that and does not follow the
  redirect.
- **Limits**: the newest 100 tasks are kept in memory, each visible only to the caller that made it
  (a restart loses them), 4,000 characters of text, 64 KiB of request body, 600 answer tokens, and 3 retries (2, 4, 8 s) when the
  workspace answers `429 REQUEST_LIMIT_EXCEEDED`.
- **Identity, release and config**: `GET /` and `GET /api/status` show the identity headers as they
  arrive, `VERSION` in `app.py` (`1.0.0`) and the settings below.
- **Health**: `GET /` and `GET /healthz` answer 200.

| Setting (`app.yaml`, overridable in the release config) | Default | Meaning |
|---|---|---|
| `AGENT_NAME` | `a2a-agent` | the name in the card |
| `LLM_ENDPOINT` | `databricks-gpt-oss-120b` | the serving endpoint of the model |
| `MCP_SERVER_URL` | empty | the `/mcp` address of an MCP server; empty means no tools |
| `A2A_PEER_URL` | empty | the base address of another A2A agent; empty means `relay` is refused |
| `A2A_PUBLIC_URL` | `DATABRICKS_APP_URL` | the address in the card; the platform sets the default (`<appmux URL>/<slug>` for a child) |

## The model

The agent calls `POST {workspace}/serving-endpoints/chat/completions` with `model` set to
`LLM_ENDPOINT`. The Free Edition workspace has pay-per-token endpoints, with a **workspace-wide
limit on queries per second**. Seen on 2026-11-03: `databricks-gpt-oss-120b` ran every check below
(it supports tool calls); `databricks-meta-llama-3-3-70b-instruct` answered `429` on the first
question, which is why the default is gpt-oss. A relay between two agents succeeded in 4 of 8 runs
before the agent retried a `429` and in 8 of 8 after. Other chat endpoints of that workspace (gpt-oss-20b,
qwen3-next-80b, llama-4-maverick and others) were listed, not run. Whether a model calls tools
well is its own matter; check the endpoint you choose.

## Files

| File | Purpose |
|---|---|
| `app.py` | the whole app: Starlette, httpx, and the Databricks SDK only to find credentials |
| `app.yaml` | `command: ["python", "-m", "uvicorn", "app:app"]` and the settings above |
| `requirements.txt` | `starlette`, `uvicorn`, `httpx`, `databricks-sdk`; all already in the Apps environment |

## Run it locally

Use a virtual environment outside `examples/`, with the versions of the Apps environment:

```sh
python3 -m venv /tmp/appmux-agent-venv
/tmp/appmux-agent-venv/bin/pip install -r examples/a2a-agent/requirements.txt
cd examples/a2a-agent
```

With a Databricks CLI profile the agent can use the real model. In one terminal, the MCP server
([mcp-tools](../mcp-tools/README.md#run-it-locally)) on port 8101; in another, the agent:

```sh
PYTHONDONTWRITEBYTECODE=1 DATABRICKS_CONFIG_PROFILE=<your-profile> \
  MCP_SERVER_URL=http://localhost:8101/mcp \
  /tmp/appmux-agent-venv/bin/python -m uvicorn app:app --port 8102
```

To try the `call-tool` skill with **no Databricks workspace at all**, give it a placeholder host and
token instead of the profile: the local MCP server ignores the bearer.

```sh
DATABRICKS_HOST=http://localhost:1 DATABRICKS_TOKEN=placeholder MCP_SERVER_URL=http://localhost:8101/mcp ...
```

Then, in a third terminal:

```sh
curl -s localhost:8102/.well-known/agent-card.json
curl -s localhost:8102/ -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' -d '{
  "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
  "params": {"message": {"messageId": "m-1", "role": "ROLE_USER",
    "parts": [{"text": "Use your tools to add 19 and 23 and answer in one sentence."}]}}}'
```

The answer is a task in `TASK_STATE_COMPLETED` whose artifact reads like "The sum of 19 and 23 is
42." To use the official client, install `a2a-sdk` in a **different** environment and run:

```python
import asyncio
import sys

from a2a.client import ClientConfig, create_client
from a2a.types import Message, SendMessageRequest
from google.protobuf import json_format


async def main(url: str) -> None:
    # create_client reads the card at <url>/.well-known/agent-card.json
    client = await create_client(url, ClientConfig(streaming=False))
    message = json_format.ParseDict(
        {
            "messageId": "m-1",
            "role": "ROLE_USER",
            "parts": [
                {"data": {"skill": "call-tool", "tool": "add", "arguments": {"a": 19, "b": 23}}}
            ],
        },
        Message(),
    )
    async for event in client.send_message(SendMessageRequest(message=message)):
        if event.HasField("task"):
            task = json_format.MessageToDict(event.task)
            print(task["status"]["state"], task.get("artifacts", [{}])[0].get("parts"))
    await client.close()


asyncio.run(main(sys.argv[1]))
```

`python client.py http://localhost:8102` prints `TASK_STATE_COMPLETED [{'text': '42'}]`. With
`ClientConfig(streaming=True)` (the default) the same call yields one event per step.

## Upload and register

Placeholders as in [../README.md](../README.md#before-you-start):

```sh
databricks workspace import-dir examples/a2a-agent \
  /Workspace/Users/<your-email>/appmux-examples/a2a-agent \
  --profile <your-profile> --overwrite
```

Then **Platform** > **Register an app**: Slug `agent`, Display name `Agent (A2A)`, Source folder
`/Workspace/Users/<your-email>/appmux-examples/a2a-agent`, Memory budget (MB) `256`, Owner
`<your-email>`; then open the app and press **Update**. The card is at
`https://<your-appmux-url>/agent/.well-known/agent-card.json` and the endpoint is
`https://<your-appmux-url>/agent/`. The agent ran under a 256 MB budget; the smallest budget that
works was not looked for.

### Give it a way to call things

Registered and Updated, the agent starts and serves its card, but a child has **no credentials**:
`ask`, `call-tool` and `relay` fail with "no Databricks credentials". Do these in order (the API
calls are in [the reference](../../docs/reference/mcp-and-a2a.md#setting-up-the-shared-sp-for-a-child)):

1. A platform admin turns on `shared_sp_delegation_allowed`, then delegates the shared service
   principal to `agent` (or an app admin asks for it on the app page and a platform admin approves).
2. **Restart** `agent`. Until then `/agent/api/status` still reads `configured: false`.
3. Register [mcp-tools](../mcp-tools/README.md#upload-and-register) as `tools`, then set the
   environment entry `MCP_SERVER_URL` of `agent` to `https://<your-appmux-url>/tools/mcp` with the
   config call in the [main walkthrough](../README.md#admin-walkthrough-in-order) (step 7). The
   release the call creates restarts the agent.
4. Give appmux's own service principal `CAN USE` on appmux itself (the shared service principal is
   appmux's own), and add an `everyone` grant on `tools`.

The same folder runs as a **standalone** App: create an App, upload the folder, set the same
environment entries in its `app.yaml`, deploy. It has its own service principal from the platform,
so only the `CAN USE` grants (on appmux for a child target, on the target App otherwise) are left.

## As an admin, try

1. `GET /agent/api/status` as yourself: `configured`, the auth type (`databricks-cli` under appmux,
   `oauth-m2m` standalone), `mcp_server_configured`, and `access_token_header_present: false`.
2. Send the `call-tool` message above to `https://<your-appmux-url>/agent/` with your token (the
   header file of the [mcp-tools README](../mcp-tools/README.md#calling-it) works the same):
   `TASK_STATE_COMPLETED` with `42`.
3. Take away `CAN USE` for appmux's service principal on the target and send it again: the task
   fails and says the proxy did not accept this App's token (`401` or `302`). Give it back.
4. Remove the `everyone` grant on `tools`: the task fails with `404`. Put it back.
5. Send a text message with no data part: the model decides to call `add` and answers in words.
6. Relay between two agents: set `A2A_PEER_URL` of the child to the standalone agent's address and
   of the standalone agent to `https://<your-appmux-url>/agent`, then send
   `{"skill": "relay", "text": "Use your tools to add 5 and 6."}` to each in turn.
7. Open the card in a browser at the root of your appmux host,
   `https://<your-appmux-url>/.well-known/agent-card.json`: `404`. Only the card under `/agent/`
   exists.

## As an end user check that

- `/<slug>/` loads and **You are** shows your email.
- `/<slug>/.well-known/agent-card.json` returns the card, and its interface URL is under `/<slug>/`.
- The links stay under `/<slug>/`.
- `/agent/api/status` never shows a token or a secret.

## What was verified

- **Locally** (Python 3.11, Starlette 0.38.6, uvicorn 0.30.6, httpx and databricks-sdk 0.33.0 as in
  the Apps environment; 62 unit tests in `tests/unit/test_example_a2a_agent.py`, checked by deliberately
  breaking the code and watching them fail): the card; status without the token; `ask` with a tool call, a block
  style content answer, no MCP server, an invented tool, bad arguments, the tool round limit and tool
  errors; `call-tool`; `relay` (success, a failed peer, no peer); the MCP handshake order and a
  JSON or event-stream answer matched to its request id; a redirect and a `401` reported and not
  followed; missing credentials; a short body in a `4xx` error; an unexpected exception hidden from
  the caller; streaming event order and failure; `GetTask` limited to the caller's own tasks; the
  task cap; `A2A-Version` and unsupported-method errors; invalid messages; the retry on `429`. The
  official `a2a-sdk` 1.3.0 client read the card, sent `SendMessage` and `SendStreamingMessage` and
  called `GetTask`; the official `mcp` 1.30.0 client reached mcp-tools; two agents relayed to each
  other, with the free model.
- **On the Free Edition workspace, 2026-11-03**: the child and the standalone agent each answered
  `ask` with the free model and a tool of an MCP server (`The sum of 19 and 23 is 42.`), ran
  `call-tool` against a child MCP server and a standalone one, and relayed to each other in both
  directions; the failures in the table of
  [the reference](../../docs/reference/mcp-and-a2a.md#what-each-failure-looks-like) appeared as
  written when a `CAN USE` or the grant was missing. `SendStreamingMessage` through the Apps proxy
  and appmux delivered its events to the official client.

## What was not verified

Whether streamed events arrive while the model is still working or only at the end; a model other
than gpt-oss-120b on this agent end to end; an `ask` that lasts near the proxy's 300 s limit; many
agents sharing the workspace's query limit; OBO (an agent acting for the visitor); a `group` grant
admitting a service principal; A2A push notifications and the other A2A methods (this agent
answers them "unsupported"); the page as rendered in a browser.
