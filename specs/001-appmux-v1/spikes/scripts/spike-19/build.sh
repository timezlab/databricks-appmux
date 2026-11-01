#!/bin/bash
# usage: build.sh <work-dir>   (a new or empty directory; needs the npm registry; runs no package install script)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
work=$1
mkdir -p "$work"
cp -r "$here/package.json" "$here/app-router" "$here/pages-router" "$work/"
cd "$work"
export NEXT_TELEMETRY_DISABLED=1
npm install --ignore-scripts --save-exact --no-audit --no-fund
(cd app-router && ../node_modules/.bin/next build)
(cd pages-router && ../node_modules/.bin/next build)
