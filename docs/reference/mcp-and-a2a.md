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
| an SP, to a child under appmux | the same UUID | **absent**, unless OBO is delegated to the child: then the SP's own token (measured 2026-11-04, below) |

For an SP, `X-Forwarded-User` has the form `<id>@<id>` and `X-Forwarded-Preferred-Username` is the App's
display name. The SP that calls from a child is appmux's shared one, so the callee sees the same
UUID whichever child called.

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
letting an SP caller in with it.

Since [ADR 0020](../decisions/0020-let-a-service-principal-hold-an-app-grant.md) (accepted 2026-11-06,
proposed 2026-11-03) a narrower grant exists: `{"type": "service_principal", "name": "<the SP's application
ID, lower case>", "permission": "CAN_USE"}` admits that one SP and nobody else. It is in the code
and its tests, and a call through the Apps proxy with it was measured on 2026-11-04 (one SP in, a
different application id still `404`, removal closes the child within a second;
[ADR 0020](../decisions/0020-let-a-service-principal-hold-an-app-grant.md#measured-on-2026-11-04)).
Taking back the SP's `CAN USE` on appmux is slow: the proxy kept admitting it for about an hour, so
remove the grant first.

## Who the target sees when the caller is an App

[agent-hub](../../examples/agent-hub/README.md) serves MCP and A2A and its tools run **as the
caller**: `whoami` asks Databricks who owns the token that came with the call (`token_owner`) and
shows the identity headers beside it, and `databricks_me` calls Databricks with the caller's own
`X-Forwarded-Access-Token`. The two 2026-11-04 runs below were made before `whoami` asked Databricks;
they read the headers. [agent-console](../../examples/agent-console/README.md)
calls the hub over both protocols, once with the visitor's token (OBO: the console must be delegated
OBO to receive it) and once with its own identity (the shared service principal), and shows what the
hub saw. Together they answer: **can the caller's identity be passed through MCP and A2A?** The
protocols carry no identity of their own; it travels in the HTTP bearer, so the answer is the one
in [Who the target sees](#who-the-target-sees). Whether a scope-limited browser token is accepted as
the bearer of a call to another App is answered below: it is.

Measured on 2026-11-04 on the Free Edition dev App, with `hub` and `console` registered as children
of appmux, **OBO and the shared service principal delegated to both**, and the Databricks CLI's
bearer token as the person calling the console (a person, **no browser**):

| Call | Result |
|---|---|
| card, MCP `initialize`, A2A `SendMessage` (`call-tool`) | `200` |
| the console, called with the CLI token, sees the token | `user_token_forwarded: true`; its own identity is appmux's service principal (a UUID) |
| **me**, MCP and A2A: the console sends the forwarded token as the bearer to the hub | `200`. The hub saw the person in `X-Forwarded-Email`, `access_token_header_present: true`, and `databricks_me` returned the person's user name. All four calls ("me" x two tools x two protocols) matched the caller |
| `ask` over A2A as **me** | `TASK_STATE_COMPLETED`: the model (`databricks-gpt-oss-120b`, called with the hub's own identity) called `whoami` and `databricks_me`, and its answer named the person twice, from the headers and from Databricks |
| **this App**, MCP and A2A: the console's own identity as the bearer, **no grant on the hub** | `404` on the first call, all four. appmux hides the hub from an identity it has no grant for |
| before the hub was delegated | `whoami` showed the person but `access_token_header_present: false`, and `databricks_me` said no user token came |

So the answer to "can the caller's identity be passed through MCP and A2A" is **yes, in the HTTP
bearer**: a caller App that was delegated OBO receives the person's token in
`X-Forwarded-Access-Token`, sends it on as the bearer, and the called App sees the same person and
can act as them. It needs OBO delegated to **both** apps (the caller to receive the token, the
callee to receive it back) and a grant for the person on the callee. MCP and A2A behave the same,
because the protocols carry no identity of their own.

### The same check from a browser

Later on 2026-11-04 the owner opened the console in a browser and pressed **Run the identity check**
three times, with two Free Edition accounts: the first owns both apps, the second is another
account. OBO and the shared service principal were delegated to both apps. The browser's token is
the real OBO path: it reaches the console only with the scopes the appmux App declares.

| Who pressed it, and who could open what | Result |
|---|---|
| the owner of both apps; no grant on the hub yet | **me**, MCP and A2A: the hub saw the owner, held their token, and `databricks_me` returned them. **this App**: `404`, as in the CLI run |
| the second account; the console shared with it, **the hub not** | every cell `404`. appmux checks the hub's grants against the person behind the forwarded token, so being allowed into the console gives no access to the hub |
| the second account; **both apps shared**, and an `everyone` grant on the hub | **me**: the hub saw the second account, held its token, and `databricks_me` returned it |
| the same press, the **this App** column | the hub saw appmux's shared service principal (a UUID in the e-mail field), **a token was present**, and `databricks_me` returned that service principal |

What follows:

- **A browser token works as the bearer** of a call to another App, over both protocols, for the owner
  and for a second account.
- **Authorization follows the person.** The second and third rows differ only in whether the hub is
  shared, and the answer flips from `404` to success. The console is not a confused deputy: a person
  who may open it still reaches the hub only with their own grant.
- **A service principal's call carries a token too** when OBO is delegated to the callee. The earlier
  claim here that an SP's call never carries one was measured without OBO on the callee and was
  wrong in general. `databricks_me` then acts as the service principal.
- **The caller App is not identifiable.** The service principal is appmux's shared one, so the hub
  cannot tell `console` from any other delegated child.
- **`X-Forwarded-Preferred-Username` is not always sent, and the cause was not found.** The hub shows
  `anonymous` when the header is missing (its own placeholder), which is what the owner got. Measured
  again on 2026-11-04 with the CLI's OAuth bearer token as the owner: the child receives
  `X-Forwarded-Email` and `X-Forwarded-User` and no `X-Forwarded-Preferred-Username`, so the owner's
  missing header is not specific to the browser. appmux forwards the header exactly as the proxy gave it
  (`identity/actor.py`), so the proxy did not send it. The second account's call carried it. That
  leaves a difference between the two accounts (a property of the account, not of the kind of call) as
  the likely cause; it was not isolated, because the Free Edition workspace has only these two
  accounts. A child must not depend on that header; `X-Forwarded-Email` is the identity.

### The console as its own App

On 2026-11-04 the console was also deployed as a standalone App (`console-solo`, outside appmux,
`user_api_scopes` = `apps`, `PROVIDER_URL` set to the hub), and the hub's `whoami` was changed to
present the received token to the SCIM `Me` API and report the owner (`token_owner`) beside the
headers. The CLI's token as the caller of the standalone console:

| Call | Result |
|---|---|
| the console receives the visitor's token | yes (`user_api_scopes` is what makes the proxy forward it to a standalone App) |
| **me**, MCP and A2A | the hub's `whoami` named the token's owner as the caller (`via: token`), and `databricks_me` agreed. A token with the scope `apps` plus the two `iam` scopes is accepted as the bearer of a call to a child of appmux |
| **this App**, MCP and A2A | `401` from the Apps proxy: the standalone App's service principal has no `CAN USE` on the appmux App. No grant was made |

Not measured yet:

- **A `service_principal` grant** ([ADR 0020](../decisions/0020-let-a-service-principal-hold-an-app-grant.md))
  in place of `everyone` on the hub. The run above used `everyone`, which admits every identity the
  proxy accepts. The grant itself was measured separately, with a throwaway SP, on the same day
  (ADR 0020, "Measured").
- Several people at once: only two accounts exist on Free Edition.

## Not measured

- The 300 s cut of the Apps proxy for a call that lasts that long (measured in spike 8 for a plain
  stream, not for MCP or A2A), and whether a stream that sends nothing for a while survives.
- A `group` grant admitting an SP, and the identity of an SP in appmux's audit and metrics.
- A `service_principal` grant (ADR 0020) held by an *App's* service principal: it was measured
  with a throwaway SP that is not an App, which the proxy treats alike but which is not the same
  object.
- Load: the cost of the extra hop through appmux, and what the free model's limit does to several
  agents sharing the workspace.
- Streamable HTTP with a session id or a server-sent stream from the MCP server (the example is
  stateless and answers with one JSON body), and A2A push notifications.
- Any workspace other than the Free Edition one, and Databricks' own managed MCP servers.
