# shellcheck shell=bash
# Sourced (never run) by scripts/run-remote-tests.sh and scripts/run-container-tests.sh. It defines
# sync_fixtures, the one place that knows how tests/fixtures reaches the workspace. The caller
# defines die and call_cli (the Databricks CLI with its profile, standard error masked) and sources
# mask-identifiers.sh first.
#
#   sync_fixtures REPO_ROOT STAGE_DIR WORKSPACE_FOLDER
#
# The fixtures go up from a copy at STAGE_DIR (it must not exist yet): the wheels of the
# offline-install ones and of pkg_env_writer (it needs an overlay to probe) are built into the
# copy (they are never committed), bytecode is left out, and databricks sync does not read this
# repository's .gitignore.

sync_fixtures() {
  local repo_root=$1
  local stage_dir=$2
  local workspace_folder=$3
  local fixture

  cp -a -- "$repo_root/tests/fixtures" "$stage_dir"
  find "$stage_dir" -name __pycache__ -type d -prune -exec rm -rf -- {} +
  for fixture in wheels_only wheel_reuse env_covered newer_wheel pkg_env_writer; do
    python3 -I "$stage_dir/$fixture/make_wheels.py" "$stage_dir/$fixture/wheels" > /dev/null
  done

  printf 'syncing the fixtures to the workspace\n'
  call_cli sync --full "$stage_dir" "$workspace_folder" | mask_identifiers \
    || die "databricks sync of the fixtures failed"
}
