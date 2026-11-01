#!/usr/bin/env bash
# Fetch the unmodified upstream static slirp4netns binary and the sources and notices that
# travel with it into the git-ignored build/slirp4netns/ (ADR 0015; FR-054: the repository
# itself never holds the binary or its source).
#
# Output (consumed by scripts/assemble-deploy.sh):
#   build/slirp4netns/bin/slirp4netns
#   build/slirp4netns/licenses/slirp4netns/{COPYING,SOURCE.txt,slirp4netns-<version>.tar.gz,
#       LICENSE-parson,libslirp-<version>.tar.gz,LICENSE-libslirp,COPYRIGHT-libslirp,
#       LICENSE-LGPL-2.1}
#
# Every download is checked against the SHA-256 pinned below; a mismatch deletes the download
# and fails, and nothing reaches build/slirp4netns/ unless all downloads pass (a failed run
# keeps the previous output). To move to a new release, change the constants together and
# re-read the licences of the libraries inside the binary first (docs: ADR 0015).
#
# Licence analysis of the pinned v1.3.6 static x86_64 binary. This is a reading of licence texts
# and of the upstream build recipe, not legal advice.
#   - slirp4netns (GPL-2.0-or-later) and the vendored parson (MIT): the source archive and the
#     notices ship in the folder, so these are met.
#   - libslirp v4.9.5 (BSD-3-Clause and MIT, per file): its source archive, LICENSE and
#     COPYRIGHT ship in the folder, so this is met.
#   - libcap is NOT linked. slirp4netns/Makefile.am in the pinned source archive has
#         slirp4netns_LDADD = libparson.a @GLIB_LIBS@ @SLIRP_LIBS@ @LIBSECCOMP_LIBS@ -lpthread
#     and no LIBCAP_LIBS; only LIBCAP_CFLAGS reaches the compiler, and sandbox.c includes
#     <sys/capability.h> for the kernel struct definitions only. (Dockerfile.artifact installs
#     libcap-dev; the binary holds no libcap symbol or string.)
#   - glibc 2.35 (static libc.a), glib and libseccomp 2.5.3 are linked in statically, from the
#     Ubuntu jammy packages named in Dockerfile.artifact. glibc and glib are
#     LGPL-2.1-or-later, libseccomp is LGPL-2.1. The owner chose route 1 of ADR 0015 on
#     2026-11-01: the folder carries the LGPL-2.1 text and SOURCE.txt carries a written offer
#     (LGPL-2.1 section 6c), valid for three years, for the source of those three libraries
#     and for what a recipient needs to relink. The libraries' source itself is not shipped.
set -euo pipefail

# --- Pinned upstream release (change only together) ------------------------------------
readonly SLIRP4NETNS_RELEASE="v1.3.6"
readonly SLIRP4NETNS_BINARY_URL="https://github.com/rootless-containers/slirp4netns/releases/download/v1.3.6/slirp4netns-x86_64"
readonly SLIRP4NETNS_BINARY_SHA256="411167abceff1fc8a7c562f53ed9f26d3ec8a17df4e2fe6eaa3bd53f1c1e1d48"
readonly SLIRP4NETNS_SOURCE_URL="https://github.com/rootless-containers/slirp4netns/archive/refs/tags/v1.3.6.tar.gz"
readonly SLIRP4NETNS_SOURCE_SHA256="1e58c3a22d1f8361c9dfd7e518f621bc5510df519d00dbdd40954b77c0e1102f"
# libslirp is linked into the static binary (the release notes name v4.9.5). Its BSD-3-Clause
# and MIT notices live in the headers of its files, so its source travels beside them.
readonly LIBSLIRP_RELEASE="v4.9.5"
readonly LIBSLIRP_SOURCE_URL="https://github.com/qemu/libslirp/archive/refs/tags/v4.9.5.tar.gz"
readonly LIBSLIRP_SOURCE_SHA256="3881a82ac476567c46146980fea439f6ecfa631bed533c584c1a6ed19c309ebc"
# The LGPL-2.1 text covers glibc, glib and libseccomp. The hash is trust-on-first-use: taken
# on 2026-11-01 from the file served at this URL after reading its heading and section 6.
readonly LGPL21_TEXT_URL="https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt"
readonly LGPL21_TEXT_SHA256="20e50fe7aae3e56378ebf0417d9de904f55a0e61e4df315333e632a4d3555d95"
# Where a recipient of the zip asks for the source of those libraries (written offer, LGPL-2.1
# section 6c). It has to stay reachable for three years after each release.
readonly SOURCE_OFFER_CONTACT_URL="https://github.com/timezlab/databricks-appmux/issues"
# ----------------------------------------------------------------------------------------

# Test seams: local file:// URLs and another output folder. The pinned hashes above have no
# override, so a redirected URL still has to deliver the pinned bytes. file:// is allowed as
# a direct URL only: --proto-redir below never lets an https redirect lead to a local file.
binary_url="${APPMUX_SLIRP4NETNS_BINARY_URL:-$SLIRP4NETNS_BINARY_URL}"
slirp_source_url="${APPMUX_SLIRP4NETNS_SOURCE_URL:-$SLIRP4NETNS_SOURCE_URL}"
libslirp_source_url="${APPMUX_LIBSLIRP_SOURCE_URL:-$LIBSLIRP_SOURCE_URL}"
lgpl_text_url="${APPMUX_LGPL21_TEXT_URL:-$LGPL21_TEXT_URL}"

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="${APPMUX_SLIRP4NETNS_OUTPUT_DIR:-$repository_root/build/slirp4netns}"

slirp_version="${SLIRP4NETNS_RELEASE#v}"
libslirp_version="${LIBSLIRP_RELEASE#v}"
slirp_archive_name="slirp4netns-${slirp_version}.tar.gz"
libslirp_archive_name="libslirp-${libslirp_version}.tar.gz"

fail() {
    echo "fetch-slirp4netns.sh: $*" >&2
    exit 1
}

[[ -n "$output_dir" && "$output_dir" != "/" ]] || fail "refusing to use '$output_dir' as the output folder"
for tool in curl sha256sum tar find; do
    command -v "$tool" >/dev/null || fail "$tool is required but was not found on PATH"
done

# The final replace step runs rm -rf on the output folder, so it may only name a folder this
# script made (it holds the marker file) or one with nothing in it. A mis-set override that
# points at somebody's folder is refused before anything is downloaded or deleted.
output_marker_name=".appmux-slirp4netns-output"
if [[ -e "$output_dir" || -L "$output_dir" ]]; then
    if [[ -L "$output_dir" || ! -d "$output_dir" ]]; then
        fail "refusing to replace '$output_dir': it is not a plain folder (check APPMUX_SLIRP4NETNS_OUTPUT_DIR)"
    fi
    if [[ ! -f "$output_dir/$output_marker_name" ]]; then
        # A failed listing must not read as an empty folder: that would let the rm -rf below
        # delete a folder this script could not even look into.
        first_entry="$(find "$output_dir" -mindepth 1 -print -quit)" \
            || fail "could not list '$output_dir', so it cannot be shown to be empty (check its permissions and APPMUX_SLIRP4NETNS_OUTPUT_DIR)"
        if [[ -n "$first_entry" ]]; then
            fail "refusing to replace '$output_dir': it is not empty and has no $output_marker_name, so this script did not make it (check APPMUX_SLIRP4NETNS_OUTPUT_DIR; remove the folder yourself if it is stale)"
        fi
    fi
fi

output_parent="$(dirname "$output_dir")"
mkdir -p "$output_parent"
staging_dir="$(mktemp -d "$output_parent/.fetch-slirp4netns.XXXXXX")"
cleanup() { rm -rf "$staging_dir"; }
trap cleanup EXIT

downloads="$staging_dir/downloads"
tree="$staging_dir/tree"
licences="$tree/licenses/slirp4netns"
mkdir -p "$downloads" "$tree/bin" "$licences"
printf 'Made by scripts/fetch-slirp4netns.sh; the script may delete and rebuild this folder.\n' >"$tree/$output_marker_name"

# fetch_verified <file name> <url> <expected sha256>: download, then delete and fail on a mismatch.
fetch_verified() {
    local name="$1" url="$2" expected="$3" actual
    curl --fail --silent --show-error --location \
        --proto '=https,file' --proto-redir '=https' \
        --output "$downloads/$name" "$url" \
        || fail "could not download $name from $url"
    actual="$(sha256sum "$downloads/$name" | cut -d' ' -f1)"
    if [[ "$actual" != "$expected" ]]; then
        rm -f "$downloads/$name"
        fail "refusing $name downloaded from $url: SHA-256 is $actual, pinned value is $expected (download deleted)"
    fi
    echo "verified $name (sha256 $actual)"
}

# extract_member <archive> <member> <destination>: copy one file out of an archive, fail if absent.
extract_member() {
    local archive="$1" member="$2" destination="$3"
    tar -xzOf "$archive" "$member" >"$destination" 2>/dev/null \
        || fail "$(basename "$archive") has no $member (a licence file the zip must carry)"
}

fetch_verified "slirp4netns-x86_64" "$binary_url" "$SLIRP4NETNS_BINARY_SHA256"
fetch_verified "$slirp_archive_name" "$slirp_source_url" "$SLIRP4NETNS_SOURCE_SHA256"
fetch_verified "$libslirp_archive_name" "$libslirp_source_url" "$LIBSLIRP_SOURCE_SHA256"
fetch_verified "lgpl-2.1.txt" "$lgpl_text_url" "$LGPL21_TEXT_SHA256"

# The binary is stored byte for byte as published ("unmodified upstream build").
install -m 0755 "$downloads/slirp4netns-x86_64" "$tree/bin/slirp4netns"

slirp_prefix="slirp4netns-${slirp_version}"
libslirp_prefix="libslirp-${libslirp_version}"
extract_member "$downloads/$slirp_archive_name" "$slirp_prefix/COPYING" "$licences/COPYING"
extract_member "$downloads/$slirp_archive_name" "$slirp_prefix/vendor/parson/LICENSE" "$licences/LICENSE-parson"
extract_member "$downloads/$libslirp_archive_name" "$libslirp_prefix/LICENSE" "$licences/LICENSE-libslirp"
extract_member "$downloads/$libslirp_archive_name" "$libslirp_prefix/COPYRIGHT" "$licences/COPYRIGHT-libslirp"
install -m 0644 "$downloads/$slirp_archive_name" "$licences/$slirp_archive_name"
install -m 0644 "$downloads/$libslirp_archive_name" "$licences/$libslirp_archive_name"
install -m 0644 "$downloads/lgpl-2.1.txt" "$licences/LICENSE-LGPL-2.1"

cat >"$licences/SOURCE.txt" <<EOF
slirp4netns ${SLIRP4NETNS_RELEASE} (static x86_64 binary, bin/slirp4netns)

This is the unmodified upstream build: appmux did not change, rebuild or repack it.
slirp4netns is licensed GPL-2.0-or-later (COPYING). appmux starts it as a separate
process and does not link it; appmux's own files are Apache-2.0 (LICENSE).

The source of this release is the archive beside this file, ${slirp_archive_name}.
The upstream build recipe (Dockerfile.artifact) is inside that archive. Anyone who
received this zip may take, study, modify and redistribute the source of slirp4netns and
libslirp from the two archives in this folder. The LGPL libraries named further down are
covered by the written offer at the end of this file.

What this folder holds
  COPYING             GNU GPL version 2, the licence of slirp4netns
  SOURCE.txt          this file
  ${slirp_archive_name}
                      source archive of slirp4netns ${SLIRP4NETNS_RELEASE} (includes the vendored parson)
  LICENSE-parson      MIT notice of parson, the JSON parser vendored in that archive
  ${libslirp_archive_name}
                      source archive of libslirp ${LIBSLIRP_RELEASE}
  LICENSE-libslirp    BSD-3-Clause text taken from the libslirp archive
  COPYRIGHT-libslirp  copyright holders taken from the libslirp archive
  LICENSE-LGPL-2.1    GNU LGPL version 2.1, the licence of glibc, glib and libseccomp

Binary
  URL     ${SLIRP4NETNS_BINARY_URL}
  SHA-256 ${SLIRP4NETNS_BINARY_SHA256}

Source archive of the same release (${slirp_archive_name})
  URL     ${SLIRP4NETNS_SOURCE_URL}
  SHA-256 ${SLIRP4NETNS_SOURCE_SHA256}

libslirp ${LIBSLIRP_RELEASE} source archive (${libslirp_archive_name})
  URL     ${LIBSLIRP_SOURCE_URL}
  SHA-256 ${LIBSLIRP_SOURCE_SHA256}

GNU LGPL version 2.1 text (LICENSE-LGPL-2.1)
  URL     ${LGPL21_TEXT_URL}
  SHA-256 ${LGPL21_TEXT_SHA256}
EOF
cat >>"$licences/SOURCE.txt" <<'EOF'
  The upstream recipe (Dockerfile.artifact) clones libslirp from
  https://gitlab.freedesktop.org/slirp/libslirp at tag v4.9.5. The archive above is
  the GitHub archive of the same tag name; it was not compared with that checkout.

Libraries linked into the static binary (from the upstream release notes, the build
recipe and the binary; a reading of licence texts, not legal advice)
  slirp4netns v1.3.6   GPL-2.0-or-later   source: the archive in this folder
  parson (vendored)    MIT                source: inside the slirp4netns archive, vendor/parson
  libslirp v4.9.5      BSD-3-Clause, MIT in some files   source: the archive in this folder
  glibc 2.35           LGPL-2.1-or-later  statically linked; source: written offer below
  glib                 LGPL-2.1-or-later  Ubuntu jammy package; source: written offer below
  libseccomp 2.5.3     LGPL-2.1           statically linked; source: written offer below

libcap is NOT linked. In the slirp4netns archive, Makefile.am links
  slirp4netns_LDADD = libparson.a @GLIB_LIBS@ @SLIRP_LIBS@ @LIBSECCOMP_LIBS@ -lpthread
and defines no LIBCAP_LIBS; sandbox.c includes <sys/capability.h> only for kernel struct
definitions. The build installs libcap-dev for that header and nothing of libcap is in
the binary.

The archives in this folder do NOT contain the source of glibc, glib or libseccomp. The
binary was built from the Ubuntu 22.04 (jammy) packages of the snapshot jammy-20260509
(UBUNTU_VERSION in Dockerfile.artifact). Their sources can be obtained as the Ubuntu
source packages `glibc`, `glib2.0` and `libseccomp` (for example `apt-get source` with a
jammy deb-src entry). Upstream releases: libseccomp
https://github.com/seccomp/libseccomp/releases/tag/v2.5.3 and glibc
https://github.com/bminor/glibc/tree/glibc-2.35. An upstream tag is not the
Ubuntu-patched source the binary was built from.

Written offer for the source of glibc, glib and libseccomp (LGPL-2.1 section 6c)
EOF
cat >>"$licences/SOURCE.txt" <<EOF
glibc, glib and libseccomp are free software under the GNU Lesser General Public License
version 2.1 (LICENSE-LGPL-2.1 in this folder is a copy of it). You may modify those
libraries for your own use and relink the program with your modified copy; reverse
engineering to debug such modifications is permitted.

appmux offers, in writing, to give you on request, for a charge no more than the cost of
performing the distribution, and valid for three years from the date you received this
zip:
  1. the complete corresponding machine-readable source code of glibc, glib and libseccomp
     as they were built into this binary (the Ubuntu source packages named above, of the
     snapshot jammy-20260509), and
  2. the program that uses them, as source: the slirp4netns and libslirp archives in this
     folder and the build recipe inside the slirp4netns archive, so that you can relink.
To ask, open an issue at ${SOURCE_OFFER_CONTACT_URL} and name the release tag of the zip
you received.
EOF

# Replace the previous output only now that every check has passed.
rm -rf "$output_dir"
mv "$tree" "$output_dir"
chmod 0755 "$output_dir"

echo "wrote $output_dir"
echo "binary $(stat -c %s "$output_dir/bin/slirp4netns") bytes, archives $(stat -c %s "$output_dir/licenses/slirp4netns/$slirp_archive_name") + $(stat -c %s "$output_dir/licenses/slirp4netns/$libslirp_archive_name") bytes"
cat >&2 <<EOF
The folder carries a written offer for the source of glibc, glib and libseccomp (LGPL-2.1
section 6c), valid for three years from each release. Keep ${SOURCE_OFFER_CONTACT_URL}
reachable for that long: a private repository is not reachable for a recipient of the zip
(ADR 0015 "Must now be true").
EOF
