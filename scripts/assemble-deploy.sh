#!/usr/bin/env bash
# Build the deployment folder build/deploy/ from an allow-list, check the tree
# it built, and optionally pack it as build/appmux-<version>.zip with a
# .sha256 beside it. This is the one place that builds the folder: a developer
# deploys it through scripts/sync-deploy.sh, and the release workflow packs the
# same folder, so a laptop deployment and the release zip do not differ
# (ADR 0015).
#
# Usage: scripts/assemble-deploy.sh [--zip]
#
# Environment (both optional; the unit test points them at a fake tree):
#   APPMUX_REPO_ROOT  repository root. Default: the folder above scripts/.
#   APPMUX_BUILD_DIR  the generated, git-ignored folder. Default: $APPMUX_REPO_ROOT/build.
#
# The allow-list (nothing else is copied):
#   app.yaml, LICENSE                       from the repository root
#   appmux/                                 from src/appmux, without __pycache__
#   web/out/                                from web/out (a warning when absent; the zip refuses)
#   requirements.txt, wheels/               from the build folder (scripts/vendor-wheels.sh)
#   bin/slirp4netns, licenses/slirp4netns/  from build/slirp4netns/ (scripts/fetch-slirp4netns.sh)
# scripts/ (including scripts/lib/) is never copied: the deployment folder holds the app, not
# the tooling.
#
# Every run first removes build/deploy/ and any build/appmux-*.zip with its .sha256 (and
# .partial), before the pre-checks, so a refused run leaves none of them behind.
#
# The built folder is refused, and removed, when it holds a pyproject.toml,
# uv.lock or package.json (the platform would install by itself), when
# requirements.txt does not start with --no-index, when it holds a symbolic
# link, or when bin/slirp4netns, COPYING, SOURCE.txt or the source archive of
# slirp4netns is missing or not usable.

set -euo pipefail

script_name=$(basename -- "$0")
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

print_usage() {
  printf 'usage: %s [--zip]\n' "$script_name"
  printf '  (no option)  build build/deploy/ and check it\n'
  printf '  --zip        also write build/appmux-<version>.zip and its .sha256\n'
}

die() {
  printf '%s: %s\n' "$script_name" "$*" >&2
  exit 1
}

make_zip=false
for argument in "$@"; do
  case "$argument" in
    --zip) make_zip=true ;;
    -h | --help)
      print_usage
      exit 0
      ;;
    *)
      printf "%s: unknown option '%s'\n" "$script_name" "$argument" >&2
      print_usage >&2
      exit 2
      ;;
  esac
done

repo_root=${APPMUX_REPO_ROOT:-$(dirname -- "$script_dir")}
[[ -d "$repo_root" ]] || die "the repository root '$repo_root' is not a folder"
repo_root=$(cd -- "$repo_root" && pwd -P)

build_dir=${APPMUX_BUILD_DIR:-$repo_root/build}
mkdir -p -- "$build_dir"
build_dir=$(cd -- "$build_dir" && pwd -P)
# The next step deletes "$build_dir/deploy"; never let it resolve to a root folder.
[[ "$build_dir" != "/" ]] || die "the build folder must not be the filesystem root"
deploy_dir="$build_dir/deploy"

# Fail closed on stale output: whatever an earlier run built is removed before any check
# runs, so a refused run leaves no folder, zip or .sha256 that could be uploaded or released
# as if this run had passed. Only the outputs of this script go; the inputs in build/
# (requirements.txt, wheels/, slirp4netns/) stay. A successful run rebuilds what it needs.
rm -rf -- "$deploy_dir"
rm -f -- "$build_dir"/appmux-*.zip "$build_dir"/appmux-*.zip.sha256 \
  "$build_dir"/appmux-*.zip.partial

vendor_hint="run scripts/vendor-wheels.sh first"

[[ -f "$repo_root/app.yaml" ]] || die "app.yaml is missing from the repository root"
[[ -f "$repo_root/LICENSE" ]] || die "LICENSE is missing from the repository root"
[[ -d "$repo_root/src/appmux" ]] || die "src/appmux is missing from the repository root"
[[ -f "$build_dir/requirements.txt" ]] || die "build/requirements.txt is missing; $vendor_hint"
[[ -d "$build_dir/wheels" ]] || die "build/wheels is missing; $vendor_hint"

have_ui_export=false
if [[ -d "$repo_root/web/out" ]]; then
  have_ui_export=true
elif [[ "$make_zip" == true ]]; then
  die "web/out is missing (the admin UI export built from web/); a release zip must hold the admin UI"
else
  printf '%s: warning: web/out is missing, so this deployment folder has no admin UI\n' \
    "$script_name" >&2
fi

zip_version=""
if [[ "$make_zip" == true ]]; then
  command -v python3 > /dev/null || die "python3 is needed to write the zip"
  zip_version=$(sed -n 's/^__version__ = "\(.*\)"[[:space:]]*$/\1/p' \
    "$repo_root/src/appmux/__init__.py" | head -n 1)
  # The version becomes part of a file name: allow no path separator and no leading dot.
  [[ "$zip_version" =~ ^[0-9][0-9A-Za-z.+_-]*$ ]] \
    || die "cannot read a usable __version__ from src/appmux/__init__.py (got '$zip_version')"
fi

# Copy a folder's contents; compiled caches never belong in a deployment.
copy_folder() {
  local source_folder=$1 destination_folder=$2
  mkdir -p -- "$destination_folder"
  tar -C "$source_folder" --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
    -cf - . | tar -C "$destination_folder" -xf -
}

# A folder that fails the checks below is removed again on exit, so a half-checked
# tree is never left behind to be uploaded.
mkdir -p -- "$deploy_dir"
assembly_checked=false
trap 'if [[ "$assembly_checked" != true ]]; then rm -rf -- "$deploy_dir"; fi' EXIT

install -m 0644 -- "$repo_root/app.yaml" "$deploy_dir/app.yaml"
install -m 0644 -- "$repo_root/LICENSE" "$deploy_dir/LICENSE"
install -m 0644 -- "$build_dir/requirements.txt" "$deploy_dir/requirements.txt"
copy_folder "$build_dir/wheels" "$deploy_dir/wheels"
copy_folder "$repo_root/src/appmux" "$deploy_dir/appmux"
if [[ "$have_ui_export" == true ]]; then
  copy_folder "$repo_root/web/out" "$deploy_dir/web/out"
fi

# slirp4netns: copy what is there; the checks below name what is missing.
slirp_build="$build_dir/slirp4netns"
if [[ -f "$slirp_build/bin/slirp4netns" ]]; then
  mkdir -p -- "$deploy_dir/bin"
  cp -p -- "$slirp_build/bin/slirp4netns" "$deploy_dir/bin/slirp4netns"
fi
if [[ -d "$slirp_build/licenses/slirp4netns" ]]; then
  copy_folder "$slirp_build/licenses/slirp4netns" "$deploy_dir/licenses/slirp4netns"
fi

# ---- check the assembled tree; list every problem at once ----
problems=()
slirp_hint="run scripts/fetch-slirp4netns.sh first (ADR 0015)"

while IFS= read -r found; do
  problems+=("${found#"$deploy_dir"/} must not be deployed: the platform installs by itself from a pyproject.toml, uv.lock or package.json")
done < <(find "$deploy_dir" \( -name pyproject.toml -o -name uv.lock -o -name package.json \) -print | sort)

while IFS= read -r found; do
  problems+=("${found#"$deploy_dir"/} is a symbolic link; the deployment folder holds regular files only")
done < <(find "$deploy_dir" -type l -print | sort)

first_requirements_line=""
IFS= read -r first_requirements_line < "$deploy_dir/requirements.txt" || true
if [[ ! "$first_requirements_line" =~ ^--no-index[[:space:]]*$ ]]; then
  problems+=("requirements.txt must start with --no-index (the workspace index is blocked); $vendor_hint")
fi

slirp_binary="$deploy_dir/bin/slirp4netns"
if [[ ! -f "$slirp_binary" ]]; then
  problems+=("bin/slirp4netns is missing; $slirp_hint")
elif [[ ! -x "$slirp_binary" ]]; then
  problems+=("bin/slirp4netns is not executable; $slirp_hint")
fi

slirp_licence_dir="$deploy_dir/licenses/slirp4netns"
for required_file in COPYING SOURCE.txt LICENSE-LGPL-2.1; do
  if [[ ! -f "$slirp_licence_dir/$required_file" ]]; then
    problems+=("licenses/slirp4netns/$required_file is missing; $slirp_hint")
  fi
done

shopt -s nullglob
source_archives=(
  "$slirp_licence_dir"/*.tar.gz "$slirp_licence_dir"/*.tgz "$slirp_licence_dir"/*.tar.xz
  "$slirp_licence_dir"/*.tar.bz2 "$slirp_licence_dir"/*.tar "$slirp_licence_dir"/*.zip
)
shopt -u nullglob
if ((${#source_archives[@]} == 0)); then
  problems+=("licenses/slirp4netns/ holds no source archive (*.tar.gz, *.tar.xz, *.zip, ...); $slirp_hint")
fi

if ((${#problems[@]} > 0)); then
  printf '%s: refusing the deployment folder:\n' "$script_name" >&2
  for problem in "${problems[@]}"; do
    printf '  - %s\n' "$problem" >&2
  done
  exit 1
fi
assembly_checked=true

file_count=$(find "$deploy_dir" -type f | wc -l)
printf 'assembled %s (%d files)\n' "$deploy_dir" "$file_count"

# ---- the release zip ----
if [[ "$make_zip" == true ]]; then
  zip_path="$build_dir/appmux-$zip_version.zip"
  # Reproducible on purpose: sorted entries, a fixed timestamp, normalised modes, so the
  # same folder always gives the same .sha256. The unix mode is kept because
  # bin/slirp4netns must still be executable after the operator unpacks the zip.
  python3 -I - "$deploy_dir" "$zip_path" "appmux-$zip_version" << 'PYTHON'
import hashlib
import os
import stat
import sys
import zipfile
from pathlib import Path

deploy_dir, archive_path, top_folder = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)  # the earliest date a zip can hold

partial_path = archive_path.with_name(archive_path.name + ".partial")
with zipfile.ZipFile(partial_path, "w") as bundle:
    for path in sorted(deploy_dir.rglob("*")):
        if not path.is_file():
            continue
        entry = zipfile.ZipInfo(f"{top_folder}/{path.relative_to(deploy_dir).as_posix()}", FIXED_TIMESTAMP)
        is_executable = bool(path.stat().st_mode & stat.S_IXUSR)
        entry.external_attr = (stat.S_IFREG | (0o755 if is_executable else 0o644)) << 16
        entry.compress_type = zipfile.ZIP_DEFLATED
        bundle.writestr(entry, path.read_bytes())
os.replace(partial_path, archive_path)

digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
# The sha256sum format, so an operator can run `sha256sum -c` beside the zip.
Path(f"{archive_path}.sha256").write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
PYTHON
  printf 'wrote %s and %s.sha256\n' "$zip_path" "$zip_path"
fi
