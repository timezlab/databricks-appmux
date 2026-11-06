#!/usr/bin/env bash
# Load the appmux registry schema into the Lakebase database, with the caller's own login.
# scripts/sync-deploy.sh runs this before it uploads anything; run it alone to load the schema
# without deploying.
#
# Usage: scripts/load-schema.sh [MIGRATIONS_FOLDER]
#
# MIGRATIONS_FOLDER defaults to the migrations/ folder of the repository.
#
# Why the caller's login and not the App's: the appmux service principal must not own the registry
# tables or hold DDL rights, or a child holding the shared service principal's token could rewrite
# `apps` (ADR 0004, data model 6). So this runs on the deployer's side, as the deployer's own
# Postgres role, and the App only ever sees tables that already exist.
#
# Environment (local, never committed; the script stops and names the first one that is unset,
# before it calls the Databricks CLI):
#   APPMUX_DATABRICKS_PROFILE  Databricks CLI profile to use. Required, never defaulted.
#   APPMUX_LAKEBASE_ENDPOINT   the Lakebase endpoint's resource name,
#                              projects/<project>/branches/<branch>/endpoints/<endpoint>
# Optional:
#   APPMUX_REPO_ROOT           repository root. Default: the folder above scripts/.
#
# What it does: asks the CLI for the endpoint's host, the caller's user name (their Postgres role)
# and a database credential, then runs `python -m appmux.registry.load_schema` with the three in
# PGHOST, PGUSER and PGPASSWORD. They travel in the environment of that one process: a token on a
# command line shows in the process list and in the shell history.
#
# Exit code: that of the loader. 0 = loaded now, 3 = already loaded (nothing changed; not a
# failure), 2 = refused before anything ran (a role or the folder is missing), 1 = failed. Any
# failure before the loader runs is 1. The output passes through mask_identifiers
# (scripts/lib/mask-identifiers.sh).
#
# An operator who holds only the release zip has no scripts/ folder: they set PGHOST, PGUSER and
# PGPASSWORD themselves and run `python -m appmux.registry.load_schema` from the unpacked folder
# (design 13.1).

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
require_variable APPMUX_LAKEBASE_ENDPOINT

(($# <= 1)) || die "usage: $script_name [MIGRATIONS_FOLDER]"

repo_root=${APPMUX_REPO_ROOT:-$(dirname -- "$script_dir")}
migrations_folder=${1:-$repo_root/migrations}

command -v databricks > /dev/null || die "the Databricks CLI ('databricks') is not on PATH"
command -v uv > /dev/null || die "uv is not on PATH; it runs the loader with the project's environment"
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

# One JSON document on stdin -> the value at the key path given as the arguments, or nothing.
read_json_value() {
  python3 -I -c '
import json
import sys

value = json.load(sys.stdin)
for key in sys.argv[1:]:
    value = value.get(key) if isinstance(value, dict) else None
print(value if isinstance(value, str) else "")
' "$@"
}

endpoint_json=$(call_cli postgres get-endpoint "$APPMUX_LAKEBASE_ENDPOINT" -o json) \
  || die "could not read the Lakebase endpoint; check APPMUX_LAKEBASE_ENDPOINT and the profile"
host=$(printf '%s' "$endpoint_json" | read_json_value status hosts host) \
  || die "the endpoint answer was not the JSON the CLI usually prints"
[[ -n "$host" ]] || die "the Lakebase endpoint names no host; is the endpoint running?"

user_json=$(call_cli current-user me -o json) \
  || die "could not read the current user; check the profile"
user=$(printf '%s' "$user_json" | read_json_value userName) \
  || die "the current-user answer was not the JSON the CLI usually prints"
[[ -n "$user" ]] || die "the current user has no user name"

credential_json=$(call_cli postgres generate-database-credential "$APPMUX_LAKEBASE_ENDPOINT" -o json) \
  || die "could not mint a database credential; check that you may connect to the endpoint"
token=$(printf '%s' "$credential_json" | read_json_value token) \
  || die "the credential answer was not the JSON the CLI usually prints"
[[ -n "$token" ]] || die "the credential answer holds no token"

loader_status=0
PGHOST="$host" PGUSER="$user" PGPASSWORD="$token" \
  uv run --project "$repo_root" python -m appmux.registry.load_schema \
  --migrations "$migrations_folder" \
  > "$work_dir/loader-output.txt" 2> "$work_dir/loader-error.txt" || loader_status=$?
mask_identifiers < "$work_dir/loader-output.txt"
mask_identifiers < "$work_dir/loader-error.txt" >&2
exit "$loader_status"
