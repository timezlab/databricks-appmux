# Route by path prefix behind a RouteSelector

**Status:** accepted
**Date:** 2026-10-30
**Deciders:** Liam Lee (design approved 2026-10-30)

## Context

One hostname and one port reach appmux, so a request can name its app only
through the path, a cookie or a header. On 2026-10-29 the user chose three
modes (`prefix`, `prefix-rescue`, `root`); on 2026-10-30 they narrowed v1 to the
path prefix alone, with the code keeping room for the others. The Databricks
docs say nothing about base paths, WebSocket, timeouts or size limits, and the
proxy connects from loopback, so appmux sees only what the proxy forwards.

## Decision

v1 routes by path prefix only: `/<slug>/x` goes to the app `slug` as `/x`, an
unmatched path gets `404`, and `/_appmux/` (launcher, admin UI, API) uses the
reserved slug `_appmux`. Routing sits behind a `RouteSelector` interface
(`select(request, registry_snapshot) → Selection | NoMatch` and
`rewrite_response`), registered in `SELECTORS`; selectors are pure. The edge
pipeline runs in a fixed order: `identity → routing → authz → rewrite(request) →
upstream → rewrite(response)`. appmux sets the base path for the child
(`APPMUX_BASE_PATH`, the `X-Forwarded-Prefix` header and a framework's own
variable where one exists) and rewrites only headers, `Location` and
`Set-Cookie` (`Path` forced to `/<slug>/`, `Domain` dropped). **Response bodies
are never rewritten** ([design §7](../product/v1-design.md#7-request-path-and-routing)).

## Alternatives considered

- **Host-based routing** — rejected: one hostname per Databricks App.
- **Root `/` routing by cookie as the only mode** — rejected: one app per
  browser and tabs conflict; kept as the deferred `root` selector.
- **Port or query-parameter selection** — rejected: one port reaches the app,
  and a query parameter is lost at the first link or redirect.
- **Rewrite response bodies (nginx `sub_filter`)** — rejected: it misses URLs
  built in JavaScript or JSON, breaks streaming and buffers every response in
  the shared edge.
- **`prefix-rescue` (route a stray path by `Referer` or cookie)** — deferred;
  would revisit if spike 5 shows too few frameworks honour a base path.

## Consequences

**Better:**
- Identity, routing, authorization, rewriting and upstream are separate
  modules, so a second selector is a new module and not a change to the edge.
- `routing` and `authz` are pure and tested as tables, including path-escape
  fixtures (`/sales/../hr`, `//hr`, encoded dots).

**Worse:**
- An app that hard-codes absolute links (`<script src="/static/app.js">`) is not
  hostable in v1: its page loads and its assets get `404`.
- All children and the admin UI share one browser origin, so a hostile child's
  script in an admin's browser can reach the admin API; v1 hardens the UI
  (`frame-ancestors 'none'`, `Cross-Origin-Opener-Policy: same-origin`, no
  CORS, `Sec-Fetch-Site` checks, strict CSP) and accepts the rest.
- Which frameworks can run unchanged depends on spike 5.

**Must now be true:**
- Selectors do no I/O and check no permissions; they read only the in-memory
  registry snapshot passed in.
- No module rewrites a response body.
- `_appmux` is a reserved slug that no registration or rename can take.
- The edge removes client-supplied identity headers before forwarding.

## Revisit if

Spike 5 shows that the target frameworks mostly cannot run under a prefix (add
`prefix-rescue`); users must host apps with hard-coded absolute links; or the
Databricks proxy starts offering a hostname per app.

## Spike results

**Spike 8, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-8--behaviour-of-the-databricks-proxy-2026-10-30)).**
The decision holds. The proxy forwards sub-paths with path and query intact and
strips no prefix, carries WebSocket and SSE, and overwrites a client-supplied
`X-Forwarded-Email`. It resolves `..` and `%2e%2e` segments and answers `//`
with a 301 before the app sees the request, which does not remove the need for
the edge to normalise and reject ambiguous paths itself. Two platform limits
apply to every child under a prefix: 300 s per request or stream, and 32 KB for
any single header. `Location` is passed through unchanged, so the rewrite in the
Decision is appmux's job. The "Revisit if" conditions are unchanged; spike 5
still decides the hostable framework list.

**Spike 5, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-5--base-path-per-framework-2026-10-30)).**
The decision holds, and the framework list is now measured (local run, prefix
stripped, `X-Forwarded-Prefix` added). Hostable in v1: FastAPI with
`UVICORN_ROOT_PATH=/<slug>`; Streamlit with **no** setting; Dash with
`DASH_REQUESTS_PATHNAME_PREFIX=/<slug>/` and `DASH_ROUTES_PATHNAME_PREFIX=/`;
Gradio with `GRADIO_ROOT_PATH=/<slug>`; Node/Express when its links are
relative. Not hostable, whatever the framework: an app that writes absolute
links such as `<script src="/static/app.js">`. No framework failed, so the
"Revisit if" trigger for `prefix-rescue` did not fire.

*Consequences (added).* The "Which frameworks can run unchanged depends on spike
5" item under **Worse** is answered by the list above. The Decision's "a
framework's own variable where one exists" becomes: appmux sets the FastAPI,
Gradio and Dash variables for every child and none for Streamlit, because
`STREAMLIT_SERVER_BASE_URL_PATH` breaks Streamlit when the prefix is stripped.
Not run: a FastAPI app that starts uvicorn from code. In uvicorn's source only
the command-line entry point reads `UVICORN_*`, so such an app would ignore
`UVICORN_ROOT_PATH`; the plan decides how the supervisor handles that form.
