# Bundle slirp4netns in the release zip with its source

**Status:** accepted
**Date:** 2026-10-30 (proposed); accepted 2026-11-01
**Deciders:** Liam Lee (asked on 2026-10-30 for a release zip that deploys as unpacked, with no manual `slirp4netns` step; on 2026-11-01 accepted the duty below and chose route 1 for the `LGPL` libraries)

## Context

[ADR 0013](0013-license-appmux-under-apache-2-0.md) decided that appmux
neither links nor redistributes `slirp4netns`, so an operator would download
the binary and add it to the unpacked release as `bin/slirp4netns`. The release
zip ([plan](../../specs/001-appmux-v1/plan.md#source-code-repository-root)) is
meant to be unpacked and deployed as it is; that manual step contradicts it,
and a start-time download is not an option where the container has no network
(Free Edition; the company workspace was not measured).

Spike 9 read the licence: `slirp4netns` is `GPL-2.0-or-later` (release
`v1.3.6`). The same spike listed `libslirp`, glib, libcap and libseccomp as the
libraries of the static build without reading their licences. Reading the
build recipe and the binary on 2026-10-30 corrected that list: the binary
embeds `slirp4netns` itself (`GPL-2.0-or-later`), its vendored `parson` (MIT),
`libslirp` v4.9.5 (BSD-3-Clause and MIT), glibc 2.35 linked statically
(`LGPL-2.1-or-later`), glib (`LGPL-2.1-or-later`) and libseccomp 2.5.3
(`LGPL-2.1` only). libcap is **not** linked: the build only includes its header
for kernel struct definitions. ADR 0013 already says that
bundling the binary moves the GPL source-offer duty to whoever distributes it
and needs a follow-up. This is that follow-up. It is a reading of the licence
text, not legal advice.

## Decision

The release zip contains the **unmodified upstream static `slirp4netns`
binary** at `bin/slirp4netns` and, beside it under `licenses/slirp4netns/`, its
`COPYING`, a `SOURCE.txt`, the upstream source archive of the same release,
`LICENSE-parson`, the `libslirp` v4.9.5 source archive and its
`LICENSE-libslirp` and `COPYRIGHT-libslirp`, plus `LICENSE-LGPL-2.1`. `SOURCE.txt`
names the release, the four upstream URLs and the four SHA-256 values (binary,
`slirp4netns` archive, `libslirp` archive, the `LGPL-2.1` text), states that
anyone who received the zip may obtain the source of `slirp4netns` and `libslirp`
from the archives in it, and carries a **written offer** (LGPL-2.1 section 6c)
for the three `LGPL` libraries in the binary: glibc, glib and libseccomp. The
offer is valid for three years from the date a recipient received the zip, is
for a charge no more than the cost of the distribution, and covers (1) the
corresponding source of those libraries as built into the binary (the Ubuntu
jammy source packages `glibc`, `glib2.0` and `libseccomp`) and (2) the
`slirp4netns` and `libslirp` sources and the build recipe in the zip, so that a
recipient can relink with a modified library. A recipient asks at the contact
URL pinned in `scripts/fetch-slirp4netns.sh` and names the release tag.

- The **repository still never holds the binary or its source**
  ([FR-054](../../specs/001-appmux-v1/spec.md)): `scripts/fetch-slirp4netns.sh`
  downloads all four files into the git-ignored `build/slirp4netns/` and checks
  each against a SHA-256 pinned in the script. The binary's hash equals the
  digest the GitHub release API reports; the two source-archive hashes and the
  `LGPL-2.1` text hash are trust-on-first-use (GitHub-generated tarballs are not
  guaranteed byte-stable, so a changed hash means "re-pin after checking the
  tag", not "ignore").
- appmux still starts the program as its own process and does not link it, so
  appmux's own files stay Apache-2.0 and `LICENSE` stays as ADR 0013 says. The
  zip carries `LICENSE` too.
- The developer's `assemble-deploy.sh` and the release workflow build the same
  folder, so a deployment from a laptop and one from the zip do not differ.

## Alternatives considered

- **Keep adding the binary by hand (ADR 0013 as written)** — rejected by the
  owner: every operator repeats a download and a copy, and a forgotten step
  shows up as a failed `self_check` row.
- **Two release assets, one with the binary and one without** — the fallback if
  the owner or counsel will not accept the duty for the main asset. It costs an
  operator decision and a second file to explain; nothing else in this design
  changes. Not needed now that the duty is accepted.
- **The three `LGPL` source packages inside the zip (no later duty)** — not
  chosen on 2026-11-01 (the owner took the recommended route 1): it adds several
  tens of MB to every release for libraries few recipients will ask about, and the
  exact jammy package versions of the build snapshot have not been determined, so
  pinning their hashes would first need that research.
- **Download the binary at first start** — impossible where the container has
  no network, and a start-time download is a supply-chain path of its own.
- **Bind `libslirp` from appmux (no GPL program)** — rejected: the glue that
  `slirp4netns` provides would become new native code in the most
  security-sensitive part of the design.
- **Write a user-space NAT of its own** — rejected: a project in itself and a
  new attack surface, to avoid shipping one source archive.

## Consequences

**Better:**
- The zip is complete: unpack, upload, deploy. The missing-binary `self_check`
  row stays as a guard but should never fire for a release zip.
- One build path for development and release.

**Worse:**
- The project, and anyone who re-hosts the zip, redistributes a
  `GPL-2.0-or-later` program and owes its source with it.
- The static binary embeds libraries with their own conditions. The `LGPL`
  ones (glibc, glib, libseccomp) require more than the program's own source,
  and their corresponding source is the patched Ubuntu jammy packages, not a
  GitHub tag. The zip does not carry it; it carries the written offer, so the
  project owes that source on request for three years after each release, and
  someone has to answer the request.
- The zip grows by the binary and the source archive (their sizes are recorded
  the first time `fetch-slirp4netns.sh` runs).

**Must now be true:**
- The owner has accepted this ADR before the first release tag whose zip
  carries the binary; the release workflow refuses to publish while this file's
  status is not `accepted`.
- The pinned release, the three URLs and the three SHA-256 values are constants
  in `scripts/fetch-slirp4netns.sh`; a mismatch deletes the download and fails.
- The zip meets the `LGPL` duty for glibc, glib and libseccomp by route 1 (chosen
  by the owner on 2026-11-01): `LICENSE-LGPL-2.1` and the written offer in
  `SOURCE.txt` are in `licenses/slirp4netns/`, and `scripts/fetch-slirp4netns.sh`
  writes them. Whether an unpatched upstream tag counts as "complete
  corresponding source" is a question for counsel; this ADR does not answer it,
  and the offer names the Ubuntu packages, not a tag.
- **Before the first release tag**, the contact URL in the offer
  (`SOURCE_OFFER_CONTACT_URL` in `scripts/fetch-slirp4netns.sh`, today the issue
  tracker of `timezlab/databricks-appmux`) is reachable by a recipient of the
  zip, and stays so for three years after the last release that carried the
  binary. The repository was private when this was accepted; the owner made it
  public on 2026-11-01 (its issues page answered an unauthenticated request),
  so the URL is reachable today. Making it private again, deleting the repository,
  or moving it without changing the constant ends that, and the owner then owes a
  replacement contact. Nothing in the workflow can check this.
- `assemble-deploy.sh` fails when `bin/slirp4netns`, `COPYING`, `SOURCE.txt`,
  `LICENSE-LGPL-2.1` or the source archive is missing from the assembled folder.
- No `slirp4netns` binary or source is committed (FR-054).

## Revisit if

The owner or counsel declines the duty, the library review finds a condition
the zip cannot meet, upstream stops publishing a static binary, or a build of
`slirp4netns` that is not GPL becomes available.


## Update 2026-11-06: the first release carried the binary

The Decision is unchanged and the status stays `accepted`. The first release tag, `v0.1.0`, was
pushed on 2026-11-06 and its zip, with `bin/slirp4netns` v1.3.6 and `licenses/slirp4netns/`, is
published as a GitHub Release. The release job found this ADR accepted. The contact URL of the
written offer (the issue tracker of `timezlab/databricks-appmux`) answered an unauthenticated
request with `200` just before the tag, which is the condition under "Before the first release
tag". The three-year offer therefore runs from this release, and the contact URL has to stay
reachable until then.
