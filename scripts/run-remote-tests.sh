#!/usr/bin/env bash
# Run the container integration tests from this machine against the deployed development App.
#
# Usage: scripts/run-remote-tests.sh [pytest arguments]
#   Without arguments it runs tests/integration with -s -ra (the load and p95 reports are printed
#   by the tests, so their output must be shown). Any arguments replace those defaults:
#   scripts/run-remote-tests.sh tests/integration/test_zero_downtime.py -s -k update
#
# The tests need a real Databricks proxy in front of appmux, so they do not run inside the
# container. They run here, as the owner of the Databricks CLI's OAuth token: this script asks the
# CLI for the App's address and a fresh token, puts the fixtures (tests/fixtures, with the wheels
# of the offline-install ones built in a copy) into the workspace, and starts pytest with
# APPMUX_TEST_REMOTE=1 (tests/integration/ops_session.py). The token owner must be a platform
# admin of the App: deploy with APPMUX_DEPLOY_BOOTSTRAP_ADMIN (scripts/sync-deploy.sh).
# Tests that act as another person, or that need the container's own port, skip themselves.
# The App must carry the scopes the tests use in its user_api_scopes: workspace.workspace:read for
# Update (without it the folder reads as "empty, or you cannot read it"), and sql or files.files:read
# for the attach checks. `databricks apps update` replaces the whole body (no default scopes, no
# compute_size), and a changed scope reaches tokens only after the App is stopped and started.
#
# Environment (local, never committed; the script stops and names the first one that is unset,
# before it calls the Databricks CLI):
#   APPMUX_DATABRICKS_PROFILE  Databricks CLI profile to use. Required, never defaulted.
#   APPMUX_WORKSPACE_FOLDER    the folder of the deployment (as for sync-deploy.sh); it is masked
#                              in the output and names the default fixtures folder.
# Optional:
#   APPMUX_APP_NAME            the App under test. Default: appmux.
#   APPMUX_FIXTURES_FOLDER     workspace folder for the fixtures. Default:
#                              $APPMUX_WORKSPACE_FOLDER-fixtures.
#   APPMUX_REPO_ROOT           the repository root. Default: the parent of scripts/.
#   APPMUX_TEST_RATE_PER_S     requests a second of the load tests. Default here: 20. The tests'
#                              own default is 500, which through the platform's proxy and this
#                              machine's network only queues (2026-11-04: p95 about 52 s and 40
#                              requests a second actually served), so a remote run measures nothing.
# Any other APPMUX_TEST_* variable a test names (a warehouse id, a secret reference, ...) is
# passed on as it is in your environment.
#
# The token is asked for at the start and lasts at most an hour (the CLI hands out its cached one
# until it is close to expiry, so it may have far less left). The script prints how long it has. The
# admin-API calls of the tests (tests/integration/ops_session.py) ask the CLI again when the token
# has under ten minutes left, because pytest is given the profile's name in APPMUX_TEST_TOKEN_PROFILE
# (an environment variable, never an argument), so a 70-minute test such as the shared-SP token cycle
# can run. Two things still hold only the first token: the load generators of test_zero_downtime.py
# and the Lakebase test, which connects with it as a password. A run of those that outlasts the
# token gets a 302 (or a login failure) on every call after that, and the tests then fail with
# "got 302", which looks like a product fault and is not. Start them with at least 50 minutes left.
#
# The address and the token never appear in the output or on a command line: the token reaches
# pytest only through its environment, and everything pytest and the CLI print passes through
# mask_identifiers (scripts/lib/mask-identifiers.sh).

set -euo pipefail

script_name=$(basename -- "$0")
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

die() {
  printf '%s: %s\n' "$script_name" "$*" >&2
  exit 1
}

require_variable() {
  local variable_name=$1
  [[ -n "${!variable_name:-}" ]] \
    || die "$variable_name is not set; export it in your shell (it is local and is never committed)"
}

require_variable APPMUX_DATABRICKS_PROFILE
require_variable APPMUX_WORKSPACE_FOLDER
fixtures_folder=${APPMUX_FIXTURES_FOLDER:-$APPMUX_WORKSPACE_FOLDER-fixtures}
[[ "$fixtures_folder" == /Workspace/* ]] \
  || die "APPMUX_FIXTURES_FOLDER (default: APPMUX_WORKSPACE_FOLDER plus -fixtures) must start with /Workspace/"

default_rate_per_s=20
app_name=${APPMUX_APP_NAME:-appmux}
repo_root=${APPMUX_REPO_ROOT:-$(dirname -- "$script_dir")}
pytest_arguments=("$@")
((${#pytest_arguments[@]} > 0)) || pytest_arguments=(tests/integration -s -ra)

command -v databricks > /dev/null || die "the Databricks CLI ('databricks') is not on PATH"
command -v uv > /dev/null || die "uv is not on PATH"
command -v python3 > /dev/null || die "python3 is needed to read the CLI's JSON output"

# shellcheck source=lib/mask-identifiers.sh
source "$script_dir/lib/mask-identifiers.sh"

work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT

# Run the CLI with the profile. Standard output is the caller's; standard error is masked.
call_cli() {
  local status=0
  databricks "$@" --profile "$APPMUX_DATABRICKS_PROFILE" 2> "$work_dir/cli-error.txt" || status=$?
  mask_identifiers < "$work_dir/cli-error.txt" >&2
  return "$status"
}

# One JSON document on stdin -> the string in the field named by the argument (empty when absent).
read_field() {
  python3 -I -c '
import json
import sys

value = json.load(sys.stdin).get(sys.argv[1])
print(value if isinstance(value, str) else "")
' "$1"
}

app_json=$(call_cli apps get "$app_name" -o json) || die "databricks apps get failed"
app_url=$(printf '%s' "$app_json" | read_field url)
[[ -n "$app_url" ]] || die "the CLI gave no address for the App $app_name (is it deployed and running?)"

token_json=$(call_cli auth token -o json) \
  || die "databricks auth token failed; log in again with the CLI for this profile"
user_token=$(printf '%s' "$token_json" | read_field access_token)
[[ -n "$user_token" ]] || die "the CLI gave no access token (log in again with the CLI for this profile)"

# Only a diagnostic: when the expiry cannot be read the run still goes on, and says so.
token_minutes_left=$(printf '%s' "$token_json" | python3 -I -c '
import json
import sys
from datetime import datetime, timezone

try:
    expiry = datetime.fromisoformat(json.load(sys.stdin)["expiry"])
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    print(int((expiry - datetime.now(timezone.utc)).total_seconds() // 60))
except (KeyError, ValueError):
    print("unknown")
')
printf 'the token has %s minutes left\n' "$token_minutes_left"

# The fixtures go up from a copy: the wheels of the offline-install ones and of pkg_env_writer
# (it needs an overlay to probe) are built into it (they are never committed), bytecode is left out, and databricks sync does not read this
# repository's .gitignore.
fixtures_stage="$work_dir/fixtures"
cp -a -- "$repo_root/tests/fixtures" "$fixtures_stage"
find "$fixtures_stage" -name __pycache__ -type d -prune -exec rm -rf -- {} +
for fixture in wheels_only wheel_reuse env_covered newer_wheel pkg_env_writer; do
  python3 -I "$fixtures_stage/$fixture/make_wheels.py" "$fixtures_stage/$fixture/wheels" > /dev/null
done

printf 'syncing the fixtures to the workspace\n'
call_cli sync --full "$fixtures_stage" "$fixtures_folder" | mask_identifiers \
  || die "databricks sync of the fixtures failed"

printf 'running pytest against %s as the owner of the CLI token\n' "$app_name"
cd -- "$repo_root"
APPMUX_TEST_CONTAINER=1 \
  APPMUX_TEST_REMOTE=1 \
  APPMUX_TEST_BASE_URL="$app_url" \
  APPMUX_TEST_FIXTURES_FOLDER="$fixtures_folder" \
  APPMUX_TEST_USER_TOKEN="$user_token" \
  APPMUX_TEST_TOKEN_PROFILE="$APPMUX_DATABRICKS_PROFILE" \
  APPMUX_TEST_RATE_PER_S="${APPMUX_TEST_RATE_PER_S:-$default_rate_per_s}" \
  uv run pytest "${pytest_arguments[@]}" 2>&1 | mask_identifiers
