# Follow the standalone app.yaml contract and read databricks.yml only as an offer

**Status:** accepted
**Date:** 2026-10-30
**Deciders:** Liam Lee (design approved 2026-10-30)

## Context

Constitution principle I says a child stays a standard Databricks App with its
own `app.yaml`. The user asked for the same logic as a standalone deployment:
appmux reads `command`, `env` and the resource keys the child needs, and sets
the environment a standalone App would get. A standalone App is often deployed
from a Databricks Asset Bundle, whose `databricks.yml` declares its resources,
and applying that file blindly would attach resources with no check of a
person's rights.

## Decision

appmux defines no appmux-specific file in a child. At Update it parses the
snapshot's `app.yaml` (or `app.yml`) and keeps only `command` and `env`; both
files present, or an `env` entry with both or neither of `value` and
`valueFrom`, refuses the Update; any other key warns. The runtime comes from the
files (`requirements.txt` or `pyproject.toml` with `uv.lock` means Python,
`package.json` means Node), so there is no `app_type`. The command runs as an
argument list without a shell, with `$NAME` and `${NAME}` replaced from the
child's environment until spike 17 shows what Databricks does.

The release's **requirements** are every `valueFrom` key in the manifest plus
every `value_from` the app admin added to the release `env`. They are checked
first, before any build; a key that is not a resource of the app makes the
release `failed`, listing all missing keys at once, and Activate starts it after
they are attached. The child receives, later wins: the `app.yaml` values (with
`valueFrom` resolved from the app's resources), the release's own `env`, and
appmux's variables, which cannot be overridden (`DATABRICKS_APP_PORT`,
`DATABRICKS_APP_NAME`, `DATABRICKS_HOST`, `APPMUX_BASE_PATH`, the framework port
variables, credentials for the app's mode).

If the snapshot has a `databricks.yml`, appmux reads only
`resources.apps.<key>.resources[]` and stores each entry as an **offer**.
Nothing is attached until the app admin confirms it ([design §5.4](../product/v1-design.md#54-appyaml-requirements-and-start)).

## Alternatives considered

- **An appmux manifest in the child** — rejected: it breaks principle I.
- **Make the admin re-declare env and resources in appmux** — rejected: it
  duplicates what the child already says and drifts from it.
- **Ignore `databricks.yml`** — rejected: it loses what the author wrote.
- **Apply `databricks.yml` as the source of truth** — rejected: a file must never
  widen an app's reach; resources would attach with no permission check.
- **Declare an `app_type` at registration** — rejected: it can disagree with the
  files and does no work that detection cannot.

## Consequences

**Better:**
- A child needs no change to join appmux and can still be deployed on its own.
- A missing resource fails the release before a build, with every missing key
  listed, instead of failing at run time one key at a time.

**Worse:**
- appmux runs on Python 3.11 and Node 22 only and sets no `OTEL_*`
  telemetry; a standalone App using another Python version via `uv` is not
  supported.
- What the platform does for a `valueFrom` key with no resource, whether it
  substitutes `$DATABRICKS_APP_PORT` in a command argument, and the full variable
  list are unverified (spike 17); appmux substitutes itself meanwhile.
- A bundle with `${…}` interpolations shows those values unresolved and the app
  admin types them.

**Must now be true:**
- The `appyaml` parser keeps only `command` and `env`, never starts a process,
  and never runs the command through a shell.
- Reading `databricks.yml` writes no `app_resources` row; only an app admin's
  Attach does.
- The requirements check runs before the build in the reconciler, and the
  appmux variables are applied last.

## Revisit if

Spike 17 shows Databricks substitutes variables or resolves missing keys
differently from the assumption; or a standalone App gains a runtime feature
(another Python version, telemetry) that users need in children.

## Spike results

**Spike 17, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-17--appyaml-on-the-real-platform-2026-10-30)).**
Databricks replaces `$DATABRICKS_APP_PORT` inside a `command` argument; it does
**not** replace `${DATABRICKS_APP_PORT}`, and it replaces neither form inside an
`env` `value`. A `valueFrom` key with no resource logs a build error but the
deployment still starts, with the variable absent. This settles the "until spike
17" clause of the Decision: appmux substitutes `$NAME` only, per `command`
argument, never inside `env` values and never through a shell. The variable list
of a real App is recorded in the spike log and in
[design §5.4](../product/v1-design.md#54-appyaml-requirements-and-start). The
release-fails-before-start rule is a deliberate difference from a standalone
deploy, which only logs the error.

**Must now be true (added):**
- Command substitution replaces `$NAME` only; `${NAME}` and `env` values are
  passed as written.
