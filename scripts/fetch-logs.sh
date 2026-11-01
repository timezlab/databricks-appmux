#!/usr/bin/env bash
# Show the logs of the appmux App (the build log and the app's own output) for a
# development workspace. Read-only.
#
# Usage: scripts/fetch-logs.sh [--tail-lines N] [--source APP|SYSTEM] [--search TEXT]
#   --tail-lines N     how many recent lines to show. Default: 200. 0 shows everything.
#   --source APP|SYSTEM  restrict the logs to the app's own output or to the platform's
#   --search TEXT      ask the log service for lines that match TEXT
#
# Environment (local, never committed; the script stops and names the variable
# if the profile is unset, before it calls the Databricks CLI):
#   APPMUX_DATABRICKS_PROFILE  Databricks CLI profile to use. Required, never defaulted.
#   APPMUX_APP_NAME            the App whose logs to show. Default: appmux.
#   APPMUX_WORKSPACE_FOLDER    optional; when set, that folder is masked too.
#
# The output passes through mask_identifiers (scripts/lib/mask-identifiers.sh, shared with
# sync-deploy.sh): URLs, host names, workspace and org ids, e-mail addresses, workspace
# paths and tokens are replaced before anything is printed, so the log can be pasted into a
# ticket. Read the raw log in the workspace when a masked value is the thing you need.

set -euo pipefail

script_name=$(basename -- "$0")
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

die() {
  printf '%s: %s\n' "$script_name" "$*" >&2
  exit 1
}

print_usage() {
  printf 'usage: %s [--tail-lines N] [--source APP|SYSTEM] [--search TEXT]\n' "$script_name"
}

# shellcheck source=lib/mask-identifiers.sh
source "$script_dir/lib/mask-identifiers.sh"

[[ -n "${APPMUX_DATABRICKS_PROFILE:-}" ]] \
  || die "APPMUX_DATABRICKS_PROFILE is not set; export it in your shell (it is local and is never committed)"

app_name=${APPMUX_APP_NAME:-appmux}
tail_lines=200
filter_options=()

while (($# > 0)); do
  case "$1" in
    --tail-lines)
      [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || die "--tail-lines needs a whole number"
      tail_lines=$2
      shift 2
      ;;
    --source)
      [[ $# -ge 2 && "$2" =~ ^(APP|SYSTEM)$ ]] || die "--source needs APP or SYSTEM"
      filter_options+=(--source "$2")
      shift 2
      ;;
    --search)
      [[ $# -ge 2 && -n "$2" ]] || die "--search needs a search text"
      filter_options+=(--search "$2")
      shift 2
      ;;
    -h | --help)
      print_usage
      exit 0
      ;;
    *)
      printf "%s: unknown option '%s'\n" "$script_name" "$1" >&2
      print_usage >&2
      exit 2
      ;;
  esac
done

command -v databricks > /dev/null || die "the Databricks CLI ('databricks') is not on PATH"

if ! databricks apps logs "$app_name" --tail-lines "$tail_lines" \
  ${filter_options[@]+"${filter_options[@]}"} \
  --profile "$APPMUX_DATABRICKS_PROFILE" 2>&1 | mask_identifiers; then
  die "databricks apps logs failed"
fi
