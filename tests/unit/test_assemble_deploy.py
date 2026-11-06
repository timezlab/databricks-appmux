"""scripts/assemble-deploy.sh: the one place that builds the deployment folder.

The script copies an allow-list into ``build/deploy/`` and then checks the tree
it built. These refusals are a safety guard (a ``pyproject.toml``, ``uv.lock``
or ``package.json`` in the deployed folder makes the platform install by itself;
a requirements file without ``--no-index`` reaches for a blocked index; a
missing ``slirp4netns`` or licence file breaks ADR 0015), so each one is proved
on a fake tree in a temporary directory. Nothing here touches the network, the
real ``build/`` folder or a Databricks workspace.

A refused run must also leave no earlier output behind (``build/deploy``, the zip and its
``.sha256``): each refusal test plants those first and asserts they are gone.

The same file proves that ``sync-deploy.sh`` and ``fetch-logs.sh`` stop on an
unset local variable before they call the Databricks CLI (a stub stands in for
the CLI and must never be called), that ``sync-deploy.sh`` hands the CLI a copy outside the
repository, that both scripts mask tokens and identifiers through the one helper in
``scripts/lib/mask-identifiers.sh`` (fake values only), and that every script parses.
``scripts/vendor-wheels.sh`` has its own file, ``test_vendor_wheels.py``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
ASSEMBLE = SCRIPTS_DIR / "assemble-deploy.sh"
SYNC = SCRIPTS_DIR / "sync-deploy.sh"
FETCH_LOGS = SCRIPTS_DIR / "fetch-logs.sh"
LOAD_SCHEMA = SCRIPTS_DIR / "load-schema.sh"
VENDOR_WHEELS = SCRIPTS_DIR / "vendor-wheels.sh"
RUN_CONTAINER_TESTS = SCRIPTS_DIR / "run-container-tests.sh"

FAKE_VERSION = "1.2.3"
SLIRP_RELATIVE = "bin/slirp4netns"
LICENCE_FOLDER = "licenses/slirp4netns"

# Everything the deployment folder may hold, for the fake tree below.
EXPECTED_DEPLOY_FILES = frozenset(
    {
        "app.yaml",
        "LICENSE",
        "requirements.txt",
        "wheels/psycopg-3.3.6-py3-none-any.whl",
        "appmux/__init__.py",
        "appmux/__main__.py",
        "appmux/edge/__init__.py",
        "migrations/0001_baseline.sql",
        "web/out/index.html",
        "web/out/_next/app.js",
        "bin/slirp4netns",
        f"{LICENCE_FOLDER}/COPYING",
        f"{LICENCE_FOLDER}/SOURCE.txt",
        f"{LICENCE_FOLDER}/LICENSE-LGPL-2.1",
        f"{LICENCE_FOLDER}/slirp4netns-v1.3.6.tar.gz",
    }
)

VALID_REQUIREMENTS = (
    "--no-index\n--find-links ./wheels\npsycopg==3.3.6 \\\n    --hash=sha256:" + "0" * 64 + "\n"
)


@dataclass(frozen=True)
class FakeTree:
    """A repository root and a build folder in a temporary directory."""

    root: Path
    build: Path

    @property
    def deploy(self) -> Path:
        return self.build / "deploy"

    @property
    def slirp_source(self) -> Path:
        return self.build / "slirp4netns"


def write(path: Path, text: str = "x\n", *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700 if executable else 0o600)


@pytest.fixture
def tree(tmp_path: Path) -> FakeTree:
    root = tmp_path / "repo"
    build = root / "build"
    # What the repository root holds that must NOT reach the deployment folder.
    write(root / "pyproject.toml")
    write(root / "uv.lock")
    write(root / "README.md")
    write(root / "web" / "package.json", "{}\n")
    write(root / "web" / "build.sh", "#!/usr/bin/env bash\nexit 0\n", executable=True)
    write(root / "tests" / "unit" / "test_x.py")
    write(root / "scripts" / "assemble-deploy.sh", executable=True)
    write(root / "scripts" / "lib" / "mask-identifiers.sh")
    # What the allow-list copies.
    write(root / "app.yaml", 'command: ["python", "-m", "appmux"]\n')
    write(root / "LICENSE", "Apache License\n")
    write(root / "migrations" / "0001_baseline.sql", "CREATE TABLE apps ();\n")
    package = root / "src" / "appmux"
    write(package / "__init__.py", f'"""Fake."""\n\n__version__ = "{FAKE_VERSION}"\n')
    write(package / "__main__.py")
    write(package / "edge" / "__init__.py")
    write(package / "__pycache__" / "__init__.cpython-311.pyc")
    write(package / "edge" / "__pycache__" / "gate.cpython-311.pyc")
    write(root / "web" / "out" / "index.html", "<html></html>\n")
    write(root / "web" / "out" / "_next" / "app.js", "// built\n")
    write(build / "requirements.txt", VALID_REQUIREMENTS)
    write(build / "wheels" / "psycopg-3.3.6-py3-none-any.whl", "wheel bytes\n")
    slirp = build / "slirp4netns"
    write(slirp / SLIRP_RELATIVE, "binary\n", executable=True)
    write(slirp / LICENCE_FOLDER / "COPYING", "GPL text\n")
    write(slirp / LICENCE_FOLDER / "SOURCE.txt", "source offer\n")
    write(slirp / LICENCE_FOLDER / "LICENSE-LGPL-2.1", "LGPL text\n")
    write(slirp / LICENCE_FOLDER / "slirp4netns-v1.3.6.tar.gz", "archive\n")
    return FakeTree(root=root, build=build)


def run_script(
    script: Path, environment: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def assemble(fake: FakeTree, *args: str) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": os.environ["PATH"],
        "APPMUX_REPO_ROOT": str(fake.root),
        "APPMUX_BUILD_DIR": str(fake.build),
    }
    return run_script(ASSEMBLE, environment, *args)


def files_under(folder: Path) -> set[str]:
    return {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}


def plant_stale_outputs(fake: FakeTree) -> list[Path]:
    """Outputs of an earlier run: a deployment folder and a zip (another version) with its hash."""
    stale = [
        fake.deploy / "stale-module.py",
        fake.build / "appmux-0.0.1.zip",
        fake.build / "appmux-0.0.1.zip.sha256",
    ]
    for path in stale:
        write(path)
    return stale


def refused_assembly(fake: FakeTree, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a script that must refuse, after earlier outputs exist; none may survive.

    A refused run that leaves the previous build/deploy, zip or .sha256 behind would let
    sync-deploy.sh or a release upload an old tree that nobody checked this time.
    """
    stale = plant_stale_outputs(fake)

    result = assemble(fake, *args)

    assert result.returncode != 0, result.stdout
    survivors = [path.name for path in stale if path.exists()]
    assert survivors == [], f"a refused run left earlier output behind: {survivors}"
    assert not fake.deploy.exists()
    return result


def test_assembles_exactly_the_allow_list(tree: FakeTree) -> None:
    result = assemble(tree)

    assert result.returncode == 0, result.stderr
    assert files_under(tree.deploy) == EXPECTED_DEPLOY_FILES
    assert (tree.deploy / "app.yaml").read_text() == (tree.root / "app.yaml").read_text()
    assert os.access(tree.deploy / SLIRP_RELATIVE, os.X_OK)


def test_assembly_replaces_the_previous_deployment_folder(tree: FakeTree) -> None:
    write(tree.deploy / "stale-module.py")

    result = assemble(tree)

    assert result.returncode == 0, result.stderr
    assert "stale-module.py" not in files_under(tree.deploy)


def test_assembly_removes_the_zip_and_hash_of_an_earlier_run(tree: FakeTree) -> None:
    stale = plant_stale_outputs(tree)

    result = assemble(tree)

    assert result.returncode == 0, result.stderr
    assert [path.name for path in stale if path.exists()] == []


@pytest.mark.parametrize(
    ("missing", "named_in_message"),
    [
        pytest.param("app.yaml", "app.yaml", id="app.yaml"),
        pytest.param("LICENSE", "LICENSE", id="licence"),
        pytest.param("src/appmux", "src/appmux", id="package"),
        pytest.param("migrations", "migrations", id="migrations"),
    ],
)
def test_a_missing_repository_file_is_refused_without_leaving_earlier_output(
    tree: FakeTree, missing: str, named_in_message: str
) -> None:
    target = tree.root / missing
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

    result = refused_assembly(tree)

    assert named_in_message in result.stderr


def test_a_refused_run_removes_only_the_outputs_it_would_have_rebuilt(tree: FakeTree) -> None:
    unrelated = tree.build / "notes.zip"
    write(unrelated)
    (tree.root / "LICENSE").unlink()

    refused_assembly(tree)

    assert unrelated.exists()
    assert (tree.build / "requirements.txt").is_file()
    assert (tree.build / "wheels").is_dir()
    assert (tree.slirp_source / SLIRP_RELATIVE).is_file()


def test_the_scripts_folder_is_not_copied_into_the_deployment_folder(tree: FakeTree) -> None:
    result = assemble(tree, "--zip")

    assert result.returncode == 0, result.stderr
    assert not (tree.deploy / "scripts").exists()
    assert [name for name in files_under(tree.deploy) if "mask-identifiers" in name] == []
    with zipfile.ZipFile(tree.build / f"appmux-{FAKE_VERSION}.zip") as bundle:
        assert [name for name in bundle.namelist() if "scripts/" in name] == []


def test_a_plain_assembly_writes_no_zip(tree: FakeTree) -> None:
    assemble(tree)

    assert list(tree.build.glob("*.zip")) == []


@pytest.mark.parametrize(
    "forbidden_path",
    [
        pytest.param("src/appmux/pyproject.toml", id="pyproject.toml-in-the-package"),
        pytest.param("build/wheels/uv.lock", id="uv.lock-in-the-wheel-folder"),
        pytest.param("web/out/package.json", id="package.json-in-the-web-export"),
    ],
)
def test_refuses_a_tree_that_holds_a_file_the_platform_would_install_from(
    tree: FakeTree, forbidden_path: str
) -> None:
    write(tree.root / forbidden_path)

    result = refused_assembly(tree)

    assert Path(forbidden_path).name in result.stderr


@pytest.mark.parametrize(
    "requirements_text",
    [
        pytest.param("psycopg==3.3.6\n--no-index\n", id="requirement-first"),
        pytest.param("--find-links ./wheels\n--no-index\n", id="find-links-first"),
        pytest.param("", id="empty"),
    ],
)
def test_refuses_requirements_that_do_not_start_with_no_index(
    tree: FakeTree, requirements_text: str
) -> None:
    write(tree.build / "requirements.txt", requirements_text)

    result = refused_assembly(tree)

    assert "--no-index" in result.stderr


def test_a_missing_requirements_file_points_at_vendor_wheels(tree: FakeTree) -> None:
    (tree.build / "requirements.txt").unlink()

    result = refused_assembly(tree)

    assert "vendor-wheels.sh" in result.stderr


def test_a_missing_wheel_folder_points_at_vendor_wheels(tree: FakeTree) -> None:
    shutil.rmtree(tree.build / "wheels")

    result = refused_assembly(tree)

    assert "vendor-wheels.sh" in result.stderr


def assert_slirp_refusal(tree: FakeTree, named_in_message: str) -> None:
    result = refused_assembly(tree)

    assert named_in_message in result.stderr
    assert "fetch-slirp4netns.sh" in result.stderr


@pytest.mark.parametrize(
    ("missing_file", "named_in_message"),
    [
        pytest.param(SLIRP_RELATIVE, SLIRP_RELATIVE, id="binary"),
        pytest.param(f"{LICENCE_FOLDER}/COPYING", "COPYING", id="copying"),
        pytest.param(f"{LICENCE_FOLDER}/SOURCE.txt", "SOURCE.txt", id="source-txt"),
        pytest.param(f"{LICENCE_FOLDER}/LICENSE-LGPL-2.1", "LICENSE-LGPL-2.1", id="lgpl-text"),
        pytest.param(
            f"{LICENCE_FOLDER}/slirp4netns-v1.3.6.tar.gz", "source archive", id="source-archive"
        ),
    ],
)
def test_refuses_a_deployment_that_lacks_a_slirp4netns_file(
    tree: FakeTree, missing_file: str, named_in_message: str
) -> None:
    (tree.slirp_source / missing_file).unlink()

    assert_slirp_refusal(tree, named_in_message)


def test_refuses_a_slirp4netns_binary_that_is_not_executable(tree: FakeTree) -> None:
    (tree.slirp_source / SLIRP_RELATIVE).chmod(0o600)

    assert_slirp_refusal(tree, "not executable")


def test_a_missing_slirp4netns_build_reports_every_missing_piece_at_once(tree: FakeTree) -> None:
    shutil.rmtree(tree.slirp_source)

    result = refused_assembly(tree)

    for piece in (SLIRP_RELATIVE, "COPYING", "SOURCE.txt", "LICENSE-LGPL-2.1", "source archive"):
        assert piece in result.stderr
    assert "fetch-slirp4netns.sh" in result.stderr


def test_refuses_a_symbolic_link_in_the_package(tree: FakeTree) -> None:
    outside = tree.root.parent / "outside-secret.txt"
    write(outside)
    (tree.root / "src" / "appmux" / "link.py").symlink_to(outside)

    result = refused_assembly(tree)

    assert "symbolic link" in result.stderr


def test_a_missing_ui_export_is_a_warning_for_a_plain_assembly(tree: FakeTree) -> None:
    shutil.rmtree(tree.root / "web" / "out")

    result = assemble(tree)

    assert result.returncode == 0, result.stderr
    assert "web/out" in result.stderr
    assert not (tree.deploy / "web").exists()


def test_the_release_zip_refuses_a_missing_ui_export(tree: FakeTree) -> None:
    shutil.rmtree(tree.root / "web" / "out")

    result = refused_assembly(tree, "--zip")

    assert "web/out" in result.stderr


def test_zip_writes_the_archive_and_its_sha256(tree: FakeTree) -> None:
    result = assemble(tree, "--zip")

    assert result.returncode == 0, result.stderr
    archive = tree.build / f"appmux-{FAKE_VERSION}.zip"
    checksum_file = tree.build / f"appmux-{FAKE_VERSION}.zip.sha256"
    expected_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert checksum_file.read_text() == f"{expected_digest}  {archive.name}\n"

    top_folder = f"appmux-{FAKE_VERSION}/"
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        binary_mode = stat.S_IMODE(bundle.getinfo(top_folder + SLIRP_RELATIVE).external_attr >> 16)
        app_yaml_mode = stat.S_IMODE(bundle.getinfo(top_folder + "app.yaml").external_attr >> 16)
    assert names == {top_folder + name for name in EXPECTED_DEPLOY_FILES}
    assert binary_mode & stat.S_IXUSR, "the unpacked binary must stay executable"
    assert not app_yaml_mode & stat.S_IXUSR


def test_zip_is_reproducible(tree: FakeTree) -> None:
    assemble(tree, "--zip")
    first = (tree.build / f"appmux-{FAKE_VERSION}.zip").read_bytes()
    write(tree.root / "src" / "appmux" / "__main__.py")  # new mtime, same content

    assemble(tree, "--zip")

    assert (tree.build / f"appmux-{FAKE_VERSION}.zip").read_bytes() == first


@pytest.mark.parametrize(
    "init_text",
    [
        pytest.param('"""No version here."""\n', id="no-version"),
        pytest.param('__version__ = "../evil"\n', id="path-in-version"),
        pytest.param('__version__ = ""\n', id="empty-version"),
    ],
)
def test_zip_refuses_a_version_it_cannot_use_in_a_file_name(tree: FakeTree, init_text: str) -> None:
    write(tree.root / "src" / "appmux" / "__init__.py", init_text)

    result = refused_assembly(tree, "--zip")

    assert "__version__" in result.stderr


def test_an_unknown_option_is_a_usage_error(tree: FakeTree) -> None:
    result = assemble(tree, "--tarball")

    assert result.returncode == 2
    assert "--zip" in result.stderr
    assert not tree.deploy.exists()


STUB_CLI_SCRIPT = """#!/bin/bash
# Stands in for the Databricks CLI: records every call, prints canned output.
printf '%s\\n' "$*" >> "$STUB_CALLS"
case "$1" in
  sync)
    (cd "$3" && find . -type f | sort) > "$STUB_SYNC_LISTING"
    if [ -n "${STUB_SYNC_APP_YAML:-}" ]; then cp -- "$3/app.yaml" "$STUB_SYNC_APP_YAML"; fi
    echo "Action: PUT: app.yaml"
    echo "Initial Sync Complete for $4"
    if [ -n "${STUB_EXTRA_OUTPUT:-}" ]; then
      printf '%s\\n' "$STUB_EXTRA_OUTPUT"
      printf '%s\\n' "$STUB_EXTRA_OUTPUT" >&2
    fi
    ;;
  postgres)
    case "$2" in
      get-endpoint) echo '{"status": {"hosts": {"host": "endpoint.database.example.test"}}}' ;;
      generate-database-credential)
        if [ "${STUB_CREDENTIAL_FAILS:-}" = "1" ]; then echo "credential refused" >&2; exit 1; fi
        printf '{"token": "%s", "expire_time": "2030-01-01T00:00:00Z"}\n' "$STUB_DB_TOKEN"
        ;;
    esac
    ;;
  current-user) echo '{"userName": "deployer@example.test"}' ;;
  apps)
    case "$2" in
      deploy) echo '{"deployment_id": "deployment-1", "status": {"state": "IN_PROGRESS"}}' ;;
      get-deployment)
        printf '{"status": {"state": "%s", "message": "%s"}}\\n' "$STUB_STATE" "$STUB_MESSAGE"
        ;;
      logs) cat "$STUB_LOG_TEXT" ;;
    esac
    ;;
esac
"""

# Stands in for ``uv``: records the loader's command line and whether the login reached it through
# the environment, prints canned output and exits with the code the test asks for.
STUB_UV_SCRIPT = """#!/bin/bash
printf 'uv %s\\n' "$*" >> "$STUB_CALLS"
if [ "${PGPASSWORD:-}" = "$STUB_DB_TOKEN" ] && [ -n "${PGHOST:-}" ] && [ -n "${PGUSER:-}" ]; then
  printf 'login-in-environment\\n' >> "$STUB_CALLS"
fi
printf '%s\\n' "${STUB_LOAD_OUTPUT:-loaded the registry: applied 3 migration file(s)}"
exit "${STUB_LOAD_STATUS:-0}"
"""

WORKSPACE_FOLDER = "/Workspace/Users/user@example.com/appmux-deploy"
PROFILE = "example-profile"
LAKEBASE_ENDPOINT = "projects/example/branches/main/endpoints/primary"
# Fake, built at run time so no token-shaped literal sits in the source.
FAKE_DB_TOKEN = "dbtok" + "k3" * 10
# Fake secret-shaped text, built at run time so no such literal sits in the source.
FAKE_OPAQUE_VALUE = "Zq7" + "x" * 15 + "9"
FAKE_DOSE_TOKEN = "dose" + "0f" * 16


@dataclass(frozen=True)
class StubCli:
    """A ``databricks`` stub placed first on PATH; ``calls`` exists only if it was called."""

    environment: dict[str, str]
    calls: Path
    sync_listing: Path

    def call_lines(self) -> list[str]:
        return self.calls.read_text().splitlines()


@pytest.fixture
def stub_cli(tmp_path: Path) -> StubCli:
    stub_dir = tmp_path / "stub-bin"
    write(stub_dir / "databricks", STUB_CLI_SCRIPT, executable=True)
    write(stub_dir / "uv", STUB_UV_SCRIPT, executable=True)
    calls = tmp_path / "databricks-calls.txt"
    listing = tmp_path / "sync-listing.txt"
    log_text = tmp_path / "log-text.txt"
    write(log_text, "")
    environment = {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "STUB_CALLS": str(calls),
        "STUB_SYNC_LISTING": str(listing),
        "STUB_LOG_TEXT": str(log_text),
        "STUB_DB_TOKEN": FAKE_DB_TOKEN,
        "STUB_STATE": "SUCCEEDED",
        "STUB_MESSAGE": "",
    }
    return StubCli(environment=environment, calls=calls, sync_listing=listing)


def sync_environment(fake: FakeTree, stub: StubCli, **overrides: str) -> dict[str, str]:
    return {
        **stub.environment,
        "APPMUX_REPO_ROOT": str(fake.root),
        "APPMUX_BUILD_DIR": str(fake.build),
        "APPMUX_WORKSPACE_FOLDER": WORKSPACE_FOLDER,
        "APPMUX_DATABRICKS_PROFILE": PROFILE,
        "APPMUX_POLL_INTERVAL_S": "0",
        "APPMUX_POLL_ATTEMPTS": "3",
        **overrides,
    }


@pytest.mark.parametrize(
    ("script", "unset_variable", "other_variables"),
    [
        pytest.param(
            SYNC,
            "APPMUX_WORKSPACE_FOLDER",
            {"APPMUX_DATABRICKS_PROFILE": PROFILE},
            id="sync-without-folder",
        ),
        pytest.param(
            SYNC,
            "APPMUX_DATABRICKS_PROFILE",
            {"APPMUX_WORKSPACE_FOLDER": WORKSPACE_FOLDER},
            id="sync-without-profile",
        ),
        pytest.param(FETCH_LOGS, "APPMUX_DATABRICKS_PROFILE", {}, id="logs-without-profile"),
    ],
)
def test_workspace_scripts_stop_on_an_unset_variable_before_calling_the_cli(
    stub_cli: StubCli, script: Path, unset_variable: str, other_variables: dict[str, str]
) -> None:
    result = run_script(script, {**stub_cli.environment, **other_variables})

    assert result.returncode != 0
    assert unset_variable in result.stderr
    assert not stub_cli.calls.exists(), "the CLI must not be called while a variable is unset"


def test_sync_deploy_refuses_a_workspace_folder_that_is_not_absolute(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    result = run_script(
        SYNC, sync_environment(tree, stub_cli, APPMUX_WORKSPACE_FOLDER="relative/folder")
    )

    assert result.returncode != 0
    assert "APPMUX_WORKSPACE_FOLDER" in result.stderr
    assert not stub_cli.calls.exists()


def test_sync_deploy_syncs_the_assembled_folder_deploys_and_waits_for_success(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    result = run_script(SYNC, sync_environment(tree, stub_cli))

    assert result.returncode == 0, result.stderr
    calls = stub_cli.call_lines()
    sync_call = calls[0].split()
    assert sync_call[:2] == ["sync", "--full"]
    assert sync_call[3:] == [WORKSPACE_FOLDER, "--profile", PROFILE]
    assert calls[1:] == [
        f"apps deploy appmux --source-code-path {WORKSPACE_FOLDER} --no-wait -o json "
        f"--profile {PROFILE}",
        f"apps get-deployment appmux deployment-1 -o json --profile {PROFILE}",
    ]
    synced = {line.removeprefix("./") for line in stub_cli.sync_listing.read_text().splitlines()}
    assert synced == EXPECTED_DEPLOY_FILES
    assert "deployment succeeded" in result.stdout


def test_sync_deploy_syncs_a_copy_outside_the_repository_not_build_deploy(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    # `databricks sync` honours .gitignore, and the real repository ignores build/, so syncing
    # build/deploy itself could upload nothing. The script must hand the CLI a copy that sits
    # outside the repository (and so outside every .gitignore of it).
    write(tree.root / ".gitignore", "build/\n")

    result = run_script(SYNC, sync_environment(tree, stub_cli))

    assert result.returncode == 0, result.stderr
    sync_source = Path(stub_cli.call_lines()[0].split()[2])
    assert sync_source.is_absolute()
    assert sync_source.resolve() != tree.deploy.resolve()
    assert not sync_source.resolve().is_relative_to(tree.root.resolve())
    assert not sync_source.resolve().is_relative_to(tree.build.resolve())
    synced = {line.removeprefix("./") for line in stub_cli.sync_listing.read_text().splitlines()}
    assert synced == EXPECTED_DEPLOY_FILES, "the copy must hold the whole assembled folder"
    assert not sync_source.exists(), "the temporary copy must be removed when the script ends"


def test_sync_deploy_prints_no_workspace_identifier(tree: FakeTree, stub_cli: StubCli) -> None:
    result = run_script(SYNC, sync_environment(tree, stub_cli))

    shown = result.stdout + result.stderr
    assert WORKSPACE_FOLDER not in shown
    assert "user@example.com" not in shown
    assert "<workspace-folder>" in shown


def test_sync_deploy_uses_the_app_name_from_the_environment(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    run_script(SYNC, sync_environment(tree, stub_cli, APPMUX_APP_NAME="appmux-dev"))

    assert any(line.startswith("apps deploy appmux-dev ") for line in stub_cli.call_lines())


BOOTSTRAP_PLACEHOLDER = "dana@example.com"
OWNER_ADDRESS = "owner.name+dev@example.org"  # a fake stand-in for the developer's own address
APP_YAML_WITH_ADMIN_ENTRY = f"""\
command: ["python", "-m", "appmux"]
env:
  - name: APPMUX_BOOTSTRAP_ADMINS
    value: {BOOTSTRAP_PLACEHOLDER}
  - name: APPMUX_LAKEBASE_DATABASE
    value: databricks_postgres
  # Where the content store keeps its files.
  # - name: APPMUX_CONTENT_STORE_FOLDER
  #   value: /Workspace/Shared/appmux-store
"""
STORE_FOLDER_TEMPLATE_LINES = (
    "  # - name: APPMUX_CONTENT_STORE_FOLDER\n  #   value: /Workspace/Shared/appmux-store\n"
)
FIXED_STORE_FOLDER = "/Workspace/Shared/team-store/appmux"


def synced_app_yaml(
    fake: FakeTree, stub: StubCli, tmp_path: Path, **overrides: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run a deployment and return the run with the app.yaml the CLI was asked to sync."""
    write(fake.root / "app.yaml", APP_YAML_WITH_ADMIN_ENTRY)
    kept = tmp_path / "synced-app.yaml"
    environment = sync_environment(fake, stub, STUB_SYNC_APP_YAML=str(kept), **overrides)
    return run_script(SYNC, environment), kept


def test_sync_deploy_substitutes_the_bootstrap_admin_only_in_the_synced_copy(
    tree: FakeTree, stub_cli: StubCli, tmp_path: Path
) -> None:
    result, kept = synced_app_yaml(
        tree, stub_cli, tmp_path, APPMUX_DEPLOY_BOOTSTRAP_ADMIN=OWNER_ADDRESS
    )

    assert result.returncode == 0, result.stderr
    synced = kept.read_text()
    assert f"value: {OWNER_ADDRESS}\n" in synced
    assert BOOTSTRAP_PLACEHOLDER not in synced
    assert "value: databricks_postgres" in synced, "only the admin entry may change"
    # Neither the repository's app.yaml nor the assembled folder (which a release is built from)
    # may ever hold the developer's address.
    assert OWNER_ADDRESS not in (tree.root / "app.yaml").read_text()
    assert OWNER_ADDRESS not in (tree.deploy / "app.yaml").read_text()


def test_sync_deploy_leaves_the_placeholder_when_no_bootstrap_admin_is_given(
    tree: FakeTree, stub_cli: StubCli, tmp_path: Path
) -> None:
    result, kept = synced_app_yaml(tree, stub_cli, tmp_path)

    assert result.returncode == 0, result.stderr
    assert kept.read_text() == APP_YAML_WITH_ADMIN_ENTRY


OVERLAY_APP_YAML = APP_YAML_WITH_ADMIN_ENTRY.replace(
    'command: ["python", "-m", "appmux"]', 'command: ["python", "container_test_entry.py"]'
)


def overlay_folder(tmp_path: Path) -> Path:
    """A folder of test-run files: its own app.yaml (another command) and two more files."""
    folder = tmp_path / "overlay"
    write(folder / "app.yaml", OVERLAY_APP_YAML)
    write(folder / "container_test_entry.py", "print('entry')\n")
    write(folder / "tests" / "integration" / "test_a.py", "def test_a(): ...\n")
    return folder


def test_sync_deploy_lays_an_overlay_over_the_synced_copy_only(
    tree: FakeTree, stub_cli: StubCli, tmp_path: Path
) -> None:
    folder = overlay_folder(tmp_path)

    result, kept = synced_app_yaml(tree, stub_cli, tmp_path, APPMUX_DEPLOY_OVERLAY=str(folder))

    assert result.returncode == 0, result.stderr
    synced = {line.removeprefix("./") for line in stub_cli.sync_listing.read_text().splitlines()}
    assert {"container_test_entry.py", "tests/integration/test_a.py"} <= synced
    assert synced >= EXPECTED_DEPLOY_FILES, "the overlay adds files; it removes none"
    assert "container_test_entry.py" in kept.read_text(), "the overlay's app.yaml wins"
    # The repository, the assembled folder and a release built from it never see the overlay.
    assert "container_test_entry.py" not in (tree.root / "app.yaml").read_text()
    assert "container_test_entry.py" not in (tree.deploy / "app.yaml").read_text()
    assert not (tree.deploy / "tests").exists()


def test_sync_deploy_puts_the_bootstrap_admin_in_the_overlays_app_yaml(
    tree: FakeTree, stub_cli: StubCli, tmp_path: Path
) -> None:
    folder = overlay_folder(tmp_path)

    result, kept = synced_app_yaml(
        tree,
        stub_cli,
        tmp_path,
        APPMUX_DEPLOY_OVERLAY=str(folder),
        APPMUX_DEPLOY_BOOTSTRAP_ADMIN=OWNER_ADDRESS,
    )

    assert result.returncode == 0, result.stderr
    assert "container_test_entry.py" in kept.read_text(), "the overlay's app.yaml was synced"
    assert f"value: {OWNER_ADDRESS}\n" in kept.read_text()
    assert BOOTSTRAP_PLACEHOLDER not in kept.read_text()


def test_sync_deploy_refuses_an_overlay_that_is_not_a_folder_before_calling_the_cli(
    tree: FakeTree, stub_cli: StubCli, tmp_path: Path
) -> None:
    result = run_script(
        SYNC, sync_environment(tree, stub_cli, APPMUX_DEPLOY_OVERLAY=str(tmp_path / "absent"))
    )

    assert result.returncode != 0
    assert "APPMUX_DEPLOY_OVERLAY" in result.stderr
    assert not stub_cli.calls.exists()


def test_sync_deploy_never_prints_the_bootstrap_admin(
    tree: FakeTree, stub_cli: StubCli, tmp_path: Path
) -> None:
    result, _ = synced_app_yaml(
        tree, stub_cli, tmp_path, APPMUX_DEPLOY_BOOTSTRAP_ADMIN=OWNER_ADDRESS
    )

    assert result.returncode == 0, result.stderr
    shown = result.stdout + result.stderr
    assert OWNER_ADDRESS not in shown
    assert "owner.name" not in shown
    assert "bootstrap" in shown, (
        "the run should say that a substitution was made, without the value"
    )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("not-an-address", id="no-at-sign"),
        pytest.param("a@example.com,b@example.com", id="two-addresses"),
        pytest.param("a@example.com; touch pwned", id="shell-text"),
        pytest.param("a@example.com\nextra: key", id="newline-injects-yaml"),
        pytest.param("a@example.com #comment", id="space-and-comment"),
        pytest.param("%a@example.com", id="yaml-directive-start"),
    ],
)
def test_sync_deploy_refuses_a_bootstrap_admin_that_is_not_one_plain_address(
    tree: FakeTree, stub_cli: StubCli, tmp_path: Path, value: str
) -> None:
    result, _ = synced_app_yaml(tree, stub_cli, tmp_path, APPMUX_DEPLOY_BOOTSTRAP_ADMIN=value)

    assert result.returncode != 0
    assert "APPMUX_DEPLOY_BOOTSTRAP_ADMIN" in result.stderr
    assert "touch pwned" not in result.stdout + result.stderr, "the refused value is not echoed"
    assert not stub_cli.calls.exists(), "nothing may be uploaded after a refusal"


def test_sync_deploy_turns_the_store_folder_template_into_an_entry_in_the_synced_copy_only(
    tree: FakeTree, stub_cli: StubCli, tmp_path: Path
) -> None:
    result, kept = synced_app_yaml(
        tree, stub_cli, tmp_path, APPMUX_DEPLOY_CONTENT_STORE_FOLDER=FIXED_STORE_FOLDER
    )

    assert result.returncode == 0, result.stderr
    synced = kept.read_text()
    assert f"  - name: APPMUX_CONTENT_STORE_FOLDER\n    value: {FIXED_STORE_FOLDER}\n" in synced
    assert STORE_FOLDER_TEMPLATE_LINES not in synced
    assert "value: databricks_postgres" in synced, "only the store entry may change"
    assert FIXED_STORE_FOLDER not in (tree.root / "app.yaml").read_text()
    assert FIXED_STORE_FOLDER not in (tree.deploy / "app.yaml").read_text()


@pytest.mark.parametrize(
    "value",
    [
        "Shared/appmux-store",
        "/Users/someone/appmux-store",
        "/Workspace",
        "/Workspace/",
        "/Workspace/Shared/../Users/someone/store",
        "/Workspace/Shared/./store",
        "/Workspace//Shared/store",
        "/Workspace/has space/store",
        "/Workspace/x; touch pwned",
        "/Workspace/x\nvalue: injected",
    ],
)
def test_sync_deploy_refuses_a_store_folder_that_is_not_a_plain_workspace_path(
    tree: FakeTree, stub_cli: StubCli, tmp_path: Path, value: str
) -> None:
    result, _ = synced_app_yaml(tree, stub_cli, tmp_path, APPMUX_DEPLOY_CONTENT_STORE_FOLDER=value)

    assert result.returncode != 0
    assert "APPMUX_DEPLOY_CONTENT_STORE_FOLDER" in result.stderr
    assert "touch pwned" not in result.stdout + result.stderr, "the refused value is not echoed"
    assert not stub_cli.calls.exists(), "nothing may be uploaded after a refusal"


def test_sync_deploy_refuses_a_store_folder_when_app_yaml_has_no_such_entry(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    environment = sync_environment(
        tree, stub_cli, APPMUX_DEPLOY_CONTENT_STORE_FOLDER=FIXED_STORE_FOLDER
    )

    result = run_script(SYNC, environment)

    assert result.returncode != 0
    assert "APPMUX_CONTENT_STORE_FOLDER" in result.stderr


def test_sync_deploy_refuses_to_substitute_when_app_yaml_has_no_admin_entry(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    # The fixture's own app.yaml has no APPMUX_BOOTSTRAP_ADMINS entry: nothing to substitute, so
    # a deployment that would silently keep (or lack) the admin must stop instead.
    environment = sync_environment(tree, stub_cli, APPMUX_DEPLOY_BOOTSTRAP_ADMIN=OWNER_ADDRESS)

    result = run_script(SYNC, environment)

    assert result.returncode != 0
    assert "APPMUX_BOOTSTRAP_ADMINS" in result.stderr
    assert OWNER_ADDRESS not in result.stdout + result.stderr
    assert not stub_cli.calls.exists()


@pytest.mark.parametrize("final_state", ["FAILED", "CANCELLED"])
def test_sync_deploy_fails_on_a_failed_deployment_and_masks_the_platform_message(
    tree: FakeTree, stub_cli: StubCli, final_state: str
) -> None:
    environment = sync_environment(
        tree,
        stub_cli,
        STUB_STATE=final_state,
        STUB_MESSAGE=(
            f"Error installing packages from {WORKSPACE_FOLDER} for user@example.com "
            f"with Bearer {FAKE_OPAQUE_VALUE} and {FAKE_DOSE_TOKEN}"
        ),
    )

    result = run_script(SYNC, environment)

    assert result.returncode != 0
    assert final_state in result.stderr
    assert "fetch-logs.sh" in result.stderr
    assert "Error installing packages" in result.stderr
    shown = result.stdout + result.stderr
    assert "user@example.com" not in shown
    assert WORKSPACE_FOLDER not in shown
    assert FAKE_OPAQUE_VALUE not in shown
    assert FAKE_DOSE_TOKEN not in shown


def test_sync_deploy_gives_up_when_the_deployment_never_finishes(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    result = run_script(SYNC, sync_environment(tree, stub_cli, STUB_STATE="IN_PROGRESS"))

    assert result.returncode != 0
    assert "still not final" in result.stderr
    status_checks = [line for line in stub_cli.call_lines() if "get-deployment" in line]
    assert len(status_checks) == 3


def test_sync_deploy_never_calls_the_cli_when_the_assembly_is_refused(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    shutil.rmtree(tree.slirp_source)

    result = run_script(SYNC, sync_environment(tree, stub_cli))

    assert result.returncode != 0
    assert "fetch-slirp4netns.sh" in result.stderr
    assert not stub_cli.calls.exists(), "an unchecked folder must never be uploaded"


def test_sync_deploy_builds_the_ui_before_it_assembles_the_folder(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    # The stub build replaces the export; the deployment folder must hold the new one.
    write(
        tree.root / "web" / "build.sh",
        '#!/usr/bin/env bash\nmkdir -p "$(dirname "$0")/out"\n'
        'echo fresh > "$(dirname "$0")/out/built-by-build-sh.txt"\n',
        executable=True,
    )

    result = run_script(SYNC, sync_environment(tree, stub_cli))

    assert result.returncode == 0, result.stderr
    assert "web/out/built-by-build-sh.txt" in files_under(tree.deploy)


def test_sync_deploy_never_calls_the_cli_when_the_ui_build_fails(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    write(
        tree.root / "web" / "build.sh",
        "#!/usr/bin/env bash\necho 'the UI build broke' >&2\nexit 1\n",
        executable=True,
    )

    result = run_script(SYNC, sync_environment(tree, stub_cli))

    assert result.returncode != 0
    assert "the UI build broke" in result.stderr
    assert not stub_cli.calls.exists(), "a folder with a stale or missing UI must never be uploaded"
    assert not tree.deploy.exists(), "the folder is assembled only after the UI is built"


@dataclass(frozen=True)
class SensitiveShape:
    """One log line that carries text which must never be printed."""

    label: str
    line: str
    secrets: tuple[str, ...]
    kept: str  # harmless words of the same line that must survive the mask


def sensitive_shapes() -> list[SensitiveShape]:
    """Token and identifier shapes, all fake and built at run time (no literal in the source)."""
    personal_access_token = "dapi" + "a1b2c3d4" * 4
    oauth_secret = FAKE_DOSE_TOKEN
    jwt = ".".join(["eyJ" + "a" * 12, "b" * 12, "c" * 12])
    opaque = FAKE_OPAQUE_VALUE
    basic_credentials = "dXNlcjpwYXNzd29yZA=="
    org_id = "1234567890123456"
    shapes = [
        (
            "databricks-token",
            f"call made with {personal_access_token} by the runner",
            personal_access_token,
            "by the runner",
        ),
        (
            "oauth-secret",
            f"exchange used {oauth_secret} and then stopped",
            oauth_secret,
            "and then stopped",
        ),
        ("jwt", f"forwarded {jwt} to the proxy", jwt, "to the proxy"),
        ("bearer", f"Authorization: Bearer {opaque} accepted", opaque, "accepted"),
        ("bearer-lowercase", f"authorization: bearer {opaque} accepted", opaque, "accepted"),
        (
            "authorization-token-scheme",
            f"Authorization: token {opaque} accepted",
            opaque,
            "accepted",
        ),
        (
            "basic",
            f"Authorization: Basic {basic_credentials} accepted",
            basic_credentials,
            "accepted",
        ),
        ("bearer-in-prose", f"proxy forwarded Bearer {opaque} upstream", opaque, "upstream"),
        (
            "basic-in-prose",
            f"proxy forwarded Basic {basic_credentials} upstream",
            basic_credentials,
            "upstream",
        ),
        (
            "json-access-token",
            f'{{"access_token": "{opaque}", "expires_in": 3600}}',
            opaque,
            '"expires_in": 3600',
        ),
        ("token-assignment", f"retry with token={opaque} soon", opaque, "retry with"),
        ("client-secret", f"client_secret={opaque} was rejected", opaque, "was rejected"),
        ("password", f"login failed, password: {opaque}", opaque, "login failed"),
        ("api-key", f"header api_key={opaque} ignored", opaque, "ignored"),
        (
            "aws-host",
            "connect to fake-workspace.cloud.databricks.com:443 failed",
            "fake-workspace.cloud.databricks.com",
            "failed",
        ),
        (
            "azure-host",
            f"connect to adb-{org_id}.7.azuredatabricks.net failed",
            f"adb-{org_id}",
            "failed",
        ),
        (
            "gcp-host",
            f"connect to {org_id}.7.gcp.databricks.com failed",
            f"{org_id}.7.gcp",
            "failed",
        ),
        (
            "apps-host",
            f"proxy to fake-app-{org_id}.aws.databricksapps.com failed",
            f"fake-app-{org_id}",
            "failed",
        ),
        ("adb-workspace-id", f"workspace adb-{org_id} ready", f"adb-{org_id}", "ready"),
        ("org-id-query", f"request carried o={org_id} in the query", org_id, "in the query"),
        ("org-id-header", f"X-Databricks-Org-Id: {org_id} was set", org_id, "was set"),
        ("workspace-id-assignment", f"workspace_id={org_id} was set", org_id, "was set"),
        (
            "workspace-id-json",
            f'{{"workspaceId": "{org_id}", "state": "ok"}}',
            org_id,
            '"state": "ok"',
        ),
        (
            "url",
            "user opened https://workspace.example.invalid/apps/appmux today",
            "workspace.example.invalid",
            "today",
        ),
        (
            "url-with-org-id",
            f"GET https://fake-workspace.cloud.databricks.com/?o={org_id} done",
            org_id,
            "done",
        ),
        ("email", "owner user@example.com was notified", "user@example.com", "was notified"),
        (
            "workspace-path",
            "source /Workspace/Users/user@example.com/appmux-deploy/app.yaml read",
            "appmux-deploy",
            "read",
        ),
    ]
    return [SensitiveShape(label, line, (secret,), kept) for label, line, secret, kept in shapes]


BENIGN_LINES = [
    "child demo started on port 8001",
    "2026-10-30T12:00:00Z INFO request finished in 12 ms",
    '{"token_type": "Bearer", "scope": "all-apis"}',
    "retry in 5 seconds (attempt 2/3)",
]

MASK_HELPER = SCRIPTS_DIR / "lib" / "mask-identifiers.sh"


def mask_with_helper(text: str, **environment: str) -> str:
    """Source the shared helper in a fresh bash and pass the text through mask_identifiers."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; source "$1"; mask_identifiers',
            "bash",
            str(MASK_HELPER),
        ],
        input=text,
        env={"PATH": os.environ["PATH"], **environment},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("shape", sensitive_shapes(), ids=lambda shape: shape.label)
def test_the_masking_helper_hides_the_secret_and_keeps_the_rest_of_the_line(
    shape: SensitiveShape,
) -> None:
    masked = mask_with_helper(shape.line + "\n")

    for secret in shape.secrets:
        assert secret not in masked, masked
    assert shape.kept in masked, masked


@pytest.mark.parametrize("benign_line", BENIGN_LINES)
def test_the_masking_helper_leaves_ordinary_log_lines_alone(benign_line: str) -> None:
    assert mask_with_helper(benign_line + "\n") == benign_line + "\n"


@pytest.mark.parametrize(
    "workspace_folder",
    [
        pytest.param(WORKSPACE_FOLDER, id="typical"),
        pytest.param("/Workspace/Shared/app+mux (dev)[1]/v1.0", id="regex-characters"),
        pytest.param("/Workspace/Shared/with#hash/and&amp", id="delimiter-characters"),
    ],
)
def test_the_masking_helper_hides_the_configured_workspace_folder(workspace_folder: str) -> None:
    line = f"Initial Sync Complete for {workspace_folder} (3 files)"

    masked = mask_with_helper(line + "\n", APPMUX_WORKSPACE_FOLDER=workspace_folder)

    assert masked == "Initial Sync Complete for <workspace-folder> (3 files)\n"


def test_the_masking_helper_does_not_treat_the_folder_as_a_pattern() -> None:
    folder = "/Workspace/Shared/a.b"
    other = "/Workspace/Shared/aXb"

    masked = mask_with_helper(f"{folder} and {other}\n", APPMUX_WORKSPACE_FOLDER=folder)

    # The second path is not the folder; it still goes, through the /Workspace/ rule.
    assert masked == "<workspace-folder> and <workspace-path>\n"


def test_the_masking_helper_masks_without_a_configured_folder() -> None:
    masked = mask_with_helper("user user@example.com read /Workspace/Shared/x\n")

    assert masked == "user <email> read <workspace-path>\n"


def test_the_masking_helper_parses() -> None:
    syntax = subprocess.run(
        ["bash", "-n", str(MASK_HELPER)], capture_output=True, text=True, check=False, timeout=30
    )

    assert syntax.returncode == 0, syntax.stderr


def every_shape_secret() -> list[str]:
    return [secret for shape in sensitive_shapes() for secret in shape.secrets]


def test_fetch_logs_masks_identifiers_and_secret_shapes(stub_cli: StubCli) -> None:
    write(
        Path(stub_cli.environment["STUB_LOG_TEXT"]),
        "\n".join([shape.line for shape in sensitive_shapes()] + BENIGN_LINES) + "\n",
    )

    result = run_script(FETCH_LOGS, {**stub_cli.environment, "APPMUX_DATABRICKS_PROFILE": PROFILE})

    assert result.returncode == 0, result.stderr
    shown = result.stdout + result.stderr
    assert [secret for secret in every_shape_secret() if secret in shown] == []
    assert all(shape.kept in result.stdout for shape in sensitive_shapes())
    assert all(line in result.stdout for line in BENIGN_LINES)
    assert stub_cli.call_lines() == [f"apps logs appmux --tail-lines 200 --profile {PROFILE}"]


def test_fetch_logs_masks_the_workspace_folder_when_it_is_configured(stub_cli: StubCli) -> None:
    write(
        Path(stub_cli.environment["STUB_LOG_TEXT"]),
        f"Initial Sync Complete for {WORKSPACE_FOLDER}\n",
    )

    result = run_script(
        FETCH_LOGS,
        {
            **stub_cli.environment,
            "APPMUX_DATABRICKS_PROFILE": PROFILE,
            "APPMUX_WORKSPACE_FOLDER": WORKSPACE_FOLDER,
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Initial Sync Complete for <workspace-folder>\n"


def test_sync_deploy_masks_every_shape_in_the_cli_output(tree: FakeTree, stub_cli: StubCli) -> None:
    extra_output = "\n".join(shape.line for shape in sensitive_shapes())

    result = run_script(SYNC, sync_environment(tree, stub_cli, STUB_EXTRA_OUTPUT=extra_output))

    assert result.returncode == 0, result.stderr
    shown = result.stdout + result.stderr
    assert [secret for secret in every_shape_secret() if secret in shown] == []
    assert all(shape.kept in result.stdout for shape in sensitive_shapes())
    assert all(shape.kept in result.stderr for shape in sensitive_shapes())


def test_fetch_logs_passes_its_filters_to_the_cli(stub_cli: StubCli) -> None:
    environment = {
        **stub_cli.environment,
        "APPMUX_DATABRICKS_PROFILE": PROFILE,
        "APPMUX_APP_NAME": "appmux-dev",
    }

    result = run_script(
        FETCH_LOGS, environment, "--tail-lines", "5", "--source", "APP", "--search", "needle"
    )

    assert result.returncode == 0, result.stderr
    assert stub_cli.call_lines() == [
        f"apps logs appmux-dev --tail-lines 5 --source APP --search needle --profile {PROFILE}"
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["--source", "BOTH"], id="unknown-source"),
        pytest.param(["--tail-lines", "many"], id="tail-lines-not-a-number"),
        pytest.param(["--follow"], id="unknown-option"),
    ],
)
def test_fetch_logs_rejects_bad_arguments_before_calling_the_cli(
    stub_cli: StubCli, arguments: list[str]
) -> None:
    result = run_script(
        FETCH_LOGS, {**stub_cli.environment, "APPMUX_DATABRICKS_PROFILE": PROFILE}, *arguments
    )

    assert result.returncode != 0
    assert not stub_cli.calls.exists()


@pytest.mark.parametrize(
    "script",
    [ASSEMBLE, SYNC, FETCH_LOGS, VENDOR_WHEELS, RUN_CONTAINER_TESTS],
    ids=lambda path: path.name,
)
def test_every_script_parses_and_is_executable(script: Path) -> None:
    syntax = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False, timeout=30
    )

    assert syntax.returncode == 0, syntax.stderr
    assert os.access(script, os.X_OK), f"{script.name} must carry the executable bit"


# --- loading the schema when deploying (T217) ----------------------------------------------------


def schema_environment(fake: FakeTree, stub: StubCli, **overrides: str) -> dict[str, str]:
    return sync_environment(fake, stub, APPMUX_LAKEBASE_ENDPOINT=LAKEBASE_ENDPOINT, **overrides)


def loader_calls(stub: StubCli) -> list[str]:
    return [line for line in stub.call_lines() if line.startswith("uv ")]


@pytest.mark.parametrize(
    "unset_variable", ["APPMUX_DATABRICKS_PROFILE", "APPMUX_LAKEBASE_ENDPOINT"]
)
def test_load_schema_stops_on_an_unset_variable_before_calling_anything(
    tree: FakeTree, stub_cli: StubCli, unset_variable: str
) -> None:
    environment = schema_environment(tree, stub_cli)
    del environment[unset_variable]

    result = run_script(LOAD_SCHEMA, environment)

    assert result.returncode != 0
    assert unset_variable in result.stderr
    assert not stub_cli.calls.exists(), "the CLI or the loader was called"


def test_load_schema_gives_the_loader_the_login_through_its_environment(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    result = run_script(LOAD_SCHEMA, schema_environment(tree, stub_cli))

    assert result.returncode == 0, result.stderr
    calls = stub_cli.call_lines()
    assert [
        call.split()[:2] for call in calls if call.startswith(("postgres", "current-user"))
    ] == [
        ["postgres", "get-endpoint"],
        ["current-user", "me"],
        ["postgres", "generate-database-credential"],
    ]
    assert (
        f"uv run --project {tree.root} python -m appmux.registry.load_schema "
        f"--migrations {tree.root / 'migrations'}"
    ) in calls
    assert "login-in-environment" in calls
    assert all(FAKE_DB_TOKEN not in call for call in calls), "the token reached a command line"


def test_load_schema_takes_the_migrations_folder_as_an_argument(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    result = run_script(LOAD_SCHEMA, schema_environment(tree, stub_cli), "/unpacked/migrations")

    assert result.returncode == 0, result.stderr
    [call] = loader_calls(stub_cli)
    assert call.endswith("--migrations /unpacked/migrations")


@pytest.mark.parametrize("loader_status", [0, 1, 2, 3])
def test_load_schema_exits_with_the_code_of_the_loader(
    tree: FakeTree, stub_cli: StubCli, loader_status: int
) -> None:
    environment = schema_environment(tree, stub_cli, STUB_LOAD_STATUS=str(loader_status))

    result = run_script(LOAD_SCHEMA, environment)

    assert result.returncode == loader_status


def test_load_schema_does_not_run_the_loader_when_the_credential_is_refused(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    result = run_script(LOAD_SCHEMA, schema_environment(tree, stub_cli, STUB_CREDENTIAL_FAILS="1"))

    assert result.returncode == 1
    assert loader_calls(stub_cli) == []
    assert "credential" in result.stderr


def test_load_schema_masks_what_the_loader_prints(tree: FakeTree, stub_cli: StubCli) -> None:
    leaking = f"login as deployer@example.test failed, token={FAKE_DB_TOKEN}"

    result = run_script(LOAD_SCHEMA, schema_environment(tree, stub_cli, STUB_LOAD_OUTPUT=leaking))

    shown = result.stdout + result.stderr
    assert "deployer@example.test" not in shown
    assert FAKE_DB_TOKEN not in shown
    assert "failed" in shown, "the rest of the line is kept"


def test_sync_deploy_loads_the_schema_of_the_deployment_folder_before_it_uploads(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    result = run_script(SYNC, schema_environment(tree, stub_cli))

    assert result.returncode == 0, result.stderr
    calls = stub_cli.call_lines()
    [loader_call] = loader_calls(stub_cli)
    assert loader_call.endswith(f"--migrations {tree.deploy / 'migrations'}")
    first_upload = next(i for i, call in enumerate(calls) if call.startswith("sync "))
    assert calls.index(loader_call) < first_upload
    assert any(call.startswith("apps deploy") for call in calls[first_upload:])


@pytest.mark.parametrize("loader_status", [1, 2])
def test_sync_deploy_uploads_and_deploys_nothing_when_the_schema_cannot_be_loaded(
    tree: FakeTree, stub_cli: StubCli, loader_status: int
) -> None:
    environment = schema_environment(tree, stub_cli, STUB_LOAD_STATUS=str(loader_status))

    result = run_script(SYNC, environment)

    assert result.returncode != 0
    assert "schema" in result.stderr
    calls = stub_cli.call_lines()
    assert not any(call.startswith(("sync ", "apps ")) for call in calls)


def test_sync_deploy_goes_on_when_the_schema_is_already_loaded(
    tree: FakeTree, stub_cli: StubCli
) -> None:
    result = run_script(SYNC, schema_environment(tree, stub_cli, STUB_LOAD_STATUS="3"))

    assert result.returncode == 0, result.stderr
    assert any(call.startswith("apps deploy") for call in stub_cli.call_lines())
    assert "already loaded" in result.stdout


def test_sync_deploy_says_when_it_skips_the_schema_step(tree: FakeTree, stub_cli: StubCli) -> None:
    result = run_script(SYNC, sync_environment(tree, stub_cli))

    assert result.returncode == 0, result.stderr
    assert "APPMUX_LAKEBASE_ENDPOINT" in result.stdout
    assert "schema" in result.stdout
    assert loader_calls(stub_cli) == []
    assert not any(call.startswith("postgres") for call in stub_cli.call_lines())
