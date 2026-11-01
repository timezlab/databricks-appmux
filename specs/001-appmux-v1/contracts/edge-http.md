# Contract: edge HTTP

What a browser, a service client or a child sees at the one hostname the Databricks proxy forwards to appmux. Behaviour comes from [design 7](../../../docs/product/v1-design.md#7-request-path-and-routing) and [design 11](../../../docs/product/v1-design.md#11-failure-behaviour); this file fixes the surface the tests assert against. The admin JSON API is in [admin-api.md](admin-api.md).

## Paths

| Path | Goes to | Notes |
|---|---|---|
| `/_appmux/…` | admin UI (static export) and `/_appmux/api/v1/…` | reserved; `_appmux` is not a valid slug |
| `/` | the launcher | same page as `/_appmux/` |
| `/<slug>/…` | the child `slug`, as `/…` | a registered slug with an `active` release and a passed health check; the upstream query string is kept |
| anything else | `404` | no slug matches |

Before matching, the routing function normalises the path (percent-decoding once, dot segments resolved, repeated slashes collapsed) and rejects ambiguous input (encoded slashes, backslashes, NUL, a path that changes after one more decode) with `400`. A match is on the whole first segment: `/sales-old/x` never reaches `sales`.

## Request headers the edge reads (from the proxy)

`X-Forwarded-Email`, `X-Forwarded-User`, `X-Forwarded-Preferred-Username`, `X-Real-Ip` (the client address: the proxy overwrites it, spike 8), `X-Request-Id`, `X-Forwarded-For` (not used for the client address: the proxy appends its own hops after the client-supplied value), and, when user authorization is on, `X-Forwarded-Access-Token`. A request with no `X-Forwarded-Email` is refused `401` (the proxy always sets it; its absence means the edge was reached another way).

## Request headers a child receives

| Header | Value |
|---|---|
| `X-Forwarded-Email`, `X-Forwarded-User`, `X-Forwarded-Preferred-Username`, `X-Real-Ip`, `X-Request-Id` | the proxy's values; any client-supplied copy is removed first |
| `X-Forwarded-Prefix` | `/<slug>` |
| `X-Forwarded-Access-Token` | present only if the app is OBO-delegated and the visitor is its owner, is named by a grant, or is a platform admin who chose **Open as a user**; otherwise removed |

Not forwarded: the hop-by-hop headers (`Connection`, `Keep-Alive`, `Proxy-Authenticate`, `Proxy-Authorization`, `Proxy-Connection`, `TE`, `Trailer`, `Transfer-Encoding`, `Upgrade`), any header a `Connection` line names, and the `Sec-WebSocket-*` handshake lines (the relay opens its own handshake to the child). `Host` passes through unchanged. Every other header sent by the client, `X-Forwarded-For`, `X-Forwarded-Host`, `X-Appmux-*` and `Authorization` among them, passes through unchanged except those named above. The child never sees appmux's own environment.

The edge refuses with `400`, and forwards nothing, a request with a header name that is not an HTTP token, a value holding CR, LF or NUL, more than one `Host` line, or both `Content-Length` and `Transfer-Encoding` (two body framings). The error text names a line's position or name, never a value.

## Response handling

- `Location` beginning with `/` gets the prefix (`/login` becomes `/sales/login`).
- `Set-Cookie`: `Path` forced to `/<slug>/`, `Domain` dropped, other attributes kept.
- Response bodies are never rewritten. Streaming responses and WebSocket upgrades pass through unbuffered.

## Status codes the edge itself produces

| Status | When | Body |
|---|---|---|
| `200` | `GET` or `HEAD` on `/` while the process is starting (port bound, registry or `self_check()` not finished) | static "starting" page, no identity or registry data |
| `400` | ambiguous path, or a request header that cannot be forwarded safely | short text, no path or header value echoed |
| `401` | no forwarded identity | short text |
| `404` | no slug matches, **or** the actor lacks `CAN_USE` on the app (the same answer, so slugs cannot be enumerated; the denial is logged server-side) | short text |
| `413` | request body over `max_request_body_mb` | short text |
| `429` | more than `max_concurrent_requests` in flight for the app | `Retry-After: 1` |
| `502` | the child is unreachable or closed the connection | status page |
| `503` | the app is `crashed`, stopped or not yet `active`; or `self_check()` failed or the registry is unreachable at start (every route, `/` included); or any path but `/` while the process is starting | status page naming the app's state, the failed check or the reason |
| `504` | no response within `request_timeout_s` | status page |

Any other status is the child's own. An error body never carries a stack trace, a path or a registry message.

## Admin UI responses

Every response under `/_appmux/` carries `Content-Security-Policy` (`script-src 'self'` plus the SHA-256 of each page's inline scripts), `frame-ancestors 'none'`, `Cross-Origin-Opener-Policy: same-origin`, and no CORS headers. A state-changing request without `Sec-Fetch-Site: same-origin` is refused `403`.

## Open as a user

For an OBO-delegated app on which a platform admin has no grant, `POST /_appmux/api/v1/apps/<slug>/open-mode` sets an edge cookie (`__Host-appmux_open`, `HttpOnly`, `Secure`, `SameSite=Lax`) naming the choice per app. With no cookie the default is *without token*. The cookie is set only by the edge; a same-named `Set-Cookie` from a child is dropped.

## Not in this contract

H2C (the proxy used HTTP/1.1 in every test), a root or header selector, and any rewrite of response bodies.
