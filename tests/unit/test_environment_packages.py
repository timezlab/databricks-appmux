"""Is a requirements file satisfied by the parent's installed packages? (design 5.3, T090).

The environment is built from real ``.dist-info`` folders in ``tmp_path`` and read with
``importlib.metadata``, so the code under test meets the same objects it meets in production.
This module is the first rule of design 5.3 and nothing else: it says which requirement lines
the environment satisfies and lists the ones it does not. What happens to those (a wheel, the
index, or a failure that lists every one of them) is decided in ``packages/resolve.py``, which
calls it per line; ``test_package_resolution.py`` covers the failure sentence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from importlib.metadata import distributions
from pathlib import Path

import pytest

from appmux.packages.environment import Environment, check_requirements, read_environment


def make_environment(
    root: Path, packages: Mapping[str, str | tuple[str, Sequence[str]]]
) -> Environment:
    """Write one ``.dist-info`` per package: ``{"foo": "1.0"}`` or ``{"foo": ("1.0", [req])}``."""
    root.mkdir(parents=True, exist_ok=True)
    for name, spec in packages.items():
        version, requires = (spec, []) if isinstance(spec, str) else spec
        folder = root / f"{name.replace('-', '_')}-{version}.dist-info"
        folder.mkdir()
        lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
        lines += [f"Requires-Dist: {requirement}" for requirement in requires]
        (folder / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return read_environment(distributions(path=[str(root)]))


@pytest.fixture
def environment(tmp_path: Path) -> Environment:
    return make_environment(
        tmp_path / "site",
        {
            "fastapi": ("0.115.0", ['uvicorn>=0.30; extra == "standard"', "starlette<0.39"]),
            "uvicorn": "0.30.6",
            "starlette": "0.38.6",
            "My_Package": "2.1.0",
            "prerelease-pkg": "1.0rc1",
            "local-pkg": "1.2.3+acme1",
        },
    )


# --- reading the environment --------------------------------------------------------------------


def test_names_are_compared_in_their_canonical_form(environment: Environment) -> None:
    assert environment.version_of("my-package") is not None
    assert environment.version_of("My_Package") == environment.version_of("my.package")
    assert environment.version_of("not-installed") is None


def test_the_first_copy_on_the_path_is_the_one_that_imports(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    make_environment(first, {"dup": "1.0"})
    make_environment(second, {"dup": "2.0"})
    environment = read_environment(distributions(path=[str(first), str(second)]))
    assert str(environment.version_of("dup")) == "1.0"


def test_the_environment_of_this_process_is_read_by_default() -> None:
    environment = read_environment()
    assert environment.version_of("pytest") is not None
    assert check_requirements("pytest\n", environment).unresolved == ()


# --- the requirements that the environment satisfies --------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "starlette",
        "starlette==0.38.6",
        "starlette>=0.38",
        "starlette>=0.30,<0.40",
        "starlette~=0.38.0",
        "starlette!=0.37.0",
        "Starlette == 0.38.6",
        "my_package>=2",
        "MY.PACKAGE==2.1.0",
        "fastapi==0.115.*",
    ],
)
def test_a_requirement_the_environment_meets_is_satisfied(
    environment: Environment, line: str
) -> None:
    report = check_requirements(line + "\n", environment)
    assert report.unresolved == ()
    assert report.satisfied_count == 1


def test_a_pre_release_pin_is_satisfied_by_the_same_pre_release(environment: Environment) -> None:
    assert check_requirements("prerelease-pkg==1.0rc1\n", environment).unresolved == ()
    assert check_requirements("prerelease-pkg>=0.9\n", environment).unresolved == ()


def test_a_pin_to_a_local_version_is_not_met_by_the_plain_version(tmp_path: Path) -> None:
    # Design 5.3: a local version (1.2.3+acme1) is how an app forces its own build of 1.2.3.
    plain = make_environment(tmp_path / "plain", {"foo": "1.2.3"})
    report = check_requirements("foo==1.2.3+acme1\n", plain)
    assert report.unresolved == ("no wheel for 'foo==1.2.3+acme1' (the environment has foo 1.2.3)",)


def test_a_plain_pin_is_met_by_a_local_version(environment: Environment) -> None:
    assert check_requirements("local-pkg==1.2.3\n", environment).unresolved == ()


# --- what it cannot satisfy ---------------------------------------------------------------------


def test_a_version_the_environment_does_not_have_is_named_with_the_version_it_has(
    environment: Environment,
) -> None:
    report = check_requirements("uvicorn==0.31.0\n", environment)
    assert report.unresolved == (
        "no wheel for 'uvicorn==0.31.0' (the environment has uvicorn 0.30.6)",
    )
    assert report.satisfied_count == 0


def test_a_package_the_environment_lacks_says_it_has_none(environment: Environment) -> None:
    report = check_requirements("polars>=1.0\n", environment)
    assert report.unresolved == ("no wheel for 'polars>=1.0' (the environment has no polars)",)


def test_every_unresolved_requirement_is_listed_at_once(environment: Environment) -> None:
    text = "uvicorn==0.31.0\nstarlette\npolars\nduckdb==1.1.0\n"
    report = check_requirements(text, environment)
    assert [line.split("'")[1] for line in report.unresolved] == [
        "uvicorn==0.31.0",
        "polars",
        "duckdb==1.1.0",
    ]
    assert report.satisfied_count == 1
    assert report.total_count == 4


def test_every_requirement_the_environment_cannot_meet_is_listed_in_order(
    environment: Environment,
) -> None:
    failing = check_requirements("polars\nduckdb\n", environment)
    assert failing.unresolved == (
        "no wheel for 'polars' (the environment has no polars)",
        "no wheel for 'duckdb' (the environment has no duckdb)",
    )
    assert check_requirements("starlette\n", environment).unresolved == ()


def test_the_summary_counts_what_the_environment_covered(environment: Environment) -> None:
    report = check_requirements("starlette\nuvicorn\npolars\n", environment)
    assert report.summary() == "2 of 3 from the environment"


# --- the shape of a requirements file -----------------------------------------------------------


def test_comments_blank_lines_and_inline_comments_are_not_requirements(
    environment: Environment,
) -> None:
    text = "# pinned for the demo\n\nstarlette==0.38.6  # the web layer\n   \n"
    report = check_requirements(text, environment)
    assert (report.satisfied_count, report.total_count, report.unresolved) == (1, 1, ())


def test_a_hashed_multi_line_requirement_is_one_requirement(environment: Environment) -> None:
    text = (
        "starlette==0.38.6 \\\n"
        "    --hash=sha256:" + "a" * 64 + " \\\n"
        "    --hash=sha256:" + "b" * 64 + "\n"
        "uvicorn==0.30.6\n"
    )
    report = check_requirements(text, environment)
    assert (report.satisfied_count, report.total_count, report.unresolved) == (2, 2, ())


def test_crlf_line_endings_read_the_same(environment: Environment) -> None:
    report = check_requirements("starlette\r\nuvicorn\r\n", environment)
    assert (report.satisfied_count, report.unresolved) == (2, ())


def test_an_empty_file_has_no_requirements(environment: Environment) -> None:
    report = check_requirements("", environment)
    assert (report.total_count, report.satisfied_count, report.unresolved) == (0, 0, ())


def test_a_requirement_whose_marker_is_false_here_is_skipped(environment: Environment) -> None:
    text = 'polars; python_version < "3.0"\nduckdb; sys_platform == "win32"\n'
    report = check_requirements(text, environment)
    assert (report.total_count, report.unresolved, report.skipped_count) == (0, (), 2)


def test_a_requirement_whose_marker_is_true_here_is_checked(environment: Environment) -> None:
    report = check_requirements('polars; python_version >= "3.0"\n', environment)
    assert report.unresolved == ("no wheel for 'polars' (the environment has no polars)",)


# --- extras -------------------------------------------------------------------------------------


def test_an_extra_is_met_when_the_environment_has_what_the_extra_needs(
    environment: Environment,
) -> None:
    assert check_requirements("fastapi[standard]>=0.100\n", environment).unresolved == ()


def test_an_extra_the_environment_cannot_meet_fails_naming_the_extra(tmp_path: Path) -> None:
    thin = make_environment(
        tmp_path / "thin", {"fastapi": ("0.115.0", ['uvicorn>=0.30; extra == "standard"'])}
    )
    report = check_requirements("fastapi[standard]\n", thin)
    assert report.unresolved == (
        "no wheel for 'uvicorn>=0.30' (the environment has no uvicorn), "
        "needed by extra 'standard' of 'fastapi'",
    )


def test_an_extra_the_package_does_not_declare_asks_nothing_more(environment: Environment) -> None:
    assert check_requirements("fastapi[unknown-extra]\n", environment).unresolved == ()


# --- lines the environment can never satisfy ----------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "foo @ https://example.com/foo-1.0-py3-none-any.whl",
        "git+https://example.com/org/foo.git",
        "./vendor/foo-1.0-py3-none-any.whl",
        "-e .",
        "-r other-requirements.txt",
        "--requirement other-requirements.txt",
        "-c constraints.txt",
        "this is not a requirement",
        "foo==",
    ],
)
def test_a_line_that_names_no_installed_package_is_unresolved(
    environment: Environment, line: str
) -> None:
    report = check_requirements(line + "\n", environment)
    assert len(report.unresolved) == 1
    assert report.satisfied_count == 0


def test_an_index_option_is_noted_and_does_not_fail_the_release(environment: Environment) -> None:
    text = "--index-url https://index.example.com/simple\nstarlette\n"
    report = check_requirements(text, environment)
    assert report.unresolved == ()
    assert report.notes == ("ignored option: --index-url",)


def test_a_failure_line_never_holds_a_url_credential(environment: Environment) -> None:
    line = "foo @ https://user:pass-example@example.com/foo.whl\n"
    report = check_requirements(line, environment)
    assert "pass-example" not in " ".join(report.unresolved)
