# node-stream

A Node app on Node's own `http` module, with **no dependency**: one HTML page, one script, a JSON
endpoint and a Server-Sent Events stream. It is the example for the Node runtime and for a response
that stays open. The general walkthrough (upload, register, Update, roll back, config, share, and
what an end user checks) is in [../README.md](../README.md#admin-walkthrough-in-order); this file
holds what is specific to this app.

## What it shows

- **Identity**: the page and `GET /api/whoami` report `X-Forwarded-Email`, `X-Forwarded-User`,
  `X-Forwarded-Preferred-Username` and `X-Request-Id` as they arrive, and whether
  `X-Forwarded-Access-Token` is present (never its value). Without a header the caller is
  `anonymous`.
- **Release and config**: `VERSION` in `server.js` (`1.0.0`) and the `APP_GREETING` variable
  (`app.yaml` sets `Hello from node-stream`; the release config can override it).
- **A response that stays open**: the **Start a 15 second stream** button opens `events`, which sends
  one Server-Sent Event per second for 15 seconds and then `event: done`. The other apps answer and
  finish; this one holds the connection. Streams started since the process began are counted in
  memory, so an Update, an Activate, a config change or a Restart resets the count.
- **Relative URLs under a prefix**: the page loads `app.js` and opens `events` without a leading
  `/`, so both resolve under `/<slug>/`. appmux never rewrites a response body, so a link like
  `/app.js` would leave the app.
- **The prefix**: `X-Forwarded-Prefix` and `APPMUX_BASE_PATH` are shown.
- **Health**: `GET /` and `GET /healthz` answer 200.

## Files

| File | Purpose |
|---|---|
| `server.js` | the whole app: CommonJS, `node:http` only |
| `package.json` | makes it a Node app; `npm run start` runs `node server.js`; no dependencies |
| `app.yaml` | `command: ["node", "server.js"]` and `APP_GREETING` |

There is **no `requirements.txt` on purpose**: appmux detects the runtime from the files, and a
`requirements.txt` makes it a Python app. There is no `package-lock.json` or `node_modules` because
there is nothing to install. appmux v1 has no `npm install` step (the offline npm cache is deferred),
so a Node app that needs packages is not an example this release can host; shipping `node_modules`
inside the snapshot is untested ([research D8](../../specs/001-appmux-v1/research.md)). For the same
reason this is not Express: that would be a claim nobody has run.

`DATABRICKS_APP_PORT` is the port the app listens on (a standalone App and appmux both set it, so
`app.yaml` names none); the app refuses to start without it. `NODE_APP_HOST` overrides the bind
address and defaults to `0.0.0.0`.

## Memory budget: 768 MB, not the usual 128 MB

appmux limits a child's **address space** to 2 x its budget (`RLIMIT_AS`, see
[design 5.7](../../docs/product/v1-design.md)), and V8 reserves a large range at start. Measured
on this workstation (Node v24.12.0): the app uses about 50 MB resident and 1.4 GB virtual at peak,
and **does not start under 1,100 MB of address space**: it dies with `Fatal process out of memory:
Failed to reserve virtual memory for CodeRange`. 1,120 MB started. So a budget of 512 MB (a limit
of 1,024 MB) fails, and the smallest budget that works is about 560 MB. The suggested 768 MB (a limit
of 1,536 MB) is what every check below ran under. The cost is admission control: this app reserves
768 MB of the container's budget while using a fraction of it.

## Run it locally

Node 22 or later, nothing to install:

```sh
cd examples/node-stream
DATABRICKS_APP_PORT=8000 APPMUX_BASE_PATH=/demo APP_GREETING="Hello from my laptop" node server.js
```

`APPMUX_BASE_PATH` and `APP_GREETING` copy what appmux sets; leave them out to see it as a standalone
app. In another terminal, with and without the headers appmux would forward (the token is a fake
value):

```sh
curl -s localhost:8000/api/whoami
curl -s -H 'X-Forwarded-Email: dana@example.com' -H 'X-Forwarded-Preferred-Username: dana@example.com' \
  -H 'X-Forwarded-Prefix: /demo' -H 'X-Forwarded-Access-Token: placeholder' localhost:8000/api/whoami
curl -s -N localhost:8000/events
```

The last command prints a tick a second for 15 seconds, then `event: done`. Open
`http://localhost:8000/` in a browser to use the button. Stop it with `Ctrl-C`.

## Upload and register

Placeholders as in [../README.md](../README.md#before-you-start):

```sh
databricks workspace import-dir examples/node-stream \
  /Workspace/Users/<your-email>/appmux-examples/node-stream \
  --profile <your-profile> --overwrite
```

Then **Platform** > **Register an app**: Slug `stream`, Display name `Stream (Node)`, Source folder
`/Workspace/Users/<your-email>/appmux-examples/node-stream`, Memory budget (MB) `768`, Owner
`<your-email>`; then open the app and press **Update**.

## As an admin, try

1. **Update** with the folder unchanged: "already up to date", no new release.
2. Set `VERSION = '2.0.0'`, `import-dir` again and **Update**. Reload: the version moves and the
   stream count is back to 0 (a new process).
3. **Activate** release 1: version `1.0.0` again.
4. Override `APP_GREETING` with the config call in the
   [main walkthrough](../README.md#admin-walkthrough-in-order) (step 7): same version, new greeting,
   a new release number.
5. Start the stream and press **Update** (or **Restart**) while it runs. The design says the old
   release drains and the new one takes over. What happens to a stream that is open at that moment
   is **not observed**: look at it and write down what you see.
6. Register the app with a Memory budget of `512` and press **Update**. The release should fail to
   start; the reason shown for it is the thing to read. (Not run in the container.) Then raise the
   budget to `768` as a platform admin and Update again.

## As an end user, check that

- `/<slug>/` loads and **You are** shows your email.
- **Start a 15 second stream** prints 15 ticks, then `stream finished`, and the button works again.
  Reload halfway and start again: `streams_started` in `api/whoami` (and on the page after a
  reload) goes up by one for each stream started.
- The links **api/whoami**, **events** and **healthz** work and stay under `/<slug>/`.
- `api/whoami` shows `x_forwarded_prefix: "/<slug>"` and `appmux_base_path: "/<slug>"`.
- The access-token header reads **absent**, unless the admin delegated OBO and opened the app **as a
  user** ([../README.md](../README.md#obo-optional)).
- Open the raw `events` URL and leave it: whether the edge ends a long-lived response early (its
  limits are 120 s per request, 100 MB and 64 at once) is **not observed**. The stream ends by itself
  after 15 seconds, well inside the time limit.
