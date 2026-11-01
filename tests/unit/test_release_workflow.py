"""``.github/workflows/release.yml``: the release workflow builds the zip and publishes it (T184).

The workflow cannot run in this repository's tests, so two kinds of check stand in for a run:

* its structure is read from the YAML (when it triggers, what each job may write, that every action
  is pinned by commit, that no Databricks credential reaches it, and in what order the build
  steps run);
* the three steps that decide something (the tag must equal the package version, the publishing
  job must refuse while ADR 0015 is not accepted, and the release notes must name the pinned
  ``slirp4netns``) are cut out of the YAML and run in a temporary tree, so the shell the runner
  will execute is the shell tested here.

Nothing here touches the network, GitHub or a Databricks workspace.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"

ADR_PATH = "docs/decisions/0015-bundle-slirp4netns-in-the-release-zip-with-its-source.md"
FETCH_SCRIPT_PATH = "scripts/fetch-slirp4netns.sh"

COMMIT_PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
# What the runner runs for ``shell: bash``; the tests run the same flags.
RUNNER_BASH = ["bash", "--noprofile", "--norc", "-eo", "pipefail"]


def load_workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the bare key ``on`` as the boolean True.
    value = next(value for key, value in workflow.items() if key in ("on", True))
    assert isinstance(value, dict)
    return value


def steps_of(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    steps = workflow["jobs"][job]["steps"]
    assert isinstance(steps, list)
    return steps


def step_by_id(workflow: dict[str, Any], job: str, step_id: str) -> dict[str, Any]:
    matches = [step for step in steps_of(workflow, job) if step.get("id") == step_id]
    assert len(matches) == 1, f"job {job!r} needs exactly one step with id {step_id!r}"
    return matches[0]


def position_of(steps: list[dict[str, Any]], command: str) -> int:
    for index, step in enumerate(steps):
        if command in str(step.get("run", "")):
            return index
    raise AssertionError(f"no step runs {command!r}")


def all_steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def run_step(
    step: dict[str, Any], cwd: Path, **environment: str
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"], "HOME": str(cwd), "RUNNER_TEMP": str(cwd), **environment}
    return subprocess.run(
        [*RUNNER_BASH, "-c", step["run"]],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


# ---- when it runs and what it may do ----


def test_the_workflow_file_exists_and_parses() -> None:
    assert WORKFLOW_PATH.is_file()
    assert set(load_workflow()["jobs"]) == {"build", "release"}


def test_it_runs_on_a_version_tag_and_by_hand_and_nothing_else() -> None:
    on = triggers(load_workflow())

    assert set(on) == {"push", "workflow_dispatch"}
    assert on["push"] == {"tags": ["v*"]}, (
        "a branch push or a pull request must not build a release"
    )


def test_the_workflow_grants_nothing_and_only_the_release_job_can_write() -> None:
    workflow = load_workflow()

    assert workflow["permissions"] == {}, "the default token must start with no permission"
    assert workflow["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["release"]["permissions"] == {"contents": "write"}


def test_every_action_is_pinned_by_commit() -> None:
    uses = [step["uses"] for step in all_steps(load_workflow()) if "uses" in step]

    assert uses, "the workflow uses no action at all"
    unpinned = [action for action in uses if not COMMIT_PINNED_ACTION.match(action)]
    assert not unpinned, f"pin by 40-character commit, not by tag or branch: {unpinned}"


def test_the_checkouts_do_not_keep_the_token_in_the_git_config() -> None:
    checkouts = [
        step
        for step in all_steps(load_workflow())
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]

    assert checkouts
    assert all(step["with"]["persist-credentials"] is False for step in checkouts)


def test_it_builds_and_never_deploys_so_no_databricks_credential_reaches_it() -> None:
    # The parsed workflow, so a comment that explains the rule does not trip it.
    text = json.dumps(load_workflow())

    assert "databricks" not in text.lower()
    secrets_used = set(re.findall(r"secrets\.(\w+)", text))
    assert secrets_used <= {"GITHUB_TOKEN"}, f"unexpected secrets: {secrets_used}"


def test_the_test_only_loopback_switch_of_the_wheel_vendoring_is_never_named() -> None:
    assert "APPMUX_VENDOR_ALLOW_LOOPBACK_HTTP_FOR_TESTS" not in WORKFLOW_PATH.read_text(
        encoding="utf-8"
    )


def test_no_run_script_interpolates_a_github_expression() -> None:
    # ``${{ github.ref_name }}`` pasted into a script is shell injection through a tag name; a
    # value reaches a script only through ``env:``.
    interpolated = [
        step.get("name", step.get("id"))
        for step in all_steps(load_workflow())
        if "${{" in str(step.get("run", ""))
    ]

    assert not interpolated, f"pass these through env: {interpolated}"


def test_every_run_step_uses_the_strict_bash_the_tests_use() -> None:
    workflow = load_workflow()

    assert workflow["defaults"]["run"]["shell"] == "bash"


# ---- the build job ----


def test_the_build_job_runs_the_gates_then_the_builders_then_packs_in_that_order() -> None:
    steps = steps_of(load_workflow(), "build")

    order = [
        position_of(steps, "ruff format --check"),
        position_of(steps, "ruff check"),
        position_of(steps, "mypy src"),
        position_of(steps, "pytest tests/unit tests/contract"),
        position_of(steps, "scripts/vendor-wheels.sh"),
        position_of(steps, "scripts/fetch-slirp4netns.sh"),
        position_of(steps, "web/build.sh"),
        position_of(steps, "scripts/assemble-deploy.sh --zip"),
        position_of(steps, "unzip -l"),
    ]
    assert order == sorted(order)
    assert len(set(order)) == len(order)


def test_the_wheels_are_vendored_with_the_python_that_has_the_projects_packages() -> None:
    steps = steps_of(load_workflow(), "build")
    vendor = steps[position_of(steps, "scripts/vendor-wheels.sh")]

    assert vendor["env"]["APPMUX_PYTHON"].endswith("/.venv/bin/python")


def test_the_build_installs_from_the_lock_and_pins_its_tools() -> None:
    workflow = load_workflow()
    steps = steps_of(workflow, "build")
    sync = steps[position_of(steps, "uv sync")]
    install = steps[position_of(steps, "pip install")]
    python = next(s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python@"))
    node = next(s for s in steps if str(s.get("uses", "")).startswith("actions/setup-node@"))

    assert "--frozen" in sync["run"], "an unlocked sync would resolve versions on the runner"
    assert re.search(r"uv==\d+\.\d+\.\d+", install["run"])
    assert str(python["with"]["python-version"]) == "3.11"
    assert str(node["with"]["node-version"]).startswith("22")
    assert workflow["jobs"]["build"]["runs-on"] != "ubuntu-latest"


def test_the_zip_and_its_checksum_are_uploaded_for_the_release_job_and_for_a_dry_run() -> None:
    steps = steps_of(load_workflow(), "build")
    upload = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact@"))
    paths = str(upload["with"]["path"])

    assert "build/appmux-*.zip" in paths
    assert "build/appmux-*.zip.sha256" in paths
    assert upload["with"]["if-no-files-found"] == "error"
    assert steps.index(upload) == len(steps) - 1, "upload only what the checks accepted"


# ---- the release job ----


def test_only_a_pushed_tag_publishes_a_dry_run_stops_after_the_build() -> None:
    release = load_workflow()["jobs"]["release"]

    assert release["needs"] == "build"
    assert release["if"] == "github.event_name == 'push'"


def test_the_publishing_step_comes_after_the_adr_gate_and_the_checksum_check() -> None:
    steps = steps_of(load_workflow(), "release")

    gate = steps.index(step_by_id(load_workflow(), "release", "adr-gate"))
    publish = position_of(steps, "gh release create")
    checksum = position_of(steps, "sha256sum -c")
    assert gate < checksum < publish


def test_the_publish_step_attaches_the_zip_and_checksum_with_the_notes() -> None:
    steps = steps_of(load_workflow(), "release")
    publish = steps[position_of(steps, "gh release create")]

    assert "appmux-*.zip" in publish["run"]
    assert "appmux-*.zip.sha256" in publish["run"]
    assert "--notes-file" in publish["run"]
    assert "secrets.GITHUB_TOKEN" in publish["env"]["GH_TOKEN"]


# ---- the steps that decide, run for real ----


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "appmux").mkdir(parents=True)
    (tmp_path / "src" / "appmux" / "__init__.py").write_text(
        '"""appmux."""\n\n__version__ = "1.2.3"\n', encoding="utf-8"
    )
    (tmp_path / "docs" / "decisions").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / FETCH_SCRIPT_PATH).write_text(
        'readonly SLIRP4NETNS_RELEASE="v9.8.7"\nreadonly LIBSLIRP_RELEASE="v4.9.5"\n',
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.parametrize("tag", ["v1.2.3"])
def test_a_tag_equal_to_the_package_version_passes(tree: Path, tag: str) -> None:
    step = step_by_id(load_workflow(), "build", "check-tag")

    result = run_step(step, tree, TAG=tag)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("tag", ["v1.2.4", "1.2.3", "v1.2.3-rc1", "v1.2.3; echo pwned", "v"])
def test_any_other_tag_is_refused_and_says_what_to_change(tree: Path, tag: str) -> None:
    step = step_by_id(load_workflow(), "build", "check-tag")

    result = run_step(step, tree, TAG=tag)

    assert result.returncode != 0
    assert "1.2.3" in result.stderr, "the message names the version the tag must match"
    assert "__version__" in result.stderr


def test_the_tag_check_runs_only_for_a_pushed_tag() -> None:
    step = step_by_id(load_workflow(), "build", "check-tag")

    assert step["if"] == "github.event_name == 'push'"


def test_the_tag_check_refuses_a_package_without_a_readable_version(tree: Path) -> None:
    (tree / "src" / "appmux" / "__init__.py").write_text('"""appmux."""\n', encoding="utf-8")
    step = step_by_id(load_workflow(), "build", "check-tag")

    result = run_step(step, tree, TAG="v1.2.3")

    assert result.returncode != 0
    assert "__version__" in result.stderr


@pytest.mark.parametrize(
    "status_line",
    [
        "**Status:** accepted",
        "**Status:** accepted — the owner accepted the duty on 2026-11-01",
    ],
)
def test_the_gate_opens_only_when_the_adr_is_accepted(tree: Path, status_line: str) -> None:
    (tree / ADR_PATH).write_text(f"# Bundle\n\n{status_line}\n", encoding="utf-8")
    step = step_by_id(load_workflow(), "release", "adr-gate")

    result = run_step(step, tree)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "status_line",
    [
        "**Status:** proposed",
        "**Status:** proposed — to be accepted before the first release",
        "**Status:** superseded by ADR 0099, accepted on 2026-11-01",
        "**Status:** not accepted",
        "Status: accepted",
    ],
)
def test_the_gate_refuses_every_other_status_and_says_what_to_do(
    tree: Path, status_line: str
) -> None:
    (tree / ADR_PATH).write_text(
        f"# Bundle\n\n{status_line}\n\nThe owner has accepted this ADR before the first tag.\n",
        encoding="utf-8",
    )
    step = step_by_id(load_workflow(), "release", "adr-gate")

    result = run_step(step, tree)

    assert result.returncode != 0
    assert "0015" in result.stderr
    assert "accepted" in result.stderr


def test_the_gate_refuses_when_the_adr_file_is_missing(tree: Path) -> None:
    step = step_by_id(load_workflow(), "release", "adr-gate")

    result = run_step(step, tree)

    assert result.returncode != 0
    assert "0015" in result.stderr


def test_the_release_notes_name_the_pinned_slirp4netns_and_its_licence(tree: Path) -> None:
    step = step_by_id(load_workflow(), "release", "notes")

    result = run_step(step, tree, TAG="v1.2.3")

    assert result.returncode == 0, result.stderr
    notes = (tree / "release-notes.md").read_text(encoding="utf-8")
    assert "slirp4netns v9.8.7" in notes, "the version comes from the fetch script, not a copy"
    assert "GPL-2.0-or-later" in notes
    assert "LGPL" in notes, "the notes name the LGPL libraries inside the binary"
    assert "written offer" in notes.lower()
    assert "source" in notes.lower()
    assert "0015" in notes
    assert "v1.2.3" in notes


def test_the_release_notes_are_refused_when_the_pinned_version_cannot_be_read(tree: Path) -> None:
    (tree / FETCH_SCRIPT_PATH).write_text("# no version here\n", encoding="utf-8")
    step = step_by_id(load_workflow(), "release", "notes")

    result = run_step(step, tree, TAG="v1.2.3")

    assert result.returncode != 0
    assert "SLIRP4NETNS_RELEASE" in result.stderr
    assert not (tree / "release-notes.md").exists()


def test_the_publish_step_reads_the_notes_the_notes_step_wrote() -> None:
    steps = steps_of(load_workflow(), "release")
    publish = steps[position_of(steps, "gh release create")]

    assert "RUNNER_TEMP}/release-notes.md" in publish["run"]
    assert "RUNNER_TEMP}/release-notes.md" in step_by_id(load_workflow(), "release", "notes")["run"]
