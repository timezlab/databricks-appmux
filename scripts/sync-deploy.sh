#!/usr/bin/env bash
# Development deployment: build the deployment folder (assemble-deploy.sh), sync
# it to a workspace folder, deploy the App from that folder and wait for the
# deployment to reach a final state. An operator deploys the release zip
# instead (design 13.1); this script is for the developer's own workspace.
#
# Usage: scripts/sync-deploy.sh
#
# Run these first (once, and again when uv.lock or the slirp4netns pin changes):
#   scripts/vendor-wheels.sh      writes build/requirements.txt and build/wheels/
#   scripts/fetch-slirp4netns.sh  writes build/slirp4netns/ (the binary and its licences)
# This script does not run them: assemble-deploy.sh builds only from their outputs in build/,
# and both download from the network, which a deployment must not do unasked. Without them
# assemble-deploy.sh refuses ("run scripts/vendor-wheels.sh first") and nothing is uploaded.
# It does run web/build.sh first (npm ci --ignore-scripts, then the static export into
# web/out), so a deployment never carries a stale or missing admin UI; that needs Node 22.18+.
#
# Environment (local, never committed; the script stops and names the first one
# that is unset, before it calls the Databricks CLI):
#   APPMUX_WORKSPACE_FOLDER    workspace folder the deployment folder is synced to
#                              (an absolute workspace path)
#   APPMUX_DATABRICKS_PROFILE  Databricks CLI profile to use. Required, never
#                              defaulted: an implicit default profile could point
#                              at a workspace this project must not touch.
# Optional:
#   APPMUX_LAKEBASE_ENDPOINT   the Lakebase endpoint's resource name,
#                              projects/<project>/branches/<branch>/endpoints/<endpoint>.
#                              When it is set, the deployment first loads the registry schema
#                              (scripts/load-schema.sh, with your own Lakebase login, from the
#                              assembled folder's migrations/) and uploads nothing if that fails.
#                              A registry that is already loaded is left as it is and the
#                              deployment goes on. When it is unset the step is skipped, and the
#                              script says so.
#   APPMUX_APP_NAME            the App to deploy. Default: appmux.
#   APPMUX_POLL_INTERVAL_S     seconds between deployment checks. Default: 10.
#   APPMUX_POLL_ATTEMPTS       checks before giving up. Default: 60.
#   APPMUX_DEPLOY_BOOTSTRAP_ADMIN   one e-mail address that replaces the placeholder of the
#                              APPMUX_BOOTSTRAP_ADMINS entry in app.yaml, in the copy that is
#                              synced and nowhere else: the repository, build/deploy and a
#                              release zip keep the placeholder. It makes that person the
#                              first platform admin of this development App (the integration
#                              tests need one). The value is never printed.
#   APPMUX_DEPLOY_CONTENT_STORE_FOLDER   a workspace folder (/Workspace/...) that turns the
#                              commented APPMUX_CONTENT_STORE_FOLDER template of app.yaml into an
#                              entry, in the synced copy only. Unset, appmux keeps its content
#                              store in the service principal's own folder, which is lost when
#                              the App is deleted and created again. A fixed folder is not: grant
#                              the App's service principal CAN_MANAGE on it (docs/product/
#                              v1-design.md section 3.3). Plain characters only; not printed.
#   APPMUX_DEPLOY_OVERLAY      a folder whose files are copied over the synced copy (its app.yaml
#                              replaces the assembled one), for a test run inside the container
#                              (scripts/run-container-tests.sh). Like the bootstrap admin it reaches
#                              the synced copy only: the repository, build/deploy and a release zip
#                              never hold it. The bootstrap admin is put into the overlay's app.yaml
#                              the same way.
#   APPMUX_REPO_ROOT, APPMUX_BUILD_DIR   as in assemble-deploy.sh.
#
# Nothing in the output names the workspace: CLI output passes through
# mask_identifiers (scripts/lib/mask-identifiers.sh, shared with fetch-logs.sh), which
# replaces the folder, URLs, host names, workspace ids, e-mail addresses and tokens. Files
# removed from the deployment folder are not removed from the workspace folder; delete that
# folder by hand for a clean start.

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

require_variable APPMUX_WORKSPACE_FOLDER
require_variable APPMUX_DATABRICKS_PROFILE
[[ "$APPMUX_WORKSPACE_FOLDER" == /* ]] \
  || die "APPMUX_WORKSPACE_FOLDER must be an absolute workspace path (it starts with /)"

# One plain address: a letter or digit first (a leading % or - means something to YAML), then
# the characters an address uses, and nothing else. The value goes into a YAML scalar, so a
# space, a comma, a quote or a line break must never get through. It is not echoed on refusal.
bootstrap_admin_pattern='^[A-Za-z0-9][A-Za-z0-9._+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'
if [[ -n "${APPMUX_DEPLOY_BOOTSTRAP_ADMIN:-}" ]]; then
  [[ "$APPMUX_DEPLOY_BOOTSTRAP_ADMIN" =~ $bootstrap_admin_pattern ]] \
    || die "APPMUX_DEPLOY_BOOTSTRAP_ADMIN must be one plain e-mail address (the value is not shown)"
fi

# A path under /Workspace/ of plain characters: it goes into a YAML scalar, so no space, quote,
# '#' or line break. No "." or ".." segment and no empty one (a "//") either. Not echoed.
store_folder_pattern='^/Workspace/[A-Za-z0-9._@+-]+(/[A-Za-z0-9._@+-]+)*$'
if [[ -n "${APPMUX_DEPLOY_CONTENT_STORE_FOLDER:-}" ]]; then
  [[ "$APPMUX_DEPLOY_CONTENT_STORE_FOLDER" =~ $store_folder_pattern \
    && "$APPMUX_DEPLOY_CONTENT_STORE_FOLDER/" != */./* \
    && "$APPMUX_DEPLOY_CONTENT_STORE_FOLDER/" != */../* ]] \
    || die "APPMUX_DEPLOY_CONTENT_STORE_FOLDER must be a plain path under /Workspace/ (the value is not shown)"
fi

if [[ -n "${APPMUX_DEPLOY_OVERLAY:-}" ]]; then
  [[ -d "$APPMUX_DEPLOY_OVERLAY" ]] \
    || die "APPMUX_DEPLOY_OVERLAY must be an existing folder"
fi

app_name=${APPMUX_APP_NAME:-appmux}
poll_interval_s=${APPMUX_POLL_INTERVAL_S:-10}
poll_attempts=${APPMUX_POLL_ATTEMPTS:-60}

command -v databricks > /dev/null || die "the Databricks CLI ('databricks') is not on PATH"
command -v python3 > /dev/null || die "python3 is needed to read the CLI's JSON output"

# mask_identifiers reads APPMUX_WORKSPACE_FOLDER, which was checked above.
# shellcheck source=lib/mask-identifiers.sh
source "$script_dir/lib/mask-identifiers.sh"

# Replace the value of the APPMUX_BOOTSTRAP_ADMINS entry in the file given as the argument with
# $APPMUX_DEPLOY_BOOTSTRAP_ADMIN. The address travels in the environment, not on a command line,
# and is never printed. Exactly one entry must be found: with none, the deployment would not
# have the admin the caller asked for and would not say so.
substitute_bootstrap_admin() {
  python3 -I -c '
import os
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
entry = re.compile(r"(- name: APPMUX_BOOTSTRAP_ADMINS\n[ \t]+value: )\S+")
text = path.read_text(encoding="utf-8")
replaced, count = entry.subn(
    lambda match: match.group(1) + os.environ["APPMUX_DEPLOY_BOOTSTRAP_ADMIN"], text
)
if count != 1:
    sys.exit(1)
path.write_text(replaced, encoding="utf-8")
' "$1"
}

# One JSON document on stdin -> its deployment id, then its state and message,
# one per line (an absent field is an empty line).
read_deployment() {
  python3 -I -c '
import json
import sys

document = json.load(sys.stdin)
status = document.get("status") or {}
print(document.get("deployment_id") or "")
print(status.get("state") or "")
print(str(status.get("message") or "").replace("\n", " "))
'
}

repo_root=${APPMUX_REPO_ROOT:-$(dirname -- "$script_dir")}
build_dir=${APPMUX_BUILD_DIR:-$repo_root/build}

# The UI first: assemble-deploy.sh copies web/out as it finds it, and a failed build must
# stop here, before a folder with an old or missing UI is assembled and uploaded.
"$repo_root/web/build.sh"

"$script_dir/assemble-deploy.sh"
deploy_dir="$build_dir/deploy"
[[ -d "$deploy_dir" ]] || die "assemble-deploy.sh left no deployment folder at build/deploy"

# The schema before the upload: an App started on an empty registry stops at "starting", and a
# failed load must not leave a half-deployed App behind. It is loaded from the folder that is
# about to be uploaded, so the schema and the code in it belong together.
if [[ -n "${APPMUX_LAKEBASE_ENDPOINT:-}" ]]; then
  printf 'loading the registry schema with your own Lakebase login\n'
  schema_status=0
  "$script_dir/load-schema.sh" "$deploy_dir/migrations" || schema_status=$?
  case "$schema_status" in
    0) ;;
    3) printf 'the registry schema is already loaded; nothing was changed\n' ;;
    *) die "the registry schema was not loaded (load-schema.sh exited $schema_status); nothing was uploaded" ;;
  esac
else
  printf 'registry schema step skipped: APPMUX_LAKEBASE_ENDPOINT is not set\n'
fi

work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT

# Run the CLI with the profile. Standard output is the caller's; standard error
# is masked before it is shown.
call_cli() {
  local status=0
  databricks "$@" --profile "$APPMUX_DATABRICKS_PROFILE" 2> "$work_dir/cli-error.txt" || status=$?
  mask_identifiers < "$work_dir/cli-error.txt" >&2
  return "$status"
}

# databricks sync honours .gitignore files and this repository ignores build/, so sync a
# copy that lives outside the repository instead of depending on how that interacts.
sync_source="$work_dir/deploy"
# Turn the APPMUX_CONTENT_STORE_FOLDER entry of the file given as the argument (the commented
# template, or an entry already switched on) into an active entry whose value is
# $APPMUX_DEPLOY_CONTENT_STORE_FOLDER. Exactly one must be found, for the reason given above.
substitute_content_store_folder() {
  python3 -I -c '
import os
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
entry = re.compile(
    r"^([ \t]*)(?:# ?)?- name: APPMUX_CONTENT_STORE_FOLDER\n[ \t]*#?[ \t]*value:[^\n]*", re.MULTILINE
)
text = path.read_text(encoding="utf-8")
folder = os.environ["APPMUX_DEPLOY_CONTENT_STORE_FOLDER"]
replaced, count = entry.subn(
    lambda match: (
        f"{match.group(1)}- name: APPMUX_CONTENT_STORE_FOLDER\n{match.group(1)}  value: {folder}"
    ),
    text,
)
if count != 1:
    sys.exit(1)
path.write_text(replaced, encoding="utf-8")
' "$1"
}

cp -a -- "$deploy_dir" "$sync_source"

if [[ -n "${APPMUX_DEPLOY_OVERLAY:-}" ]]; then
  cp -a -- "$APPMUX_DEPLOY_OVERLAY"/. "$sync_source"/
  printf 'overlay laid over the synced copy (%d files)\n' \
    "$(find "$APPMUX_DEPLOY_OVERLAY" -type f | wc -l)"
fi

if [[ -n "${APPMUX_DEPLOY_BOOTSTRAP_ADMIN:-}" ]]; then
  substitute_bootstrap_admin "$sync_source/app.yaml" \
    || die "app.yaml has no single APPMUX_BOOTSTRAP_ADMINS entry to put the bootstrap admin in"
  printf 'bootstrap admin set in the synced copy of app.yaml (the value is not shown)\n'
fi

if [[ -n "${APPMUX_DEPLOY_CONTENT_STORE_FOLDER:-}" ]]; then
  substitute_content_store_folder "$sync_source/app.yaml" \
    || die "app.yaml has no single APPMUX_CONTENT_STORE_FOLDER entry to put the store folder in"
  printf 'content store folder set in the synced copy of app.yaml (the value is not shown)\n'
fi

printf 'syncing the deployment folder to the workspace folder\n'
if ! call_cli sync --full "$sync_source" "$APPMUX_WORKSPACE_FOLDER" | mask_identifiers; then
  die "databricks sync failed"
fi

printf 'deploying %s\n' "$app_name"
deploy_output=$(call_cli apps deploy "$app_name" \
  --source-code-path "$APPMUX_WORKSPACE_FOLDER" --no-wait -o json) \
  || die "databricks apps deploy failed"
{
  IFS= read -r deployment_id
  IFS= read -r _
  IFS= read -r _
} < <(printf '%s' "$deploy_output" | read_deployment) \
  || die "the deploy output was not the JSON the CLI usually prints"
[[ -n "$deployment_id" ]] || die "the deploy output named no deployment"

for ((attempt = 1; attempt <= poll_attempts; attempt++)); do
  status_output=$(call_cli apps get-deployment "$app_name" "$deployment_id" -o json) \
    || die "databricks apps get-deployment failed"
  {
    IFS= read -r _
    IFS= read -r state
    IFS= read -r message
  } < <(printf '%s' "$status_output" | read_deployment) \
    || die "the deployment status was not the JSON the CLI usually prints"
  printf '[%d/%d] deployment state: %s\n' "$attempt" "$poll_attempts" "${state:-unknown}"
  case "$state" in
    SUCCEEDED)
      printf 'deployment succeeded\n'
      exit 0
      ;;
    FAILED | CANCELLED)
      [[ -z "$message" ]] || printf 'platform message: %s\n' "$message" | mask_identifiers >&2
      die "deployment $state; read the build log with scripts/fetch-logs.sh"
      ;;
  esac
  sleep "$poll_interval_s"
done
die "the deployment was still not final after $poll_attempts checks; read the log with scripts/fetch-logs.sh"
