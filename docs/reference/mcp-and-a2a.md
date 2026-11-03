# MCP and A2A between Databricks Apps, inside and outside appmux

MCP (Model Context Protocol) and A2A (Agent2Agent) are JSON-RPC 2.0 messages in ordinary HTTP
requests. Whether app A can call app B is therefore not a protocol question. It is three questions
about the hops, and the answers below were **measured on 2026-11-03** on the Free Edition workspace
with the two example apps [mcp-tools](../../examples/mcp-tools/README.md) and
[a2a-agent](../../examples/a2a-agent/README.md). Each ran twice: as a child of appmux and as a
standalone Databricks App. The official `mcp` 1.30.0 and `a2a-sdk` 1.3.0 clients also ran against
them. Anything not in the measured tables is marked as such; the list of what was not measured is
at the end.

## The three hops

A call from a caller to an App passes three checks, in this order:

1. **Network.** The caller can open a connection to the App's public URL. The agents called other
   Apps by their public URLs from a child and from a standalone App; both worked. The design
   blocks child-to-child traffic inside the sandbox ([design 5](../product/v1-design.md); not
   tried here), so a child calls a sibling at the sibling's address on appmux, the public URL a
   browser uses. That worked.
2. **A bearer the Apps proxy accepts.** An OAuth token of a user or of a service principal (SP).
   Databricks documents that a personal access token does not work for MCP on Apps (not tried
   here). The proxy checks the bearer before any App code runs.
3. **Permission on the target.** The caller's identity needs `CAN USE` on the target App. Inside
   appmux there is a fourth check, the child's own grants ([design 9](../product/v1-design.md)).

## What each failure looks like

| What the caller gets | Cause | Fix |
|---|---|---|
| `302` to the OIDC sign-in page | no bearer, a bad bearer, or an SP with no `CAN USE` on the target, **and** the request has no `Accept` header or accepts `text/html` | give the caller a token the proxy accepts and `CAN USE` |
| `401` with body `{}` | the same causes, when the request's `Accept` includes `application/json` (MCP and A2A clients send it) | the same |
| `404` `not found` from appmux | the proxy let the call through, but the identity has no grant on the child | an `everyone` grant on the child (below) |
| `200` | all hops passed | |
| a task that fails with "no Databricks credentials" (the example agent) | a child with no delegated credential, or the child was not restarted after the delegation | delegate the shared SP, then restart the child |
| `429` `REQUEST_LIMIT_EXCEEDED` from a serving endpoint | the Free Edition workspace limit on queries per second | wait and retry; the example agent retries 3 times |

Spike 8 recorded only the `302`, because its probe sent no `Accept` header
([log](../../specs/001-appmux-v1/spikes/README.md#spike-8--behaviour-of-the-databricks-proxy-2026-10-30)).

## Who the target sees

| Caller | `X-Forwarded-Email` | `X-Forwarded-Access-Token` |
|---|---|---|
| a person with a user token, to a standalone App | their email | present |
| the same, to a child under appmux | their email | **absent**, unless OBO is delegated to the child |
| an SP, to a standalone App | the SP's application ID, **a UUID, not an email** | present |
| an SP, to a child under appmux | the same UUID | **absent** |

For an SP, `X-Forwarded-User` has the form `<id>@<id>` and `X-Forwarded-Preferred-Username` is the App's
display name.

## Calls between Apps, measured

Every row was a successful call (`add` = 42, or an answer from the model that used the tool) unless
the last column says otherwise. "Agent" is the a2a-agent example, "MCP" is mcp-tools.

| From | To | What it needed | Without it |
|---|---|---|---|
| laptop, user token | standalone MCP, child MCP, standalone agent, child agent | nothing but the user's own `CAN USE` | |
| child agent, shared SP | standalone MCP | `CAN USE` for appmux's SP on the target App | `401` `{}` |
| child agent, shared SP | child MCP, by appmux's address | `CAN USE` for appmux's SP **on appmux**, and an `everyone` grant on the child | `401` `{}` without the first, `404` without the second |
| standalone agent, its own SP | standalone MCP, child MCP | `CAN USE` for its SP on the target (on appmux for a child), and the `everyone` grant for a child | `401` `{}` without the first, `404` without the second |
| standalone agent | child agent, `relay` | the same as above | `404` |
| child agent | standalone agent, `relay` | `CAN USE` for appmux's SP on the standalone agent | |
| child agent and standalone agent | a free foundation model (`databricks-gpt-oss-120b`) | nothing for the standalone agent; the shared SP delegation for the child | "no Databricks credentials" |

Also measured:

- `SendStreamingMessage` through the Apps proxy and appmux delivered the task, the status updates
  and the answer as separate Server-Sent Events. Whether they arrived while the model was still
  working, or only at the end, was not checked.
- The agent card of a child is at `/<slug>/.well-known/agent-card.json`. appmux answers `404` for
  `/.well-known/agent-card.json` at the root of its host, so a client that finds a card by the host
  name alone cannot find a child's card.
- A child's `DATABRICKS_APP_URL` is `<appmux URL>/<slug>`, which the agent uses as its card URL.

## Setting up the shared SP for a child

A child has no SP of its own. To let one call the model or another App:

1. A platform admin turns on `shared_sp_delegation_allowed`
   (`PUT /_appmux/api/v1/platform/settings/shared_sp_delegation_allowed`, body `{"value": true}`).
2. A platform admin delegates it to the child
   (`PUT /apps/<slug>/delegation/shared_sp`, body `{"version": <the app's version>, "reason": "..."}`).
3. **Restart the child** (`POST /apps/<slug>/restart`). The delegation does not restart it; until it
   restarts, the child reads as having no credentials. The Databricks SDK then finds the identity
   through `Config()` with no change to the child's source (auth type `databricks-cli`).
4. `CAN USE` for appmux's own SP (the one the shared SP is) on every other App the child calls,
   including appmux itself: `databricks apps update-permissions <target> --json` with the SP's
   application ID.
5. For a child that is called, an `everyone` grant (`PUT /apps/<slug>/grants`, body
   `{"type": "everyone", "name": "*", "permission": "CAN_USE"}`). A `user` grant cannot name an SP:
   it takes a lower-case email and the SP's address is a UUID. A `group` grant for an SP was not
   tried.

An `everyone` grant admits every identity the proxy accepts, people included. That is the cost of
letting an SP caller in.

## Not measured

- The 300 s cut of the Apps proxy for a call that lasts that long (measured in spike 8 for a plain
  stream, not for MCP or A2A), and whether a stream that sends nothing for a while survives.
- A user bearer passed on to a child by OBO, and an agent that calls on behalf of the visitor.
- A `group` grant admitting an SP, and the identity of an SP in appmux's audit and metrics.
- Load: the cost of the extra hop through appmux, and what the free model's limit does to several
  agents sharing the workspace.
- Streamable HTTP with a session id or a server-sent stream from the MCP server (the example is
  stateless and answers with one JSON body), and A2A push notifications.
- Any workspace other than the Free Edition one, and Databricks' own managed MCP servers.
