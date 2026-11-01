# License appmux under Apache-2.0

**Status:** accepted (the "never bundled" sentence of the Decision is read with [ADR 0015](0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md), accepted 2026-11-01: it lets the release zip, not the repository, carry the binary)
**Date:** 2026-10-30
**Deciders:** Liam Lee (chose "whatever fits"; Apache-2.0 proposed 2026-10-30)

## Context

appmux is an open-source project that runs other people's code inside a shared
Databricks App. The product design listed the licence as an open question. The
user left the choice to what fits. appmux needs `slirp4netns` at run time, which
is believed to be licensed GPL-2.0.

## Decision

appmux is licensed under **Apache-2.0**. The `LICENSE` file at the repository
root holds the unmodified Apache-2.0 text. `slirp4netns` is a separate program
that appmux starts as its own process; it is fetched at install time with a
checksum and is never committed to this repository or bundled into its files.

## Alternatives considered

- **MIT** — rejected: no express patent grant, which matters for a tool that
  other teams will run and extend.
- **GPL-3.0 or AGPL-3.0** — rejected: copyleft would restrict teams that embed
  or modify appmux inside company deployments, which is the intended use.
- **Defer the choice until release** — rejected: contributors need a licence
  from the first commit.

## Consequences

**Better:**
- A patent grant and a permissive licence that companies can adopt without
  review friction.

**Worse:**
- Apache-2.0 requires preserving the licence and notices in redistributions, and
  does not stop a competitor from using appmux in a closed product.
- Starting `slirp4netns` as a separate process is believed to keep the licences
  apart, but that reading has not been checked against the binary's actual
  licence (spike 9).

**Must now be true:**
- The repository root has a `LICENSE` file containing the Apache-2.0 text.
- No `slirp4netns` binary or source is committed; the install step fetches it
  and verifies its checksum.
- New third-party code added to the repository carries a licence compatible with
  Apache-2.0.

## Revisit if

Spike 9 shows the `slirp4netns` licence cannot be kept at arm's length, or the
project owner decides on a different distribution model (for example a hosted
service).

## Spike results

**Spike 9, 2026-10-30 (pass; [log](../../specs/001-appmux-v1/spikes/README.md#spike-9--cascade-privileges-on-lakebase-and-the-slirp4netns-licence-2026-10-30)).**
`slirp4netns` carries the SPDX identifier **`GPL-2.0-or-later`**: its `COPYING` is
the GNU GPL version 2, its README says `GPL-2.0-or-later`, and ten source files
repeat it (release `v1.3.6`, the version the probes ran). One header says
`LGPL-2.1+` and the vendored `parson` is `MIT`. The Decision holds: appmux starts
the program as a separate process and fetches it from the upstream release, so it
neither links nor redistributes it, and no `NOTICE` file is needed. If the plan
bundles the binary with appmux, the source-offer duty of the GPL falls on whoever
distributes it and this ADR needs a follow-up. That is a reading of the licence
text, not legal advice. The static binary also contains `libslirp`, glib, libcap
and libseccomp, whose licences were not read.
