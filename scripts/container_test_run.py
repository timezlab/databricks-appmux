"""The developer-side half of a test run inside the container (``scripts/run-container-tests.sh``).

The shell script does the Databricks work (sync, deploy, read the log, restore). This module does
what has to be exact and is easy to get silently wrong in shell:

* ``overlay``: build the folder that ``scripts/sync-deploy.sh`` lays over the synced copy of the
  deployment folder (``APPMUX_DEPLOY_OVERLAY``): an ``app.yaml`` whose command is the test entry
  (``container_test_entry.py``) and that carries the ``APPMUX_TEST_*`` variables, the tests, a
  ``pytest.ini`` made from ``pyproject.toml``, the vendored test libraries and the run
  configuration. The user's access token is one of the variables; ``app.yaml`` is the only file
  that holds it and it is written readable by its owner only;
* ``verdict``: read the App's log on standard input and say how run ``--run-id`` ended;
* ``new-run-id``: a fresh id for the run, so the log of an earlier run cannot be taken for this one.

Standard library only, plus the entry module next to it. Nothing here prints a variable's value.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent))
import container_test_entry as entry

TEST_VARIABLE_PREFIX: Final = entry.TEST_VARIABLE_PREFIX
REQUIRED_VARIABLES: Final = ("APPMUX_TEST_USER_TOKEN", "APPMUX_TEST_FIXTURES_FOLDER")
# Decided inside the container by the entry (or meaningful only on the developer's machine): a value
# from the caller's shell would contradict the real one.
ENTRY_OWNED_VARIABLES: Final = frozenset(
    {
        "APPMUX_TEST_CONTAINER",
        "APPMUX_TEST_REMOTE",
        "APPMUX_TEST_BASE_URL",
        "APPMUX_TEST_ADMIN_EMAIL",
        "APPMUX_TEST_EDGE_PID",
        "APPMUX_TEST_TOKEN_PROFILE",
    }
)
ENTRY_FILE: Final = "container_test_entry.py"
TEST_DIRECTORIES: Final = ("integration", "load")
# The fixtures are hostile apps that run only in a sandbox, so none of their code goes to the
# container. Their pinned requirements are data, and test_offline_install.py compares what a
# release recorded with them.
FIXTURE_DATA_FILE: Final = "requirements.txt"
# What pytest needs that the platform's Python does not install; all pure Python (checked below).
TEST_LIBRARIES: Final = ("pytest", "_pytest", "pluggy", "iniconfig")
# Single-file modules of those distributions: pytest imports ``py`` (a shim it ships as a file
# beside its packages), which the first container run on 2026-11-04 lacked.
TEST_MODULE_FILES: Final = ("py.py",)
COMPILED_SUFFIXES: Final = (".so", ".pyd", ".dylib")
NO_CACHE_OPTION: Final = "-p no:cacheprovider"
OWNER_ONLY: Final = 0o600
VERDICT_EXIT_CODES: Final = {
    "passed": 0,
    "failed": 1,
    "pending": 2,
    "not_run": entry.NOT_RUN_EXIT_CODE,
}


class OverlayError(Exception):
    """The overlay cannot be built; the message names the cause and never a variable's value."""


def variables_for_app(environment: Mapping[str, str]) -> dict[str, str]:
    """The ``APPMUX_TEST_*`` variables of the caller that the container's tests receive."""
    for name in REQUIRED_VARIABLES:
        if not environment.get(name, "").strip():
            raise OverlayError(f"{name} is not set in your environment")
    return {
        name: value
        for name, value in sorted(environment.items())
        if name.startswith(TEST_VARIABLE_PREFIX) and name not in ENTRY_OWNED_VARIABLES
    }


def app_yaml_for_test_run(text: str, variables: Mapping[str, str]) -> str:
    """``app.yaml`` with the test entry as its command and ``variables`` added to its ``env``.

    Text, not a YAML round trip, so the rest of the file stays as it is. It insists on the shape
    the real file has (one ``command`` line, ``env`` the last top-level key) and refuses anything
    else, since a guess here would deploy a file that does not run the tests. A value is written as
    a JSON string, which is a YAML double-quoted scalar: a quote, a colon or a line break in it
    cannot start another entry.
    """
    lines = text.splitlines()
    command_lines = [index for index, line in enumerate(lines) if line.startswith("command:")]
    if len(command_lines) != 1:
        raise OverlayError("app.yaml must have exactly one top-level command line")
    top_level = [
        line for line in lines if line and not line[0].isspace() and not line.startswith("#")
    ]
    if not top_level or top_level[-1] != "env:":
        raise OverlayError("env must be the last top-level key of app.yaml")
    lines[command_lines[0]] = f"command: {json.dumps(['python', ENTRY_FILE])}"
    for name, value in variables.items():
        lines.append(f"  - name: {name}")
        lines.append(f"    value: {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def pytest_ini(pyproject_text: str) -> str:
    """A ``pytest.ini`` with the options of ``pyproject.toml`` (the deployment folder has none)."""
    options = tomllib.loads(pyproject_text).get("tool", {}).get("pytest", {}).get("ini_options")
    if not isinstance(options, dict):
        raise OverlayError(
            "pyproject.toml has no [tool.pytest.ini_options] to build pytest.ini from"
        )
    addopts = " ".join(filter(None, [str(options.get("addopts", "")).strip(), NO_CACHE_OPTION]))
    lines = [
        "[pytest]",
        f"testpaths = {' '.join(options.get('testpaths', ['tests']))}",
        # The deployed folder is not a place to leave a .pytest_cache.
        f"addopts = {addopts}",
        "markers =",
        *(f"    {marker}" for marker in options.get("markers", [])),
    ]
    return "\n".join(lines) + "\n"


def vendor_test_libraries(site_packages: Path, destination: Path) -> None:
    """Copy pytest and its pure-Python dependencies, with their metadata, into ``destination``."""
    destination.mkdir(parents=True)
    for library in TEST_LIBRARIES:
        source = site_packages / library
        if not source.is_dir():
            raise OverlayError(f"{library} is not in the environment this script runs in")
        compiled = [
            path.name for path in source.rglob("*") if path.name.endswith(COMPILED_SUFFIXES)
        ]
        if compiled:
            raise OverlayError(
                f"{library} holds a compiled file ({compiled[0]}), which the platform cannot import"
            )
        shutil.copytree(source, destination / library, ignore=shutil.ignore_patterns("__pycache__"))
    for module_file in TEST_MODULE_FILES:
        source_file = site_packages / module_file
        if not source_file.is_file():
            raise OverlayError(f"{module_file} is not in the environment this script runs in")
        shutil.copy2(source_file, destination / module_file)
    for distribution in ("pytest", "pluggy", "iniconfig"):
        for metadata in site_packages.glob(f"{distribution}-*.dist-info"):
            shutil.copytree(metadata, destination / metadata.name)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created private, not made private afterwards: the token must never sit in a readable file.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, OWNER_ONLY)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def build_overlay(
    *,
    repo_root: Path,
    out_dir: Path,
    run_id: str,
    pytest_arguments: Sequence[str],
    environment: Mapping[str, str],
    site_packages: Path,
) -> None:
    if not entry.RUN_ID_PATTERN.fullmatch(run_id):
        raise OverlayError("run_id must be 8 to 32 lower-case hex digits")
    if not pytest_arguments:
        raise OverlayError(
            "name the pytest arguments to run (a run that selects no test would read as a pass)"
        )
    if out_dir.exists() and any(out_dir.iterdir()):
        raise OverlayError("the overlay folder is not empty")
    variables = variables_for_app(environment)
    app_yaml = app_yaml_for_test_run(
        (repo_root / "app.yaml").read_text(encoding="utf-8"), variables
    )
    ini = pytest_ini((repo_root / "pyproject.toml").read_text(encoding="utf-8"))

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_private(out_dir / "app.yaml", app_yaml)
    shutil.copy2(repo_root / "scripts" / ENTRY_FILE, out_dir / ENTRY_FILE)
    (out_dir / entry.RUN_CONFIG_FILE).write_text(
        json.dumps({"run_id": run_id, "pytest_arguments": list(pytest_arguments)}), encoding="utf-8"
    )
    (out_dir / "pytest.ini").write_text(ini, encoding="utf-8")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    (out_dir / "tests").mkdir()
    shutil.copy2(repo_root / "tests" / "conftest.py", out_dir / "tests" / "conftest.py")
    for directory in TEST_DIRECTORIES:
        shutil.copytree(
            repo_root / "tests" / directory, out_dir / "tests" / directory, ignore=ignore
        )
    for lock_file in sorted((repo_root / "tests" / "fixtures").glob(f"*/{FIXTURE_DATA_FILE}")):
        destination = out_dir / "tests" / "fixtures" / lock_file.parent.name / FIXTURE_DATA_FILE
        destination.parent.mkdir(parents=True)
        shutil.copy2(lock_file, destination)
    vendor_test_libraries(site_packages, out_dir / entry.LIBS_DIRECTORY)


def find_site_packages() -> Path:
    """Where pytest is installed in the environment running this script."""
    # Imported here: only the overlay command needs it, and only on the developer's machine.
    import pytest

    return Path(pytest.__file__).resolve().parent.parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("new-run-id", help="print a fresh run id")
    overlay = commands.add_parser("overlay", help="build the overlay folder")
    overlay.add_argument("--repo-root", type=Path, required=True)
    overlay.add_argument("--out", type=Path, required=True)
    overlay.add_argument("--run-id", required=True)
    overlay.add_argument("pytest_arguments", nargs="*", help="after --: the pytest arguments")
    verdict = commands.add_parser("verdict", help="read the App's log on standard input")
    verdict.add_argument("--run-id", required=True)
    arguments = parser.parse_args(argv)

    if arguments.command == "new-run-id":
        print(secrets.token_hex(8))
        return 0
    try:
        if arguments.command == "overlay":
            build_overlay(
                repo_root=arguments.repo_root,
                out_dir=arguments.out,
                run_id=arguments.run_id,
                pytest_arguments=arguments.pytest_arguments,
                environment=os.environ,
                site_packages=find_site_packages(),
            )
            print(f"overlay built: {sum(1 for p in arguments.out.rglob('*') if p.is_file())} files")
            return 0
        result = entry.find_verdict(sys.stdin.read(), arguments.run_id)
    except (OverlayError, entry.RunConfigError) as exc:
        print(f"{Path(__file__).name}: {exc}", file=sys.stderr)
        return 64
    print(_describe(result))
    return VERDICT_EXIT_CODES[result.kind]


def _describe(result: entry.Verdict) -> str:
    match result.kind:
        case "passed":
            return "the tests passed (pytest exit code 0)"
        case "failed":
            return f"the tests failed (pytest exit code {result.exit_code})"
        case "not_run":
            return f"pytest did not run: {result.detail}"
        case _:
            return "waiting: the closing line of this run is not in the log yet"


if __name__ == "__main__":
    raise SystemExit(main())
