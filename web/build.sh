#!/usr/bin/env bash
# Build the admin UI: install from the lockfile, build the static export into
# web/out, and check that the export has its entry page. scripts/sync-deploy.sh
# runs this before it assembles the deployment folder, and the release workflow
# runs it before it packs the zip, so a deployment never ships a stale or
# missing UI.
#
# Usage: web/build.sh
#
# `npm ci` installs exactly what package-lock.json names and never resolves a
# version afresh. `--ignore-scripts` keeps every dependency's install script
# from running: the build needs none of them (checked with a clean install on
# 2026-10-31), and an install script is where a hijacked package would run code
# on the builder's machine.
#
# The previous web/out is removed first, so a failed build leaves no export
# instead of one that looks current.

set -euo pipefail

script_name=$(basename -- "$0")
web_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

die() {
  printf '%s: %s\n' "$script_name" "$*" >&2
  exit 1
}

command -v npm >/dev/null 2>&1 \
  || die "npm is not installed; the admin UI needs Node 22.18 or newer (web/package.json, engines)"
[[ -f "$web_dir/package-lock.json" ]] \
  || die "web/package-lock.json is missing; without it npm would pick versions afresh"

cd -- "$web_dir"
rm -rf -- out
npm ci --ignore-scripts
npm run build

[[ -f out/index.html ]] \
  || die "the build finished but web/out/index.html is missing, so there is no admin UI to ship"
