"""The ``app.yaml`` parser: ``command`` and ``env`` only, and the manifest it stores (FR-033, T074).

The parser is pure. A snapshot is given to it as the names of its root files and a way to read one
of them, so these tests need no archive and no directory, except the ones that run it over the
fixture apps of ``tests/fixtures``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from appmux.appyaml.appyaml import (
    MAX_APP_YAML_BYTES,
    AppYamlInvalid,
    read_manifest,
    substitute_command_arguments,
)
from appmux.registry.models import DetectedManifest, EnvEntry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def manifest_of(
    files: Mapping[str, str | bytes] | Iterable[str] = (),
) -> DetectedManifest:
    """Run the parser over root files given as ``{name: content}`` (or names with empty content)."""
    contents = (
        dict.fromkeys(files, b"")
        if not isinstance(files, Mapping)
        else {
            name: data.encode() if isinstance(data, str) else data for name, data in files.items()
        }
    )
    return read_manifest(contents.keys(), lambda name: contents[name])


def with_app_yaml(text: str, **others: str) -> DetectedManifest:
    return manifest_of({"app.yaml": text, **others})


def refusal(text: str, **others: str) -> str:
    with pytest.raises(AppYamlInvalid) as caught:
        with_app_yaml(text, **others)
    return str(caught.value)


# --- the file itself ----------------------------------------------------------------------------


def test_command_and_env_become_the_manifest() -> None:
    manifest = with_app_yaml(
        """
command: ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
env:
  - name: LOG_LEVEL
    value: info
  - name: OPENAI_API_KEY
    valueFrom: openai-api-key
"""
    )
    assert manifest.command == [
        "uvicorn",
        "app:app",
        "--host",
        "0.0.0.0",  # noqa: S104 - sample command text, nothing binds to it
        "--port",
        "8000",
    ]
    assert manifest.env == [
        EnvEntry(name="LOG_LEVEL", value="info"),
        EnvEntry(name="OPENAI_API_KEY", value_from="openai-api-key"),
    ]
    assert manifest.warnings == []


def test_the_manifest_is_the_shape_the_release_row_stores() -> None:
    manifest = with_app_yaml(
        'command: ["python", "app.py"]\n'
        'env:\n  - {name: A, value: "1"}\n  - {name: B, valueFrom: k}\n'
        "foo: bar\n",
        **{"requirements.txt": "fastapi\n"},
    )
    stored = json.loads(manifest.model_dump_json())
    assert stored["env"] == [
        {"name": "A", "value": "1", "value_from": None},
        {"name": "B", "value": None, "value_from": "k"},
    ]
    assert DetectedManifest.model_validate(stored) == manifest


def test_no_app_yaml_is_a_manifest_with_no_command_and_no_env() -> None:
    manifest = manifest_of(["app.py", "requirements.txt"])
    assert (manifest.command, manifest.env, manifest.warnings) == ([], [], [])
    assert manifest.runtime == "python"


@pytest.mark.parametrize("text", ["", "# only a comment\n", "---\n", "{}\n"])
def test_an_empty_app_yaml_is_a_manifest_with_no_command_and_no_env(text: str) -> None:
    manifest = with_app_yaml(text)
    assert (manifest.command, manifest.env, manifest.warnings) == ([], [], [])


def test_app_yml_alone_is_read_like_app_yaml() -> None:
    manifest = manifest_of({"app.yml": 'command: ["python", "other.py"]\n'})
    assert manifest.command == ["python", "other.py"]


def test_both_file_names_refuse_the_update() -> None:
    with pytest.raises(AppYamlInvalid) as caught:
        manifest_of({"app.yaml": 'command: ["a"]\n', "app.yml": 'command: ["b"]\n'})
    assert "app.yaml" in str(caught.value)
    assert "app.yml" in str(caught.value)


def test_the_invalid_fixture_snapshot_is_refused_for_having_both_files() -> None:
    folder = FIXTURES / "appyaml_invalid"
    assert (folder / "app.yaml").exists()
    assert (folder / "app.yml").exists()
    with pytest.raises(AppYamlInvalid):
        read_manifest([p.name for p in folder.iterdir()], lambda n: (folder / n).read_bytes())


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ("{name: A, value: x, valueFrom: k}", "both"),
        ("{name: A}", "neither"),
    ],
)
def test_an_env_entry_needs_exactly_one_of_value_and_value_from(entry: str, problem: str) -> None:
    message = refusal(f"env:\n  - {entry}\n")
    assert "'A'" in message
    assert "value" in message
    assert "valueFrom" in message
    assert problem in message


def test_the_missing_resource_fixture_parses_and_keeps_both_resource_keys() -> None:
    folder = FIXTURES / "appyaml_missing_resource"
    manifest = read_manifest(
        [p.name for p in folder.iterdir()], lambda n: (folder / n).read_bytes()
    )
    assert {entry.value_from for entry in manifest.env if entry.value_from} == {
        "openai-api-key",
        "sql-warehouse",
    }
    assert manifest.runtime == "python"


# --- unknown keys -------------------------------------------------------------------------------


def test_an_unknown_key_warns_and_is_ignored() -> None:
    manifest = with_app_yaml(
        'command: ["python", "app.py"]\nresources:\n  - name: x\nlicense: MIT\n'
    )
    assert manifest.command == ["python", "app.py"]
    assert manifest.warnings == [
        "app.yaml: unknown key 'resources' ignored",
        "app.yaml: unknown key 'license' ignored",
    ]


def test_the_warning_names_the_file_that_was_read() -> None:
    manifest = manifest_of({"app.yml": "scale: 3\n"})
    assert manifest.warnings == ["app.yml: unknown key 'scale' ignored"]


def test_an_unknown_key_of_an_env_entry_warns_with_the_entry_named() -> None:
    manifest = with_app_yaml("env:\n  - {name: A, value: x, description: hi}\n")
    assert manifest.env == [EnvEntry(name="A", value="x")]
    assert manifest.warnings == ["app.yaml: env entry 'A' has unknown key 'description' ignored"]


# --- values are text ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "text"),
    [
        ("8080", "8080"),
        ("true", "true"),
        ("no", "no"),
        ("0.10", "0.10"),
        ("007", "007"),
        ("null", "null"),
        ("~", "~"),
        ('""', ""),
        ("'quoted text'", "quoted text"),
    ],
)
def test_an_env_value_is_the_text_written_never_a_yaml_number_or_boolean(
    written: str, text: str
) -> None:
    manifest = with_app_yaml(f"env:\n  - name: A\n    value: {written}\n")
    assert manifest.env == [EnvEntry(name="A", value=text)]


def test_a_command_argument_is_the_text_written() -> None:
    manifest = with_app_yaml("command: [python, app.py, --port, 8000, --debug, true]\n")
    assert manifest.command == ["python", "app.py", "--port", "8000", "--debug", "true"]


def test_a_multiline_value_keeps_its_text() -> None:
    manifest = with_app_yaml("env:\n  - name: A\n    value: |\n      line1\n      line2\n")
    assert manifest.env == [EnvEntry(name="A", value="line1\nline2\n")]


# --- invalid files ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "command: [unclosed\n",
        "- just\n- a list\n",
        "plain text\n",
        "command: python app.py\n",
        "command: []\n",
        "command: [python, {a: b}]\n",
        "command: [python, [nested]]\n",
        "command: ['', app.py]\n",
        'command: ["python", "a\\x00b"]\n',
        "env: {A: b}\n",
        "env: [plain]\n",
        "env:\n  - name: A\n    value: [x]\n",
        "env:\n  - value: x\n",
        "env:\n  - {name: '', value: x}\n",
        "env:\n  - {name: 'A=B', value: x}\n",
        "env:\n  - {name: A, valueFrom: ''}\n",
        "command: [a]\ncommand: [b]\n",
        "env:\n  - {name: A, value: x, value: y}\n",
        "command: !!python/object/apply:os.system ['echo hi']\n",
    ],
)
def test_an_invalid_app_yaml_refuses_the_update(text: str) -> None:
    assert refusal(text)


def test_an_alias_cannot_expand_into_a_large_structure() -> None:
    laughs = "a: &a [x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
    manifest = with_app_yaml(laughs + 'command: ["python", "app.py"]\n')
    assert manifest.command == ["python", "app.py"]
    assert refusal("command: &c [*c]\n")


def test_a_deeply_nested_app_yaml_is_refused_not_a_server_error() -> None:
    # 60 000 bytes of brackets are under the size limit and exhaust the parser's stack (T182).
    assert refusal("[" * 30000 + "]" * 30000)


def test_a_refusal_never_echoes_a_value() -> None:
    message = refusal("env:\n  - {name: SECRET_NAME, value: hunter2-not-a-secret, valueFrom: k}\n")
    assert "hunter2-not-a-secret" not in message
    assert "SECRET_NAME" in message


def test_a_file_that_is_not_utf8_is_refused() -> None:
    with pytest.raises(AppYamlInvalid):
        manifest_of({"app.yaml": b"command: [\xff\xfe]\n"})


def test_a_very_large_file_is_refused_before_it_is_parsed() -> None:
    with pytest.raises(AppYamlInvalid) as caught:
        manifest_of({"app.yaml": b"#" * (MAX_APP_YAML_BYTES + 1)})
    assert str(MAX_APP_YAML_BYTES) in str(caught.value)


# --- runtime ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("root_files", "runtime"),
    [
        (["requirements.txt"], "python"),
        (["package.json"], "node"),
        (["pyproject.toml", "uv.lock"], "python"),
        (["pyproject.toml"], None),
        (["uv.lock"], None),
        (["app.py"], None),
        ([], None),
    ],
)
def test_runtime_comes_from_the_marker_files(root_files: list[str], runtime: str | None) -> None:
    assert manifest_of(root_files).runtime == runtime


def test_both_markers_present_is_python() -> None:
    # A Python app with a front-end build ships a package.json too; the reverse is rarer.
    assert manifest_of(["package.json", "requirements.txt"]).runtime == "python"


def test_a_command_never_decides_the_runtime() -> None:
    # No marker file: the runtime stays unknown and the update is not refused (the command is
    # run as written; appmux invents no default start command).
    manifest = manifest_of({"app.yaml": 'command: ["python", "app.py"]\n', "app.py": ""})
    assert manifest.runtime is None
    assert manifest.command == ["python", "app.py"]


def test_the_absolute_links_fixture_keeps_its_command() -> None:
    folder = FIXTURES / "absolute_links"
    manifest = read_manifest(
        [p.name for p in folder.iterdir()], lambda n: (folder / n).read_bytes()
    )
    assert manifest.command == ["python", "app.py"]


# --- $NAME in command arguments -----------------------------------------------------------------

ENVIRONMENT = {"DATABRICKS_APP_PORT": "8000", "APPMUX_BASE_PATH": "/sales", "EMPTY": ""}


def test_dollar_name_is_replaced_in_a_command_argument() -> None:
    command = ["python", "app.py", "--port", "$DATABRICKS_APP_PORT"]
    assert substitute_command_arguments(command, ENVIRONMENT) == [
        "python",
        "app.py",
        "--port",
        "8000",
    ]


def test_dollar_name_is_replaced_inside_an_argument_too() -> None:
    assert substitute_command_arguments(["--bind=0.0.0.0:$DATABRICKS_APP_PORT"], ENVIRONMENT) == [
        "--bind=0.0.0.0:8000"
    ]


def test_braced_names_and_lone_dollars_are_left_as_written() -> None:
    command = ["${DATABRICKS_APP_PORT}", "$", "$1", "a$", "$$DATABRICKS_APP_PORT"]
    assert substitute_command_arguments(command, ENVIRONMENT) == [
        "${DATABRICKS_APP_PORT}",
        "$",
        "$1",
        "a$",
        "$8000",
    ]


def test_an_unknown_name_is_left_as_written() -> None:
    assert substitute_command_arguments(["$NOT_SET", "x$NOT_SET"], ENVIRONMENT) == [
        "$NOT_SET",
        "x$NOT_SET",
    ]


def test_an_empty_value_replaces_the_name_with_nothing() -> None:
    assert substitute_command_arguments(["--x=$EMPTY"], ENVIRONMENT) == ["--x="]


def test_a_substituted_value_is_not_expanded_again() -> None:
    environment = {"A": "$B", "B": "never"}
    assert substitute_command_arguments(["$A"], environment) == ["$B"]


def test_a_longer_name_is_not_cut_short() -> None:
    environment = {"PORT": "1", "PORT_TLS": "2"}
    assert substitute_command_arguments(["$PORT_TLS", "$PORT"], environment) == ["2", "1"]


def test_env_values_of_the_manifest_are_never_substituted() -> None:
    manifest = with_app_yaml(
        "command: [python, app.py, $DATABRICKS_APP_PORT]\n"
        "env:\n"
        "  - {name: A, value: '$DATABRICKS_APP_PORT'}\n"
        "  - {name: B, value: '${DATABRICKS_APP_PORT}'}\n"
    )
    assert manifest.env == [
        EnvEntry(name="A", value="$DATABRICKS_APP_PORT"),
        EnvEntry(name="B", value="${DATABRICKS_APP_PORT}"),
    ]
    assert manifest.command[-1] == "$DATABRICKS_APP_PORT"  # the manifest stores the text as written
    assert substitute_command_arguments(manifest.command, ENVIRONMENT)[-1] == "8000"


def test_the_input_command_is_not_changed() -> None:
    command = ["$DATABRICKS_APP_PORT"]
    substitute_command_arguments(command, ENVIRONMENT)
    assert command == ["$DATABRICKS_APP_PORT"]
