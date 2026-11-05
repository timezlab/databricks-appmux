"""The files a test run inside the container is made of (``scripts/container_test_run.py``).

``scripts/run-container-tests.sh`` lays an overlay over the synced copy of the deployment folder:
an ``app.yaml`` whose command is the test entry, the tests, a pytest configuration, the vendored
test libraries and the run configuration. The overlay is the only place the user's access token
travels to the container, and a wrong overlay does not fail loudly: an ``app.yaml`` that kept the
normal command runs appmux and no test, a configuration that selects nothing reads as a pass, a
compiled library vendored from the developer's machine fails to import on the platform. So the
parts that can be wrong silently are checked here, on made-up trees; the real run is the proof of
the rest. Nothing here touches the network, a workspace or the real ``build/`` folder.

The module is loaded by path because ``scripts/`` is not a package.
"""

from __future__ import annotations

import configparser
import importlib.util
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
MODULE = SCRIPTS_DIR / "container_test_run.py"
RUN_ID = "0123456789abcdef"
# Made up, and built at run time so no secret-shaped literal sits in the source.
FAKE_TOKEN = "tok" + "y" * 12
FOLDER = "/Workspace/Users/someone@example.test/fixtures"
# In the App environment (spike 18's package list) and needed by pytest, so not vendored.
PACKAGES_THE_PLATFORM_HAS_THAT_PYTEST_NEEDS = ("packaging", "pygments")


@pytest.fixture(scope="module")
def run_module() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        spec = importlib.util.spec_from_file_location("container_test_run_under_test", MODULE)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["container_test_run_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS_DIR))
    return module


def caller_environment(**extra: str) -> dict[str, str]:
    environment = {
        "APPMUX_TEST_USER_TOKEN": FAKE_TOKEN,
        "APPMUX_TEST_FIXTURES_FOLDER": FOLDER,
        "PATH": "/usr/bin",
        "DATABRICKS_CONFIG_PROFILE": "somebody",
    }
    environment.update(extra)
    return environment


# ---- which variables travel ---------------------------------------------------------------------


def test_the_test_variables_of_the_caller_travel_and_nothing_else_does(
    run_module: ModuleType,
) -> None:
    travelling = run_module.variables_for_app(
        caller_environment(APPMUX_TEST_WAREHOUSE_ID="warehouse-1")
    )

    assert travelling == {
        "APPMUX_TEST_USER_TOKEN": FAKE_TOKEN,
        "APPMUX_TEST_FIXTURES_FOLDER": FOLDER,
        "APPMUX_TEST_WAREHOUSE_ID": "warehouse-1",
    }


@pytest.mark.parametrize(
    "owned_by_the_entry",
    [
        "APPMUX_TEST_CONTAINER",
        "APPMUX_TEST_REMOTE",
        "APPMUX_TEST_BASE_URL",
        "APPMUX_TEST_ADMIN_EMAIL",
        "APPMUX_TEST_EDGE_PID",
        "APPMUX_TEST_TOKEN_PROFILE",
    ],
)
def test_what_the_entry_decides_is_never_taken_from_the_callers_environment(
    run_module: ModuleType, owned_by_the_entry: str
) -> None:
    travelling = run_module.variables_for_app(caller_environment(**{owned_by_the_entry: "x"}))

    assert owned_by_the_entry not in travelling


@pytest.mark.parametrize("missing", ["APPMUX_TEST_USER_TOKEN", "APPMUX_TEST_FIXTURES_FOLDER"])
def test_a_missing_required_variable_is_named_before_anything_is_built(
    run_module: ModuleType, missing: str
) -> None:
    environment = caller_environment()
    del environment[missing]

    with pytest.raises(run_module.OverlayError, match=missing):
        run_module.variables_for_app(environment)


def test_an_empty_required_variable_counts_as_missing(run_module: ModuleType) -> None:
    with pytest.raises(run_module.OverlayError, match="APPMUX_TEST_USER_TOKEN"):
        run_module.variables_for_app({**caller_environment(), "APPMUX_TEST_USER_TOKEN": "  "})


# ---- app.yaml of the test run -------------------------------------------------------------------


def real_app_yaml() -> str:
    return (REPO_ROOT / "app.yaml").read_text(encoding="utf-8")


def test_the_command_of_the_test_run_is_the_entry_and_not_appmux(run_module: ModuleType) -> None:
    document = yaml.safe_load(run_module.app_yaml_for_test_run(real_app_yaml(), {}))

    assert document["command"] == ["python", "container_test_entry.py"]


def test_the_environment_of_the_real_app_yaml_is_kept(run_module: ModuleType) -> None:
    original = yaml.safe_load(real_app_yaml())["env"]

    document = yaml.safe_load(
        run_module.app_yaml_for_test_run(real_app_yaml(), {"APPMUX_TEST_USER_TOKEN": FAKE_TOKEN})
    )

    assert document["env"][: len(original)] == original


def test_every_variable_becomes_one_environment_entry_with_its_exact_value(
    run_module: ModuleType,
) -> None:
    awkward = 'has "quotes", a colon: and a # hash'
    variables = {"APPMUX_TEST_USER_TOKEN": FAKE_TOKEN, "APPMUX_TEST_NOTE": awkward}

    document = yaml.safe_load(run_module.app_yaml_for_test_run(real_app_yaml(), variables))

    added = {entry["name"]: entry["value"] for entry in document["env"]}
    assert added["APPMUX_TEST_USER_TOKEN"] == FAKE_TOKEN
    assert added["APPMUX_TEST_NOTE"] == awkward


def test_a_value_with_a_line_break_cannot_start_a_new_yaml_entry(run_module: ModuleType) -> None:
    hostile = "x\n  - name: APPMUX_BOOTSTRAP_ADMINS\n    value: evil@example.test"

    document = yaml.safe_load(
        run_module.app_yaml_for_test_run(real_app_yaml(), {"APPMUX_TEST_NOTE": hostile})
    )

    names = [entry["name"] for entry in document["env"]]
    assert names.count("APPMUX_BOOTSTRAP_ADMINS") == 1


@pytest.mark.parametrize(
    "document",
    [
        "env:\n  - name: A\n    value: b\n",
        'command: ["python", "-m", "appmux"]\ncommand: ["python", "x.py"]\nenv:\n  - name: A\n',
        'command: ["python", "-m", "appmux"]\nenv:\n  - name: A\n    value: b\nresources: []\n',
        'command: ["python", "-m", "appmux"]\n',
    ],
)
def test_an_app_yaml_that_is_not_the_expected_shape_is_refused(
    run_module: ModuleType, document: str
) -> None:
    with pytest.raises(run_module.OverlayError):
        run_module.app_yaml_for_test_run(document, {"APPMUX_TEST_USER_TOKEN": FAKE_TOKEN})


# ---- pytest.ini ---------------------------------------------------------------------------------


def parsed_ini(run_module: ModuleType) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read_string(
        run_module.pytest_ini((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    )
    return parser


def test_pytest_gets_the_markers_and_options_of_pyproject_so_there_is_one_source(
    run_module: ModuleType,
) -> None:
    section = parsed_ini(run_module)["pytest"]

    assert "integration" in section["markers"]
    assert "--strict-markers" in section["addopts"]
    assert section["testpaths"] == "tests"


def test_pytest_does_not_write_a_cache_into_the_deployed_folder(run_module: ModuleType) -> None:
    assert "-p no:cacheprovider" in parsed_ini(run_module)["pytest"]["addopts"]


def test_a_pyproject_without_pytest_options_is_refused(run_module: ModuleType) -> None:
    with pytest.raises(run_module.OverlayError, match="pytest"):
        run_module.pytest_ini("[project]\nname = 'x'\n")


# ---- the vendored test libraries ----------------------------------------------------------------


def fake_site_packages(root: Path, *, compiled: bool = False) -> Path:
    site = root / "site-packages"
    for package in ("pytest", "_pytest", "pluggy", "iniconfig"):
        (site / package).mkdir(parents=True)
        (site / package / "__init__.py").write_text("", encoding="utf-8")
        (site / package / "__pycache__").mkdir()
        (site / package / "__pycache__" / "x.pyc").write_bytes(b"\0")
    for distribution in ("pytest-9.1.1", "pluggy-1.6.0", "iniconfig-2.1.0"):
        (site / f"{distribution}.dist-info").mkdir()
        (site / f"{distribution}.dist-info" / "METADATA").write_text("Name: x\n", encoding="utf-8")
    (site / "py.py").write_text("", encoding="utf-8")
    (site / "httpx").mkdir()
    (site / "httpx" / "__init__.py").write_text("", encoding="utf-8")
    if compiled:
        (site / "pluggy" / "_speed.cpython-311-x86_64-linux-gnu.so").write_bytes(b"\x7fELF")
    return site


def test_the_test_libraries_and_their_metadata_are_copied_and_nothing_else(
    run_module: ModuleType, tmp_path: Path
) -> None:
    destination = tmp_path / "testlibs"

    run_module.vendor_test_libraries(fake_site_packages(tmp_path), destination)

    assert {path.name for path in destination.iterdir()} == {
        "pytest",
        "_pytest",
        "pluggy",
        "iniconfig",
        "py.py",
        "pytest-9.1.1.dist-info",
        "pluggy-1.6.0.dist-info",
        "iniconfig-2.1.0.dist-info",
    }
    assert not list(destination.rglob("__pycache__"))


def test_a_compiled_library_is_refused_because_the_platform_cannot_import_it(
    run_module: ModuleType, tmp_path: Path
) -> None:
    site = fake_site_packages(tmp_path, compiled=True)

    with pytest.raises(run_module.OverlayError, match="pluggy"):
        run_module.vendor_test_libraries(site, tmp_path / "testlibs")


def test_a_missing_single_file_module_is_named(run_module: ModuleType, tmp_path: Path) -> None:
    site = fake_site_packages(tmp_path)
    (site / "py.py").unlink()

    with pytest.raises(run_module.OverlayError, match=r"py\.py"):
        run_module.vendor_test_libraries(site, tmp_path / "testlibs")


def test_a_missing_library_is_named(run_module: ModuleType, tmp_path: Path) -> None:
    site = fake_site_packages(tmp_path)
    shutil.rmtree(site / "iniconfig")

    with pytest.raises(run_module.OverlayError, match="iniconfig"):
        run_module.vendor_test_libraries(site, tmp_path / "testlibs")


def test_the_vendored_libraries_are_enough_to_run_pytest_where_only_the_platforms_packages_exist(
    run_module: ModuleType, tmp_path: Path
) -> None:
    """The first container run (2026-11-04) crashed at ``import py``: no test ran, and the unit
    tests above, which copy made-up folders, did not see it. This one copies the real pytest and
    runs it in an interpreter that has no site-packages, with the two packages the platform's
    environment has and pytest needs (spike 18 lists them)."""
    real_site_packages = run_module.find_site_packages()
    vendored = tmp_path / "testlibs"
    run_module.vendor_test_libraries(real_site_packages, vendored)
    platform = tmp_path / "platform"
    platform.mkdir()
    for package in PACKAGES_THE_PLATFORM_HAS_THAT_PYTEST_NEEDS:
        shutil.copytree(real_site_packages / package, platform / package)
    (tmp_path / "test_probe.py").write_text(
        "def test_passes():\n    assert 1 + 1 == 2\n\n"
        "def test_fails_with_a_diff():\n    assert {'a': 1} == {'a': 2}\n",
        encoding="utf-8",
    )
    program = (
        "import sys; sys.path[:0] = sys.argv[1:3]; import pytest; "
        "raise SystemExit(pytest.main(['-p', 'no:cacheprovider', '--rootdir', sys.argv[3], "
        "sys.argv[3]]))"
    )

    # -I -S: no user site, no PYTHONPATH, no site-packages; the standard library only.
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program, str(vendored), str(platform), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    output = result.stdout + result.stderr
    assert "ModuleNotFoundError" not in output, output
    assert "1 passed" in output
    assert "1 failed" in output
    assert result.returncode == 1  # pytest's code for "some tests failed", not for "never ran"


# ---- the whole overlay --------------------------------------------------------------------------


def fake_repository(root: Path) -> Path:
    repository = root / "repository"
    (repository / "scripts").mkdir(parents=True)
    (repository / "scripts" / "container_test_entry.py").write_text("# entry\n", encoding="utf-8")
    (repository / "app.yaml").write_text(real_app_yaml(), encoding="utf-8")
    (repository / "pyproject.toml").write_text(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    for relative in (
        "tests/conftest.py",
        "tests/integration/test_a.py",
        "tests/integration/ops_session.py",
        "tests/load/http_load.py",
        "tests/unit/test_unit.py",
        "tests/contract/test_contract.py",
        "tests/fixtures/light_fastapi/app.py",
        "tests/integration/__pycache__/ops_session.cpython-311.pyc",
    ):
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# test\n", encoding="utf-8")
    return repository


def build(run_module: ModuleType, tmp_path: Path, **overrides: object) -> Path:
    out_dir = tmp_path / "overlay"
    arguments: dict[str, object] = {
        "repo_root": fake_repository(tmp_path),
        "out_dir": out_dir,
        "run_id": RUN_ID,
        "pytest_arguments": ["tests/integration/test_a.py", "-s"],
        "environment": caller_environment(),
        "site_packages": fake_site_packages(tmp_path),
    }
    arguments.update(overrides)
    run_module.build_overlay(**arguments)
    return out_dir


def relative_files(directory: Path) -> set[str]:
    return {str(path.relative_to(directory)) for path in directory.rglob("*") if path.is_file()}


def test_the_overlay_holds_what_a_test_run_needs_and_no_other_test(
    run_module: ModuleType, tmp_path: Path
) -> None:
    files = relative_files(build(run_module, tmp_path))

    assert {
        "app.yaml",
        "container_test_entry.py",
        "container-test-run.json",
        "pytest.ini",
        "tests/conftest.py",
        "tests/integration/test_a.py",
        "tests/integration/ops_session.py",
        "tests/load/http_load.py",
        "testlibs/pytest/__init__.py",
    } <= files
    assert not [name for name in files if "__pycache__" in name]
    assert not [name for name in files if name.startswith(("tests/unit", "tests/contract"))]
    assert not [name for name in files if name.startswith("tests/fixtures")]


def test_the_run_configuration_is_read_back_by_the_entry_as_it_was_given(
    run_module: ModuleType, tmp_path: Path
) -> None:
    overlay = build(run_module, tmp_path)
    spec = importlib.util.spec_from_file_location(
        "entry_for_round_trip", SCRIPTS_DIR / "container_test_entry.py"
    )
    assert spec is not None and spec.loader is not None
    entry = importlib.util.module_from_spec(spec)
    sys.modules["entry_for_round_trip"] = entry
    spec.loader.exec_module(entry)

    config = entry.load_run_config(overlay / "container-test-run.json")

    assert config.run_id == RUN_ID
    assert config.pytest_arguments == ("tests/integration/test_a.py", "-s")


def test_the_app_yaml_with_the_token_is_readable_by_its_owner_only(
    run_module: ModuleType, tmp_path: Path
) -> None:
    overlay = build(run_module, tmp_path)

    mode = stat.S_IMODE((overlay / "app.yaml").stat().st_mode)

    assert mode == 0o600
    assert FAKE_TOKEN in (overlay / "app.yaml").read_text(encoding="utf-8")


def test_the_token_is_in_no_other_file_of_the_overlay(
    run_module: ModuleType, tmp_path: Path
) -> None:
    overlay = build(run_module, tmp_path)

    holders = [
        name
        for name in relative_files(overlay)
        if FAKE_TOKEN.encode() in (overlay / name).read_bytes()
    ]

    assert holders == ["app.yaml"]


def test_a_run_that_selects_no_test_is_refused_before_the_overlay_is_built(
    run_module: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(run_module.OverlayError, match="pytest"):
        build(run_module, tmp_path, pytest_arguments=[])

    assert not (tmp_path / "overlay").exists()


def test_a_run_id_that_is_not_hex_is_refused(run_module: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(run_module.OverlayError, match="run_id"):
        build(run_module, tmp_path, run_id="not hex")


def test_an_overlay_folder_that_already_has_files_is_refused(
    run_module: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "overlay").mkdir()
    (tmp_path / "overlay" / "left-over.txt").write_text("x", encoding="utf-8")

    with pytest.raises(run_module.OverlayError, match="not empty"):
        build(run_module, tmp_path)


# ---- the command line the shell script calls ----------------------------------------------------


def run_cli(*arguments: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MODULE), *arguments],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_new_run_id_is_sixteen_hex_digits_and_differs_each_time() -> None:
    first = run_cli("new-run-id").stdout.strip()
    second = run_cli("new-run-id").stdout.strip()

    assert len(first) == 16
    assert int(first, 16) >= 0
    assert first != second


def test_the_verdict_command_exits_zero_for_a_pass_and_names_a_failure() -> None:
    log = f"noise\nAPPMUX-CONTAINER-TESTS-DONE run={RUN_ID} exit=0\n"

    done = run_cli("verdict", "--run-id", RUN_ID, stdin=log)

    assert done.returncode == 0
    assert "passed" in done.stdout


def test_the_verdict_command_exits_one_when_pytest_failed() -> None:
    log = f"APPMUX-CONTAINER-TESTS-DONE run={RUN_ID} exit=1\n"

    assert run_cli("verdict", "--run-id", RUN_ID, stdin=log).returncode == 1


def test_the_verdict_command_exits_seventy_when_pytest_never_ran() -> None:
    log = f"APPMUX-CONTAINER-TESTS-DONE run={RUN_ID} pytest did not run: appmux exited\n"

    done = run_cli("verdict", "--run-id", RUN_ID, stdin=log)

    assert done.returncode == 70
    assert "appmux exited" in done.stdout


def test_the_verdict_command_exits_two_while_the_line_is_missing() -> None:
    done = run_cli("verdict", "--run-id", RUN_ID, stdin="still running\n")

    assert done.returncode == 2
    assert "waiting" in done.stdout


def test_the_overlay_command_never_prints_the_token(tmp_path: Path) -> None:
    repository = fake_repository(tmp_path)
    done = subprocess.run(
        [
            sys.executable,
            str(MODULE),
            "overlay",
            "--repo-root",
            str(repository),
            "--out",
            str(tmp_path / "overlay"),
            "--run-id",
            RUN_ID,
            "--",
            "tests/integration/test_a.py",
        ],
        env=caller_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert FAKE_TOKEN not in done.stdout + done.stderr
    assert json.loads((tmp_path / "overlay" / "container-test-run.json").read_text())["run_id"] == (
        RUN_ID
    )
