# Resolve packages environment-first, with a wheel overlay from a content store

**Status:** accepted — spikes 18, 13 and 14 passed on 2026-10-30 and none
reversed it; each added consequences under *Spike results*.
**Date:** 2026-10-30
**Deciders:** Liam Lee (design approved 2026-10-30)

## Context

The company package index is blocked from the workspace, so packages must come
from wheel files in workspace folders, for appmux itself and for every child.
Children run on the parent's Python 3.11, so the packages already installed in
the appmux App's own environment can be reused, and a child must not be able to
modify that environment. A virtualenv per release was tried locally: a
`--system-site-packages` venv created inside a venv does not see the parent's
packages, and one venv per release costs time and space.

## Decision

For each requirement of a child, in this order: (1) the **app environment**
satisfies it (read with `importlib.metadata`) and is used as is, with nothing
fetched; (2) otherwise a **wheel** is installed into the release's overlay and
shadows the environment copy; (3) otherwise the **package index** is used in
`index` and `wheelhouse+index` mode, or the Update fails listing every
unresolved requirement at once in `wheelhouse` mode. The package mode is a
platform setting.

Wheels are declared per child: the app's own `wheels/` folder, the release
config's `wheel_sources`, then the platform's optional `default_wheel_sources`,
all read with the Update presser's token. In the wheel modes a requirement the
environment cannot satisfy must be pinned `==` with a hash; appmux looks in the
**content store** by sha256 first and fetches only what is missing, verifying
the hash. The store holds one file per distinct sha256, and each release records
its **wheel list**. The overlay is built with `pip install --no-index --no-deps
--require-hashes --find-links W --target O`, mounted read-only, and put ahead of
the environment on `PYTHONPATH` and `PATH`; the cache key is the digest of the
wheel list plus the Python minor version. At every start appmux re-checks the
requirements the environment was satisfying and fails the release before it
starts if one no longer holds ([design §5.3](../product/v1-design.md#53-build-and-package-sources)).

## Alternatives considered

- **Install from the package index only** — rejected: the index is blocked.
- **A virtualenv per release** — rejected for the reasons in Context.
- **One central wheelhouse that every app must use** — rejected: every app waits
  on whoever can write it, and every file in it is offered to every app.
- **A private wheelhouse per child with no sharing** — rejected: a heavy wheel
  such as `pandas` would be stored and fetched once per app.
- **Build source distributions in the wheel modes** — rejected: it is the
  largest install-time code-execution path; wheel modes use `--only-binary`.

## Consequences

**Better:**
- Most requirements cost nothing (`41 of 58 from the environment`), and a wheel
  used by thirty apps is stored once.
- A wheel can shadow a package only for its own release, and a bad wheel in one
  app's source is never installed by another.

**Worse:**
- A release is reproducible only while the wheels are in the store and the
  environment still satisfies the rest; a redeploy of appmux can change the
  environment and fail a release at start.
- App admins prepare hashed wheel locks and platform-specific wheels
  (`manylinux2014_x86_64`, Python 3.11) elsewhere; a package with no wheel
  blocks the Update.
- appmux's own requirements become every child's base layer, so they must stay
  lean; Node children have no offline source yet (spike 14).
- Mixed overlay and environment versions can conflict; `pip check` only warns.

**Must now be true:**
- The environment is never modified: it and every overlay are mounted read-only
  into children, and `self_check()` proves it.
- In `wheelhouse` mode the build runs with an all-deny egress list and installs
  with `--no-index`.
- Content-store files are named by sha256, and every wheel installed is in the
  release's wheel list; `default_wheel_sources` is writable by platform admins
  only.
- The `packages` module starts no process outside the sandbox launcher.

## Revisit if

Spike 18 shows the overlay does not take precedence or entry-point scripts fail
from the overlay's `bin`; spike 13 or 14 shows appmux's own or Node
dependencies cannot be installed offline; or the index becomes reachable (then
`index` mode makes most of this optional).

## Spike results

**Spike 18, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-18--package-overlay-on-the-real-container-2026-10-30)).**
Status stays `proposed`: spikes 13 and 14 are still to record. Nothing
reversed the decision. On the Free Edition container, in the spike 2 sandbox,
`pip install --no-index --no-deps --require-hashes --find-links W --target O`
with the environment's own Python 3.11 and pip 24.0 installed eight hashed
wheels offline in about a second; a wrong hash, a missing hash and an unpinned
requirement each stopped it with exit code 1 and installed nothing. With
`PYTHONPATH=O` the overlay's `fastapi`, `starlette`, `uvicorn` and `click`
(other versions than the environment's) and a package the environment lacks
were the ones imported; `bin/uvicorn` ran from the overlay (its shebang is the
environment's interpreter); and every write to the environment or the overlay
returned `EROFS`.

*Consequences (added).* Two things the Decision does not say. First, the
overlay's `bin` script does not select the overlay: started without
`PYTHONPATH` it imported the environment's packages, so `PYTHONPATH` is part of
the contract. Second, the **Worse** item "Mixed overlay and environment
versions can conflict; `pip check` only warns" is now measured: overriding
`opentelemetry-api` broke the environment's `opentelemetry-sdk` and
`opentelemetry-semantic-conventions` (each pins it with `==`), and the app
still served. `pip check` named every gap and the lock converged in three
runs, so the wheel lock must cover the closure of what it overrides. Whether
such a conflict should fail the Update rather than warn is for the plan to
decide.

**Spike 13, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-13--parent-app-wheel-bootstrap-2026-10-30)).**
Status stays `proposed`: spike 14 is still to record. The Free Edition platform
honours `--no-index` and `--find-links ./wheels` in an App's `requirements.txt`
(plain `pip` 24.0 runs `-r requirements.txt`; the packages land in the app's
environment), so appmux's own dependencies can be vendored in the deployment
folder with no bootstrap. A standard-library bootstrap that downloads the wheels
from a workspace folder to `/tmp` also worked, once the folder's owner granted
the app's service principal `CAN_READ`; it stays the fallback. Not tested: a
workspace whose index is unreachable. The "Revisit if" condition about appmux's
own dependencies did not fire.

*Consequences (added).* The App environment persists across deployments of the
same app, so a package removed from `requirements.txt` stays installed. If the
bootstrap ever ships, its packages live in `/tmp`, outside the environment, and
the base layer of a child's packages needs that directory as one more read-only
layer.

**Spike 14, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-14--node-offline-source-and-wheel-coverage-2026-10-30)).**
Status moves to `accepted`: spikes 18, 13 and 14 have passed and none reversed
the decision. A Node child can be installed with no index: on the Free Edition
container, in a build sandbox with no network, `npm ci --offline
--ignore-scripts` from a **read-only** npm cache and a lock file installed 66
packages (Express 5.3.0 and `ws`) in under a second; an empty cache, or a lock
with an altered integrity, stopped with `ENOTCACHED` and installed nothing; and
the installed tree ran read-only (HTTP, WebSocket, five refused writes). For
Python, four fixtures were measured against the 132-package environment. A
declaration with no pin (`fastapi`, `streamlit`, `dash`, `gradio`) is fully
covered by the environment. Pinning the top-level package to the newest version
needs 5, 2, 3 and 16 wheels (0.6 to 38.6 MB), and pinning the whole closure
needs 14, 26, 22 and 41. Every one of those wheels exists for Python 3.11 and
`manylinux2014_x86_64`; no package lacked one.

*Consequences (added).* The **Worse** item "Node children have no offline source
yet (spike 14)" is answered: there is one, and the plan decides how the cache
reaches the builder and whether v1 ships it. The "Revisit if" condition about
Node dependencies did not fire. Pinning a top-level package to its newest
version replaced environment packages in three of four fixtures (`starlette`,
`opentelemetry-api`, `websockets`) and left the environment's own
reverse-dependents (`fastapi`, `opentelemetry-sdk`, `gradio-client`) failing
`pip check`, so the spike 18 finding holds beyond one fixture: the wheel lock
covers the closure of everything it overrides, and the plan decides whether
such a conflict fails the Update. Not measured: packages beyond these five
fixtures, native Node addons and install scripts, and delivery of the cache
through a workspace folder.
