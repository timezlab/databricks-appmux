"""``web/build.sh``: installs from the lockfile without install scripts, builds, checks the export.

The script is copied next to a stub ``npm`` in a temporary ``web`` folder, so nothing is
downloaded and no real build runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

BUILD_SCRIPT = Path(__file__).resolve().parents[2] / "web" / "build.sh"

# Records each call, then acts out the build: ``npm run build`` writes ``out/index.html`` unless
# STUB_NO_EXPORT is set, and any command named in STUB_FAIL_ON fails.
STUB_NPM = """#!/usr/bin/env bash
echo "$*" >> "$STUB_CALLS"
[[ "${STUB_FAIL_ON:-}" == "$1" ]] && { echo "npm $1 failed" >&2; exit 1; }
if [[ "$1" == "run" && "$2" == "build" && -z "${STUB_NO_EXPORT:-}" ]]; then
  mkdir -p out
  echo "<html></html>" > out/index.html
fi
exit 0
"""


@dataclass(frozen=True)
class FakeWeb:
    folder: Path
    stub_dir: Path
    calls: Path

    def environment(self, **overrides: str) -> dict[str, str]:
        return {
            "PATH": f"{self.stub_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.folder.parent / "home"),
            "STUB_CALLS": str(self.calls),
            **overrides,
        }

    def run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.folder / "build.sh")],
            env=self.environment(**overrides),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def call_lines(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def fake_web(tmp_path: Path) -> FakeWeb:
    folder = tmp_path / "web"
    folder.mkdir()
    shutil.copy(BUILD_SCRIPT, folder / "build.sh")
    (folder / "package-lock.json").write_text("{}\n", encoding="utf-8")
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    npm = stub_dir / "npm"
    npm.write_text(STUB_NPM, encoding="utf-8")
    npm.chmod(0o700)
    return FakeWeb(folder=folder, stub_dir=stub_dir, calls=tmp_path / "npm-calls.txt")


def test_the_build_script_is_executable() -> None:
    assert os.access(BUILD_SCRIPT, os.X_OK)


def test_it_installs_from_the_lockfile_without_install_scripts_then_builds(
    fake_web: FakeWeb,
) -> None:
    result = fake_web.run()

    assert result.returncode == 0, result.stderr
    assert fake_web.call_lines() == ["ci --ignore-scripts", "run build"]
    assert (fake_web.folder / "out" / "index.html").is_file()


def test_it_runs_in_the_web_folder_wherever_it_is_called_from(
    fake_web: FakeWeb, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = subprocess.run(
        ["bash", str(fake_web.folder / "build.sh")],
        cwd=elsewhere,
        env=fake_web.environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert (fake_web.folder / "out" / "index.html").is_file()
    assert not (elsewhere / "out").exists()


def test_an_earlier_export_is_removed_before_the_build(fake_web: FakeWeb) -> None:
    stale = fake_web.folder / "out" / "stale-page.html"
    stale.parent.mkdir()
    stale.write_text("old\n", encoding="utf-8")

    result = fake_web.run()

    assert result.returncode == 0, result.stderr
    assert not stale.exists(), "a file of the earlier export must not ship with the new one"


def test_a_failed_install_stops_before_the_build_and_leaves_no_export(fake_web: FakeWeb) -> None:
    result = fake_web.run(STUB_FAIL_ON="ci")

    assert result.returncode != 0
    assert "npm ci failed" in result.stderr
    assert fake_web.call_lines() == ["ci --ignore-scripts"]
    assert not (fake_web.folder / "out").exists()


def test_a_failed_build_fails_the_script(fake_web: FakeWeb) -> None:
    result = fake_web.run(STUB_FAIL_ON="run")

    assert result.returncode != 0
    assert not (fake_web.folder / "out" / "index.html").exists()


def test_a_build_that_writes_no_export_is_an_error_that_names_the_file(fake_web: FakeWeb) -> None:
    result = fake_web.run(STUB_NO_EXPORT="1")

    assert result.returncode != 0
    assert "web/out/index.html" in result.stderr


def test_a_missing_lockfile_is_refused_before_npm_runs(fake_web: FakeWeb) -> None:
    (fake_web.folder / "package-lock.json").unlink()

    result = fake_web.run()

    assert result.returncode != 0
    assert "package-lock.json" in result.stderr
    assert fake_web.call_lines() == [], "without a lockfile npm would resolve versions afresh"


def test_a_missing_npm_says_which_node_is_needed(fake_web: FakeWeb) -> None:
    if shutil.which("npm", path="/usr/bin:/bin") is not None:
        pytest.skip("this machine has npm in /usr/bin")

    result = fake_web.run(PATH="/usr/bin:/bin")

    assert result.returncode != 0
    assert "npm" in result.stderr
    assert "22" in result.stderr
