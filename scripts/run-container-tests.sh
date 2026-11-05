#!/usr/bin/env bash
# Run container tests INSIDE the deployed development App: the ones that cannot be played through
# the platform's proxy from this machine (scripts/run-remote-tests.sh).
#
# Usage: scripts/run-container-tests.sh [pytest arguments]
#   Without arguments it runs tests/integration/test_edge_capacity.py (500 requests a second on the
#   edge's own port, and the edge's CPU) and test_self_check_container.py, with -s -ra. Any arguments
#   replace those defaults. The tests named must be in tests/integration; the ones that need apps,
#   a secret, a group or a warehouse registered first need those APPMUX_TEST_* variables in your
#   environment (they travel to the App with the others) and the App must be able to use them.
#
# What it does, in order:
#   1. asks the Databricks CLI for a fresh access token and puts the fixtures in the workspace;
#   2. builds an overlay (scripts/container_test_run.py): an app.yaml whose command is
#      scripts/container_test_entry.py and that carries APPMUX_TEST_* (the token among them), the
#      tests, a pytest.ini, the vendored pytest and a run configuration with a fresh run id;
#   3. deploys the App with the overlay laid over the synced copy (scripts/sync-deploy.sh
#      APPMUX_DEPLOY_OVERLAY): the App restarts, so every app it hosts is down for the minutes a
#      start takes and comes back by itself from the registry;
#   4. reads the App's log until the entry prints this run's closing line, then prints what pytest
#      printed;
#   5. ALWAYS deploys the normal code again, whatever happened, and checks that the test entry is
#      gone from the workspace folder (the overlay's app.yaml held your token there for the run).
#
# Environment (local, never committed; the script stops and names the first one that is unset,
# before it calls the Databricks CLI):
#   APPMUX_DATABRICKS_PROFILE       Databricks CLI profile to use. Required, never defaulted.
#   APPMUX_WORKSPACE_FOLDER         the folder of the deployment (as for sync-deploy.sh).
#   APPMUX_DEPLOY_BOOTSTRAP_ADMIN   your own address: the entry takes the admin of the run from it,
#                                   and the token's owner must be a platform admin of the App.
# Optional:
#   APPMUX_APP_NAME                 the App under test. Default: appmux.
#   APPMUX_FIXTURES_FOLDER          workspace folder for the fixtures. Default:
#                                   $APPMUX_WORKSPACE_FOLDER-fixtures.
#   APPMUX_REPO_ROOT                the repository root. Default: the parent of scripts/.
#   APPMUX_CONTAINER_TESTS_TIMEOUT_S  how long to wait for the closing line. Default: 2400.
#   APPMUX_CONTAINER_TESTS_POLL_S     seconds between reads of the log. Default: 30.
#   APPMUX_CONTAINER_TESTS_MAX_LOG_FAILURES  how many reads of the log in a row may fail before the
#                                   run stops waiting. Default: 10 (five minutes at the default poll).
#   APPMUX_CONTAINER_TESTS_READ_TIMEOUT_S  how long one read of the log may take; a read cut off
#                                   counts as a failed read. Default: 60.
#   APPMUX_CONTAINER_TESTS_LOG_TAIL_LINES  how many of the log's last lines one read asks for.
#                                   Default: 5000. Never 0: `--tail-lines 0` asks for the whole log
#                                   and hung for two hours on 2026-11-05 with the run long finished.
#
# Exit status: 0 the tests passed; 1 they failed; 70 pytest never started (the reason is printed);
# 3 the closing line never came, or the App's log could not be read for too long (the App's state
# is printed: on 2026-11-04 the platform had stopped the App's compute and the run waited 40 minutes
# for a log nobody could read); 4 the restore failed or left a test file in the workspace folder
# (the message says what to run); anything else: a step before the tests failed.
#
# The token lives, for the run, in the overlay's app.yaml (a 0600 file in a temporary folder, then in
# your workspace folder) and in the App's environment; it never appears on a command line or in
# the output. It lasts at most an hour, so a run is cut short when it outlasts the token.
# Everything printed from the CLI or the App's log passes through mask_identifiers
# (scripts/lib/mask-identifiers.sh).

set -euo pipefail
umask 077

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
require_variable APPMUX_DEPLOY_BOOTSTRAP_ADMIN
fixtures_folder=${APPMUX_FIXTURES_FOLDER:-$APPMUX_WORKSPACE_FOLDER-fixtures}
[[ "$fixtures_folder" == /Workspace/* ]] \
  || die "APPMUX_FIXTURES_FOLDER (default: APPMUX_WORKSPACE_FOLDER plus -fixtures) must start with /Workspace/"
timeout_s=${APPMUX_CONTAINER_TESTS_TIMEOUT_S:-2400}
poll_s=${APPMUX_CONTAINER_TESTS_POLL_S:-30}
max_log_failures=${APPMUX_CONTAINER_TESTS_MAX_LOG_FAILURES:-10}
read_timeout_s=${APPMUX_CONTAINER_TESTS_READ_TIMEOUT_S:-60}
log_tail_lines=${APPMUX_CONTAINER_TESTS_LOG_TAIL_LINES:-5000}
[[ "$timeout_s" =~ ^[0-9]+$ && "$poll_s" =~ ^[0-9]+$ ]] \
  || die "APPMUX_CONTAINER_TESTS_TIMEOUT_S and APPMUX_CONTAINER_TESTS_POLL_S must be whole numbers"
[[ "$max_log_failures" =~ ^[1-9][0-9]*$ && "$read_timeout_s" =~ ^[1-9][0-9]*$ \
  && "$log_tail_lines" =~ ^[1-9][0-9]*$ ]] \
  || die "APPMUX_CONTAINER_TESTS_MAX_LOG_FAILURES, _READ_TIMEOUT_S and _LOG_TAIL_LINES must be whole numbers of at least 1"

app_name=${APPMUX_APP_NAME:-appmux}
repo_root=${APPMUX_REPO_ROOT:-$(dirname -- "$script_dir")}
pytest_arguments=("$@")
((${#pytest_arguments[@]} > 0)) || pytest_arguments=(
  tests/integration/test_edge_capacity.py tests/integration/test_self_check_container.py -s -ra
)

command -v databricks > /dev/null || die "the Databricks CLI ('databricks') is not on PATH"
command -v uv > /dev/null || die "uv is not on PATH"
command -v timeout > /dev/null || die "timeout (GNU coreutils) is not on PATH"
command -v python3 > /dev/null || die "python3 is needed to read the CLI's JSON output"

# shellcheck source=lib/mask-identifiers.sh
source "$script_dir/lib/mask-identifiers.sh"
# shellcheck source=lib/sync-fixtures.sh
source "$script_dir/lib/sync-fixtures.sh"

work_dir=$(mktemp -d)
final_status=
restore_needed=0

# Run the CLI with the profile. Standard output is the caller's; standard error is masked.
call_cli() {
  local status=0
  databricks "$@" --profile "$APPMUX_DATABRICKS_PROFILE" 2> "$work_dir/cli-error.txt" || status=$?
  mask_identifiers < "$work_dir/cli-error.txt" >&2
  return "$status"
}

# The developer-side half: overlay, verdict, run id (scripts/container_test_run.py).
helper() {
  uv run python "$script_dir/container_test_run.py" "$@"
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

# Print what the platform says about the App: the two states and their messages. It explains a log
# nobody can read or a deployment that was refused. A read that fails prints nothing: this runs
# where the run is already failing, and must not hide the first error.
print_app_state() {
  local description
  description=$(call_cli apps get "$app_name" -o json 2> /dev/null | python3 -I -c '
import json
import sys

document = json.load(sys.stdin)
parts = []
for key in ("app_status", "compute_status"):
    status = document.get(key) or {}
    state = status.get("state")
    message = status.get("message")
    parts.append(f"{key}: {state} ({message})")
print("; ".join(parts))
' 2> /dev/null) || return 0
  printf '%s: the App says: %s\n' "$script_name" "$(printf '%s' "$description" | mask_identifiers)" >&2
}

# Take the overlay's files out of the workspace folder. The sync of sync-deploy.sh starts from a
# fresh copy each time, so it only uploads: what the normal code no longer has stays in the folder
# and would be deployed again (the first run on 2026-11-04 left the entry, the tests and the
# libraries there). app.yaml stays: the sync that follows replaces it.
remove_overlay_files() {
  local listing present name
  listing=$(call_cli workspace list "$APPMUX_WORKSPACE_FOLDER" -o json) || {
    printf '%s: could not list the workspace folder to remove the test files\n' "$script_name" >&2
    return 1
  }
  # Only names that are in the folder: a deployment that failed early may have synced nothing, and
  # deleting what is not there is an error.
  present=$(printf '%s' "$listing" | python3 -I -c '
import json
import os
import sys

in_folder = {os.path.basename(item["path"]) for item in json.load(sys.stdin)}
for name in sorted(os.listdir(sys.argv[1])):
    if name != "app.yaml" and name in in_folder:
        print(name)
' "$overlay_dir") || return 1
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    call_cli workspace delete --recursive "$APPMUX_WORKSPACE_FOLDER/$name" || return 1
  done <<<"$present"
}

# Put the normal code back and check that no test file stayed in the workspace folder.
restore() {
  local listing
  printf 'restoring the normal deployment\n'
  remove_overlay_files || return 1
  env -u APPMUX_DEPLOY_OVERLAY "$script_dir/sync-deploy.sh" || return 1
  listing=$(call_cli workspace list "$APPMUX_WORKSPACE_FOLDER" -o json) || {
    printf '%s: could not list the workspace folder to check the restore\n' "$script_name" >&2
    return 1
  }
  if [[ "$listing" == *container_test_entry.py* ]]; then
    printf '%s: container_test_entry.py is still in the workspace folder\n' "$script_name" >&2
    return 1
  fi
}

finish() {
  local status=$?
  trap - EXIT
  if ((restore_needed)) && ! restore; then
    {
      printf '%s: THE RESTORE FAILED. The App may still run the test entry, and the app.yaml in\n' "$script_name"
      printf '%s: your workspace folder holds your access token (it expires within an hour).\n' "$script_name"
      printf '%s: Run scripts/sync-deploy.sh now, then check the folder.\n' "$script_name"
    } >&2
    print_app_state
    final_status=4
  fi
  rm -rf -- "$work_dir"
  exit "${final_status:-$status}"
}
trap finish EXIT

cd -- "$repo_root"

token_json=$(call_cli auth token -o json) \
  || die "databricks auth token failed; log in again with the CLI for this profile"
user_token=$(printf '%s' "$token_json" | read_field access_token)
[[ -n "$user_token" ]] || die "the CLI gave no access token (log in again with the CLI for this profile)"

sync_fixtures "$repo_root" "$work_dir/fixtures" "$fixtures_folder"

run_id=$(helper new-run-id)
overlay_dir="$work_dir/overlay"
APPMUX_TEST_USER_TOKEN="$user_token" APPMUX_TEST_FIXTURES_FOLDER="$fixtures_folder" \
  helper overlay --repo-root "$repo_root" --out "$overlay_dir" --run-id "$run_id" \
  -- "${pytest_arguments[@]}" || die "the overlay could not be built"

printf 'deploying %s with the tests laid over it\n' "$app_name"
# From here the restore runs whatever happens: a deployment that fails may have synced the overlay.
restore_needed=1
APPMUX_DEPLOY_OVERLAY="$overlay_dir" "$script_dir/sync-deploy.sh" \
  || die "the deployment with the tests failed; the normal code is put back next"

waited_s=0
log_failures=0
while :; do
  # A few failed reads are the log not being there yet (the App is still starting); a run of them
  # is an App that is down, and waiting longer will not bring the closing line.
  if log=$(timeout --kill-after=5 "$read_timeout_s" \
    "$script_dir/fetch-logs.sh" --source APP --tail-lines "$log_tail_lines" 2>&1); then
    log_failures=0
  else
    log=
    log_failures=$((log_failures + 1))
    if ((log_failures >= max_log_failures)); then
      printf '%s: the App'"'"'s log could not be read %d times in a row; stopping the wait\n' \
        "$script_name" "$log_failures" >&2
      print_app_state
      verdict_status=3
      verdict_text="waiting: the App's log could not be read"
      break
    fi
  fi
  verdict_status=0
  verdict_text=$(printf '%s\n' "$log" | helper verdict --run-id "$run_id") || verdict_status=$?
  ((verdict_status == 2)) || break
  if ((waited_s >= timeout_s)); then
    printf '%s: gave up after %d seconds without this run'"'"'s closing line\n' \
      "$script_name" "$waited_s" >&2
    print_app_state
    verdict_status=3
    break
  fi
  printf 'waiting for the run (%d of %d seconds)\n' "$waited_s" "$timeout_s"
  sleep "$poll_s"
  waited_s=$((waited_s + poll_s))
done

((verdict_status != 64)) || die "the verdict could not be read: $verdict_text"
printf '%s\n' "$verdict_text"
if [[ "$log" == *"running pytest inside the container"* ]]; then
  printf '%s\n' "$log" | awk '/running pytest inside the container/ { found = 1 } found' | mask_identifiers
else
  printf '%s\n' "$log" | tail -n 80 | mask_identifiers
fi
final_status=$verdict_status
