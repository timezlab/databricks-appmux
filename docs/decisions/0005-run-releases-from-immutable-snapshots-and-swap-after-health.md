# Run releases from immutable snapshots and swap routes after health

**Status:** accepted
**Date:** 2026-10-30
**Deciders:** Liam Lee (design approved 2026-10-30)

## Context

Editing a source folder can break a running app (the user's observation of a
Databricks deployment in `AUTO_SYNC` mode), and the constitution requires that a
route opens only after fetch → build → start → health, and that adding or
updating a child never interrupts running apps (principles II and III). Children
inside appmux are not Databricks deployments, so appmux must snapshot them
itself. Reading the source must not need a grant to a service principal.

## Decision

A **snapshot** is an immutable, content-addressed copy of a child's source,
taken only when an app admin presses Update, read with **that person's own OBO
token**. A **release** is a snapshot plus config and moves
`pending → building → starting → healthy → active`, or `failed` or
`superseded`. Update starts the new release next to the running one on a private
route and polls `health_path` (any status below 500) until
`health_deadline_s`; on success the route table swaps atomically and the old
release drains for 30 s, on failure the release is `failed` and the running
release is untouched. A config change creates a new release with the same
snapshot. Rollback is activating **any** live release from its stored snapshot,
wheels and config, with no source read and no token. A crash restarts the same
release with backoff and then marks only that app `crashed` (503 for its route);
there is no automatic fallback to an older release. Admission control refuses an
Update or start when the budget does not fit next to the running copy
([design §5](../product/v1-design.md#5-child-lifecycle)).

The platform setting `max_releases_per_app` (default 10) caps live releases and
`max_locked_releases` (default 3, at most the cap minus 4) caps manual locks. A
release is **protected** while it is active, the target, in progress or locked;
when an Update would exceed the cap, the oldest unprotected release is deleted,
or the Update is refused if none qualifies. Tags and names protect nothing.
Resources, secrets and own-SP keys belong to the app, not a release.

## Alternatives considered

- **Follow the source folder live** — rejected: this is the failure the user
  observed.
- **Stop-then-start updates** — rejected: the app is down during fetch and
  build, and stays down if the new release is bad.
- **Read the source with the appmux or the child's SP** — rejected: the folder
  owner must grant the SP per app, and appmux would read with rights the
  presser may not have (a confused deputy).
- **Tags that protect a release, or a fixed retention number** — rejected: an
  explicit, capped lock and a platform-set cap are easier to reason about.
- **Automatic rollback** — deferred: a crashed app answers 503 until an admin
  activates a release; revisit with real usage.

## Consequences

**Better:**
- An update causes no downtime, and a bad release never replaces a good one.
- Rollback to any version needs no source access, so it works when the source
  folder is gone or the admin lost read access.

**Worse:**
- An update needs the app's memory budget twice for up to the health deadline;
  admission control refuses it when the slot cannot hold both.
- An Update whose token expires mid-snapshot fails and must be repeated.
- Snapshots and wheels need durable storage; the content store location is
  unverified (spike 4).
- Secrets and resources are not versioned per release, so a rollback runs with
  current values.

**Must now be true:**
- Only the `reconciler` opens or closes a route, and only after a release
  reaches `healthy` (constitution II).
- Release numbers are never reused; deletion drops the snapshot link and keeps
  the `releases` row as history.
- A snapshot's digest is verified at start (`snapshot sha256 mismatch` fails the
  release).
- The active, target, in-progress and manually locked releases cannot be
  deleted; no code path deletes a protected release.
- The appmux SP holds no right on a child's source folder.

## Revisit if

Admins ask for automatic rollback or canary traffic; the doubled budget keeps
blocking updates of large apps; or the Update token's lifetime keeps failing
large snapshots.

## Spike results

**Spike 4, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-4--content-store-location-2026-10-30)).**
The decision holds, and the "content store location is unverified" item under
**Worse** is answered (Free Edition only). The appmux SP creates its own folder
under `/Workspace/Users/<sp client id>/` with no grant and no workspace admin,
and writes, reads back (digest equal) and deletes files there: 10, 100 and 500 MB
in under 7 s each way. **501 MB is refused** (the connection is closed, no HTTP
status), so the folder takes at most 500 MB per file. A Unity Catalog volume the
SP creates itself in `workspace.default` took 1000 MB and is the fallback for
larger files.

*Consequences (added).*
- A file over 500 MB cannot go in the folder; the plan decides between refusing it
  at Update with the file named and sending it to the volume. Not checked: how
  large the machine-learning wheels that apps will bring are.
- The cost of a snapshot is its file count. Per-file calls ran at about 9 files/s
  sequentially on the folder route, which answers parallel calls with HTTP 429
  after about 20 in a burst; the volume route ran at about 3.5 files/s
  sequentially and 22 to 32 files/s at 8 threads, with no 429. The plan picks one
  layout: a release's small files packed into one archive named by its digest, or
  per-file objects on the volume fetched in parallel, or per-file objects in the
  folder written one at a time with back-off.
- The folder's ACL gives `CAN_MANAGE` to the SP and to the `admins` group, so any
  workspace admin can read every snapshot and wheel. The "Must now be true"
  list is unchanged.

**SC-001 and a failed release, 2026-11-05 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#sc-001-no-disruption-under-operations-t108)).**
The first "Must now be true" item was measured in the Free Edition dev App's container with app A
under 500 requests a second: updating A with a good release (the swap), and with a release that
fails its health check, caused no failed request (0 in 30 000 and 0 in 40 000, the control also 0).
The failed release stayed `failed` and A kept serving on its active release. Free Edition only,
one container, one workload; "Revisit if" is not triggered. The consequences and the list above are
unchanged.
