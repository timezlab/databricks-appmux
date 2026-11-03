# mcp-tools

A small **Model Context Protocol (MCP) server** on Starlette, with four tools. It is the example
for an app that other apps and agents call, and the target the [a2a-agent](../a2a-agent/README.md)
example uses. The general walkthrough (upload, register, Update, roll back, config, share, and what
an end user checks) is in [../README.md](../README.md#admin-walkthrough-in-order); this file holds
what is specific to this app. Which calls between Apps work, and what each failure looks like, is in
[docs/reference/mcp-and-a2a.md](../../docs/reference/mcp-and-a2a.md).

## What it shows

- **MCP over HTTP**: `POST /mcp` takes one JSON-RPC 2.0 message (`initialize`, `ping`, `tools/list`,
  `tools/call`; the notification `notifications/initialized` is accepted). It follows the
  Streamable HTTP transport of the 2025-11-25 revision in its simplest legal form: **stateless**
  (no `Mcp-Session-Id`) and every answer is one JSON body, never an event stream. `GET /mcp` and
  `DELETE /mcp` answer `405`.
- **Four tools**: `add` (two numbers), `utc_now`, `word_count` (a text) and `whoami`. `whoami`
  returns the identity headers of the request that called it, so a caller learns who this server
  sees at the end of a chain of Apps.
- **Identity**: the page and `GET /api/whoami` show `X-Forwarded-Email`, `X-Forwarded-User`,
  `X-Forwarded-Preferred-Username` and `X-Request-Id`, and whether `X-Forwarded-Access-Token` is
  present (never its value).
- **Who authenticates**: the Databricks proxy does, before any code here runs. This app never sees
  the caller's bearer and trusts the headers.
- **Release and config**: `VERSION` in `app.py` (`1.0.0`).
- **Health**: `GET /` and `GET /healthz` answer 200.

## Files

| File | Purpose |
|---|---|
| `app.py` | the whole app: Starlette and the standard library |
| `app.yaml` | `command: ["python", "-m", "uvicorn", "app:app"]` |
| `requirements.txt` | `starlette` and `uvicorn`, both already in the Apps environment |

uvicorn reads its host, port and root path from `UVICORN_HOST`, `UVICORN_PORT` and
`UVICORN_ROOT_PATH`, which the platform and appmux set, so `app.yaml` names none. The page's links
are relative, because appmux never rewrites a response body.

## Run it locally

Use a virtual environment outside `examples/` (`import-dir` uploads everything it finds):

```sh
python3 -m venv /tmp/appmux-mcp-venv
/tmp/appmux-mcp-venv/bin/pip install -r examples/mcp-tools/requirements.txt
cd examples/mcp-tools
PYTHONDONTWRITEBYTECODE=1 /tmp/appmux-mcp-venv/bin/python -m uvicorn app:app --port 8000
```

In another terminal:

```sh
curl -s localhost:8000/api/whoami
curl -s localhost:8000/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"add","arguments":{"a":19,"b":23}}}'
```

The second prints `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"42"}],"isError":false}}`.
To use the official client, install `mcp` in a **different** environment (it needs a newer uvicorn
than the Apps environment holds) and run:

```python
import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(url: str) -> None:
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print([tool.name for tool in tools.tools])
            result = await session.call_tool("add", {"a": 19, "b": 23})
            print(result.content[0].text)


asyncio.run(main(sys.argv[1]))
```

`python client.py http://localhost:8000/mcp` prints the four tool names and `42`.

## Upload and register

Placeholders as in [../README.md](../README.md#before-you-start):

```sh
databricks workspace import-dir examples/mcp-tools \
  /Workspace/Users/<your-email>/appmux-examples/mcp-tools \
  --profile <your-profile> --overwrite
```

Then **Platform** > **Register an app**: Slug `tools`, Display name `Tools (MCP)`, Source folder
`/Workspace/Users/<your-email>/appmux-examples/mcp-tools`, Memory budget (MB) `128`, Owner
`<your-email>`; then open the app and press **Update**. Its MCP endpoint is
`https://<your-appmux-url>/tools/mcp`.

To run it as a **standalone** Databricks App instead (the other half of the comparison), create an
App, upload the same folder and deploy it; its endpoint is `https://<the-app-url>/mcp`.

## Calling it

A caller needs a bearer the Apps proxy accepts and `CAN USE`. From your machine, with your own
token (written to a file so it is not on a command line):

```sh
umask 077
printf 'Authorization: Bearer %s\n' \
  "$(databricks auth token --profile <your-profile> -o json | jq -r .access_token)" > /tmp/appmux-auth-header
curl -s -H @/tmp/appmux-auth-header -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  https://<your-appmux-url>/tools/mcp \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"whoami","arguments":{}}}'
rm /tmp/appmux-auth-header
```

Leave out the header file and the answer is `401` with body `{}` (or a `302` to a sign-in page if
you also leave out `Accept`).

**From another App, including another child of appmux**, the caller's identity is a service
principal and two more things are needed; the steps are in
[the reference](../../docs/reference/mcp-and-a2a.md#setting-up-the-shared-sp-for-a-child): `CAN USE`
for that service principal on the target (on appmux itself when the target is a child), and an
`everyone` grant on this child, because a `user` grant cannot name a service principal. Without the
grant appmux answers `404`.

## As an admin, try

1. **Update** with the folder unchanged: "already up to date", no new release.
2. Set `VERSION = "2.0.0"`, `import-dir` again and **Update**. The page shows the new version.
3. Call `whoami` as yourself (above): `x_forwarded_email` is your email, `x_forwarded_prefix` is
   `/tools`, and `access_token_header_present` is `false` (the token is stripped unless OBO is
   delegated).
4. Call it from the [a2a-agent](../a2a-agent/README.md) child. `whoami` then shows a UUID where the
   email was: the agent's service principal.
5. Remove the `everyone` grant and call it again from the agent: `404`.

## As an end user, check that

- `/<slug>/` loads and **You are** shows your email.
- The links **api/whoami** and **healthz** work and stay under `/<slug>/`.
- `GET /<slug>/mcp` answers `405`; there is nothing to open in a browser beyond the page.

## What was verified

- **Locally** (Python 3.11, Starlette 0.38.6 and uvicorn 0.30.6 as in the Apps environment, 27 unit
  tests in `tests/unit/test_example_mcp_tools.py`): initialize and its protocol-version rules, list,
  a call of each tool, the error answers (unknown method, unknown tool, bad arguments, a broken
  message, a body over the limit, an unsupported protocol-version header, a foreign `Origin`), the
  `202` for a notification or a client response, `405` for `GET` and `DELETE`, and `whoami` never
  echoing the token. The official `mcp` 1.30.0 client initialised, listed the tools and called
  `add` against the running server.
- **On the Free Edition workspace, 2026-11-03**, as a child (slug `tools`, 128 MB) and as a
  standalone App: called with a user token (plain HTTP requests), and by the a2a-agent example with
  a service principal token, in every combination in
  [the reference](../../docs/reference/mcp-and-a2a.md#calls-between-apps-measured).

## What was not verified

A tool call that lasts near the proxy's 300 s limit, load, a client that opens `GET /mcp` for a
stream (this server refuses), and a session id (this server issues none). The page as rendered in
a browser was not looked at.
