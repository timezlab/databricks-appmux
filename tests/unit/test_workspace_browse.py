"""The rules of the workspace folder browser that need no HTTP (T194, admin-api.md addendum).

``GET /workspace/browse`` is tested end to end in ``tests/contract/test_workspace_browse_api.py``;
this file pins the pure parts: which paths are accepted, what the roots and parents are, how a
folder's objects become sorted, capped entries, and what the marker files say about the folder.
All emails are placeholders.
"""

from __future__ import annotations

import pytest

from appmux.admin.errors import BadRequest
from appmux.admin.workspace_browse import (
    MAX_ENTRIES,
    MAX_PATH_LENGTH,
    clean_browse_path,
    listing_of,
    parent_of,
    roots_listing,
)
from appmux.packages.store import WorkspaceObject
from appmux.reconciler.runtime_limits import NODE_MIN_MEMORY_MB

EMAIL = "alice@example.com"
HOME = f"/Workspace/Users/{EMAIL}"
SHARED = "/Workspace/Shared"


def folder(path: str) -> WorkspaceObject:
    return WorkspaceObject(path, "DIRECTORY")


def file(path: str, object_type: str = "FILE") -> WorkspaceObject:
    return WorkspaceObject(path, object_type, size_bytes=10)


def listing_of_names(
    names: list[str], *, kind: str = "FILE", at: str = f"{HOME}/sales"
) -> list[WorkspaceObject]:
    return [WorkspaceObject(f"{at}/{name}", kind) for name in names]


# --- the path -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (SHARED, SHARED),
        (f"{HOME}/sales", f"{HOME}/sales"),
        ("/Workspace/Repos/team/app", "/Workspace/Repos/team/app"),
        (f"{HOME}/sales/", f"{HOME}/sales"),  # one trailing slash is dropped
        ("/Workspace", None),  # the top of the tree: it lists Users, Shared and Repos
        ("/Workspace/", "/Workspace"),
        ("/Workspace/Users", None),  # and so does the folder that holds every person's folder
        (f"{HOME}/v1..2", f"{HOME}/v1..2"),  # two dots inside a name are a name
        (f"{HOME}/.hidden", f"{HOME}/.hidden"),
        (f"{HOME}/café ünïcode", f"{HOME}/café ünïcode"),
        ("/Workspace/" + "a" * (MAX_PATH_LENGTH - len("/Workspace/")), None),  # exactly 512
    ],
)
def test_a_path_inside_the_workspace_tree_is_accepted(raw: str, expected: str | None) -> None:
    assert clean_browse_path(raw) == (raw if expected is None else expected)


@pytest.mark.parametrize(
    "raw",
    [
        f"{HOME}/../bob@example.com",  # a '..' part
        f"{HOME}/sales/..",
        "/Workspace/..",
        "/Workspace/Shared/./x",  # a '.' part
        "/Workspace//Shared",  # an empty part
        f"{HOME}/sales//app",
        f"{HOME}/sa\nles",  # a control character
        f"{HOME}/sa\x00les",
        f"{HOME}/sa\x7fles",
        f"{HOME}/sa\x9fles",
        "/Workspace/" + "a" * (MAX_PATH_LENGTH - len("/Workspace/") + 1),  # 513 characters
        "/Users/alice@example.com",  # outside /Workspace/
        "Workspace/Shared",
        "/workspace/Shared",
        "/Workspacefoo/x",
        "/Workspace//",
        "/Volumes/main/default/files",
        "dbfs:/Workspace/Shared",
        "https://example.com/Workspace/Shared",
    ],
)
def test_a_path_outside_the_tree_or_with_a_forbidden_part_is_refused(raw: str) -> None:
    with pytest.raises(BadRequest) as caught:
        clean_browse_path(raw)

    assert caught.value.error_code == "invalid_path"
    # One fixed sentence for every refusal, so it cannot repeat what was submitted.
    with pytest.raises(BadRequest) as unrelated:
        clean_browse_path("/nope")
    assert caught.value.message == unrelated.value.message


def test_a_path_of_513_characters_is_refused_even_when_its_trailing_slash_would_be_dropped() -> (
    None
):
    raw = "/Workspace/" + "a" * (MAX_PATH_LENGTH - len("/Workspace/")) + "/"

    assert len(raw) == MAX_PATH_LENGTH + 1
    with pytest.raises(BadRequest):
        clean_browse_path(raw)


# --- the roots and the parent ---------------------------------------------------------------


def test_the_roots_listing_names_my_folder_and_shared_and_lists_nothing() -> None:
    listing = roots_listing(EMAIL)

    assert listing.path is None
    assert listing.parent is None
    assert [(root.label, root.path) for root in listing.roots] == [
        ("My folder", HOME),
        ("Shared", SHARED),
    ]
    assert listing.entries == []
    assert listing.truncated is False
    assert listing.markers == []
    assert listing.looks_like_app is False
    assert listing.runtime_hint is None
    assert listing.suggested_memory_mb is None


@pytest.mark.parametrize(
    ("path", "parent"),
    [
        (HOME, "/Workspace/Users"),  # a person's folder is below Users, like everyone else's
        (SHARED, "/Workspace"),
        (f"{HOME}/sales", HOME),
        (f"{HOME}/sales/server", f"{HOME}/sales"),
        (f"{SHARED}/team", SHARED),
        ("/Workspace/Users/bob@example.com", "/Workspace/Users"),  # another person's folder
        ("/Workspace/Users", "/Workspace"),
        ("/Workspace/Repos", "/Workspace"),
        ("/Workspace/Repos/team", "/Workspace/Repos"),
        ("/Workspace", None),  # the top: nothing above it to browse
    ],
)
def test_the_parent_is_the_folder_above_and_none_only_at_the_top(
    path: str, parent: str | None
) -> None:
    assert parent_of(path) == parent


# --- the entries ----------------------------------------------------------------------------


def test_folders_come_first_then_names_without_regard_to_case() -> None:
    at = f"{HOME}/sales"
    objects = [
        file(f"{at}/b.py"),
        folder(f"{at}/Zeta"),
        file(f"{at}/A.py"),
        folder(f"{at}/alpha"),
        file(f"{at}/a.txt"),
    ]

    listing = listing_of(at, objects)

    assert [(entry.name, entry.kind) for entry in listing.entries] == [
        ("alpha", "folder"),
        ("Zeta", "folder"),
        ("A.py", "file"),
        ("a.txt", "file"),
        ("b.py", "file"),
    ]
    assert [entry.path for entry in listing.entries][:2] == [f"{at}/alpha", f"{at}/Zeta"]


@pytest.mark.parametrize(
    ("object_type", "kind"),
    [
        ("DIRECTORY", "folder"),
        ("REPO", "folder"),
        ("FILE", "file"),
        ("NOTEBOOK", "file"),
        ("LIBRARY", "file"),
        ("DASHBOARD", "file"),
        ("SOMETHING_NEW", "file"),  # an object type this code has never heard of is not a folder
    ],
)
def test_a_directory_or_a_repo_is_a_folder_and_every_other_object_is_a_file(
    object_type: str, kind: str
) -> None:
    at = f"{HOME}/sales"

    listing = listing_of(at, [WorkspaceObject(f"{at}/thing", object_type)])

    assert [(entry.name, entry.kind) for entry in listing.entries] == [("thing", kind)]


def test_the_listing_carries_the_folder_and_its_parent_and_no_roots() -> None:
    at = f"{HOME}/sales"

    listing = listing_of(at, [])

    assert (listing.path, listing.parent, listing.roots) == (at, HOME, [])
    assert listing.entries == []
    assert listing.truncated is False


def test_an_object_that_is_not_a_path_under_the_workspace_tree_is_left_out() -> None:
    at = f"{HOME}/sales"
    objects = [file(f"{at}/app.py"), file("/Users/other/file"), file(f"{at}/"), file("")]

    listing = listing_of(at, objects)

    assert [entry.name for entry in listing.entries] == ["app.py"]


def test_an_entry_the_next_browse_would_refuse_is_not_listed() -> None:
    at = f"{HOME}/sales"
    too_long = "d" * (MAX_PATH_LENGTH - len(at))  # its path is one character over the limit
    objects = [folder(f"{at}/src"), folder(f"{at}/bad\x07name"), folder(f"{at}/{too_long}")]
    objects += [file(f"{at}/notes\x1b[31m.txt")]

    listing = listing_of(at, objects)

    assert [entry.name for entry in listing.entries] == ["src"]
    for entry in listing.entries:
        assert clean_browse_path(entry.path) == entry.path


def test_a_marker_whose_path_is_over_the_limit_still_says_what_the_folder_is() -> None:
    # The folder itself can be opened and used as a source; only the file's own path is long.
    at = "/Workspace/" + "d" * (MAX_PATH_LENGTH - len("/Workspace/") - len("/app.py"))

    listing = listing_of(at, listing_of_names(["app.yaml"], at=at))

    assert listing.entries == []
    assert listing.markers == ["app.yaml"]


def test_exactly_the_cap_is_not_truncated_and_one_more_is() -> None:
    at = f"{HOME}/sales"

    at_cap = listing_of(at, listing_of_names([f"f{n:04}.txt" for n in range(MAX_ENTRIES)], at=at))
    over = listing_of(at, listing_of_names([f"f{n:04}.txt" for n in range(MAX_ENTRIES + 1)], at=at))

    assert (len(at_cap.entries), at_cap.truncated) == (MAX_ENTRIES, False)
    assert (len(over.entries), over.truncated) == (MAX_ENTRIES, True)
    assert over.entries[-1].name == f"f{MAX_ENTRIES - 1:04}.txt"


def test_truncation_keeps_the_folders_before_any_file() -> None:
    at = f"{HOME}/sales"
    objects = listing_of_names([f"a{n:03}.txt" for n in range(150)], at=at) + listing_of_names(
        [f"z{n:03}" for n in range(60)], kind="DIRECTORY", at=at
    )

    listing = listing_of(at, objects)

    assert listing.truncated is True
    assert sum(entry.kind == "folder" for entry in listing.entries) == 60
    assert [entry.kind for entry in listing.entries[:60]] == ["folder"] * 60


# --- the markers and the hints --------------------------------------------------------------


def test_markers_are_the_known_files_in_the_contracts_order() -> None:
    at = f"{HOME}/sales"
    names = [
        "README.md",
        "pyproject.toml",
        "app.py",
        "requirements.txt",
        "databricks.yml",
        "app.yaml",
    ]

    listing = listing_of(at, listing_of_names(names, at=at))

    assert listing.markers == ["app.yaml", "databricks.yml", "requirements.txt", "pyproject.toml"]


def test_a_folder_that_has_a_marker_name_is_not_a_marker() -> None:
    at = f"{HOME}/sales"
    objects = listing_of_names(["app.yaml", "package.json"], kind="DIRECTORY", at=at)

    listing = listing_of(at, objects)

    assert listing.markers == []
    assert listing.looks_like_app is False


def test_a_marker_past_the_cap_still_counts() -> None:
    at = f"{HOME}/sales"
    objects = listing_of_names([f"a{n:04}.txt" for n in range(MAX_ENTRIES + 5)], at=at)
    objects += listing_of_names(["requirements.txt"], at=at)

    listing = listing_of(at, objects)

    assert listing.truncated is True
    assert "requirements.txt" not in [entry.name for entry in listing.entries]
    assert listing.markers == ["requirements.txt"]
    assert listing.looks_like_app is True


@pytest.mark.parametrize(
    ("names", "looks_like_app", "runtime_hint", "memory"),
    [
        ([], False, None, None),
        (["app.py", "README.md"], False, None, None),
        (["databricks.yml"], False, None, None),  # a bundle file alone is not an app
        (["app.yaml"], True, None, None),  # an app.yaml alone says nothing about the runtime
        (["requirements.txt"], True, "python", None),
        (["pyproject.toml"], False, "python", None),
        (["package.json"], True, "node", NODE_MIN_MEMORY_MB),
        (["app.yaml", "package.json"], True, "node", NODE_MIN_MEMORY_MB),
        (
            ["requirements.txt", "package.json"],
            True,
            "python",
            None,
        ),  # python wins, as on the platform
        (["pyproject.toml", "package.json"], True, "node", NODE_MIN_MEMORY_MB),
        (["app.yaml", "requirements.txt", "databricks.yml"], True, "python", None),
    ],
)
def test_the_markers_decide_looks_like_app_the_runtime_hint_and_the_memory_hint(
    names: list[str], looks_like_app: bool, runtime_hint: str | None, memory: int | None
) -> None:
    at = f"{HOME}/sales"

    listing = listing_of(at, listing_of_names(names, at=at))

    assert (listing.looks_like_app, listing.runtime_hint, listing.suggested_memory_mb) == (
        looks_like_app,
        runtime_hint,
        memory,
    )


def test_the_node_memory_hint_is_the_launchers_constant() -> None:
    assert NODE_MIN_MEMORY_MB == 768
