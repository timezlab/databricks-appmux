"""Wheels: file names, the store by sha256, the sources and the fetch (design 5.3, T134).

The store is the real ``ContentStore`` over the in-memory workspace, so a tampered file is caught
by the code that catches it in production. A wheel source is the real ``WorkspaceWheelSource`` over
a second in-memory workspace that only the presser's token can read, or a recording fake when a
test needs the order of the calls. No test opens a socket.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Sequence
from pathlib import Path

import pytest
from fake_workspace import FakeWorkspace
from packaging.tags import Tag
from packaging.version import Version

from appmux.packages.store import (
    ContentStore,
    DigestMismatch,
    WorkspaceError,
    WorkspaceSessionExpired,
)
from appmux.packages.wheels import (
    STORE_SOURCE,
    ResolvedWheel,
    UnsafeWheelName,
    WheelRequest,
    WheelSourceError,
    WheelStore,
    WorkspaceWheelSource,
    fetch_wheels,
    parse_wheel_file,
    running_tags,
    stage_wheels,
)

pytestmark = pytest.mark.anyio

STORE_FOLDER = "/Workspace/Users/00000000-example/appmux-store"
SOURCE_FOLDER = "/Workspace/Users/dana@example.com/wheels/sales"
OTHER_FOLDER = "/Workspace/Shared/appmux/wheelhouse"
SP_TOKEN = "sp-token-example"  # noqa: S105 - an obviously fake label the fake workspace compares
PRESSER_TOKEN = "tok-test"  # noqa: S105 - an obviously fake label the fake workspace compares

# Most specific first, as `packaging.tags.sys_tags()` orders them.
TAGS = (
    Tag("cp311", "cp311", "manylinux2014_x86_64"),
    Tag("py3", "none", "any"),
)

FOO_PURE = "foo-2.0-py3-none-any.whl"
FOO_COMPILED = "foo-2.0-cp311-cp311-manylinux2014_x86_64.whl"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_for(name: str, version: str, *hashes: str, line: int = 1) -> WheelRequest:
    return WheelRequest(
        requirement=f"{name}=={version}",
        name=name,
        version=Version(version),
        sha256s=hashes,
        line_number=line,
    )


def new_store(workspace: FakeWorkspace | None = None) -> tuple[WheelStore, FakeWorkspace]:
    workspace = workspace or FakeWorkspace(required_token=SP_TOKEN)
    content = ContentStore(workspace, folder=STORE_FOLDER, token_source=lambda: SP_TOKEN)
    return WheelStore(content), workspace


def source_over(workspace: FakeWorkspace, folder: str = SOURCE_FOLDER) -> WorkspaceWheelSource:
    return WorkspaceWheelSource(workspace, folder=folder, token=PRESSER_TOKEN)


def readable_workspace(files: dict[str, bytes]) -> FakeWorkspace:
    workspace = FakeWorkspace(required_token=PRESSER_TOKEN)
    for path, data in files.items():
        workspace.add_file(path, data)
    return workspace


def fetch_calls(workspace: FakeWorkspace) -> list[str]:
    return [path for operation, path, _ in workspace.calls if operation == "read"]


class RecordingSource:
    """A wheel source with a fixed content that records what is listed and fetched, in order."""

    def __init__(self, label: str, files: dict[str, bytes], log: list[str] | None = None) -> None:
        self.label = label
        self._files = files
        self.log = log if log is not None else []

    async def list_filenames(self) -> list[str]:
        self.log.append(f"list {self.label}")
        return sorted(self._files)

    async def fetch(self, filename: str, destination: Path) -> None:
        self.log.append(f"fetch {filename}")
        await asyncio.to_thread(destination.write_bytes, self._files[filename])

    @property
    def fetched(self) -> list[str]:
        return [entry.removeprefix("fetch ") for entry in self.log if entry.startswith("fetch ")]


# --- wheel file names ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "name", "version"),
    [
        ("foo-2.0-py3-none-any.whl", "foo", "2.0"),
        (
            "My_Package-1.2.3+acme1-cp311-cp311-manylinux2014_x86_64.whl",
            "my-package",
            "1.2.3+acme1",
        ),
        ("pandas-2.2.3-1-cp311-cp311-manylinux_2_17_x86_64.whl", "pandas", "2.2.3"),
    ],
)
def test_a_wheel_file_name_gives_its_distribution_and_version(
    filename: str, name: str, version: str
) -> None:
    parsed = parse_wheel_file(filename)
    assert (parsed.filename, parsed.name, parsed.version) == (filename, name, Version(version))
    assert parsed.tags


@pytest.mark.parametrize(
    "filename",
    [
        "../foo-2.0-py3-none-any.whl",
        "sub/foo-2.0-py3-none-any.whl",
        "foo-2.0-py3-none-a/b.whl",  # `packaging` accepts a '/' in the tag part; the store must not
        "foo-2.0-py3-none-any\\..\\x.whl",
        "foo-2.0-py3-none-any.whl\x00.png",
        "foo-2.0-py3-none-any.tar.gz",
        "foo-2.0.whl",
        "..",
        "",
    ],
)
def test_a_name_that_could_leave_its_directory_or_is_not_a_wheel_is_refused(
    filename: str,
) -> None:
    with pytest.raises(UnsafeWheelName):
        parse_wheel_file(filename)


# --- the store ----------------------------------------------------------------------------------


async def test_a_wheel_added_to_the_store_is_found_and_copied_back_byte_for_byte(
    tmp_path: Path,
) -> None:
    store, workspace = new_store()
    data = b"wheel bytes of foo" * 100
    local = tmp_path / "foo.whl"
    local.write_bytes(data)
    assert not await store.has(sha256_of(data))
    await store.add(sha256_of(data), local)
    assert await store.has(sha256_of(data))
    assert list(workspace.files) == [f"{STORE_FOLDER}/{sha256_of(data)}"]  # named by its digest

    copy = tmp_path / "copy.whl"
    await store.copy_to(sha256_of(data), copy)
    assert copy.read_bytes() == data


async def test_bytes_that_do_not_match_their_name_are_discarded_and_raise_digest_mismatch(
    tmp_path: Path,
) -> None:
    store, workspace = new_store()
    wanted = sha256_of(b"the real wheel")
    workspace.add_file(f"{STORE_FOLDER}/{wanted}", b"someone edited this file")
    copy = tmp_path / "copy.whl"
    with pytest.raises(DigestMismatch):
        await store.copy_to(wanted, copy)
    assert not copy.exists()


async def test_a_store_that_does_not_verify_is_still_caught_by_the_wheel_store(
    tmp_path: Path,
) -> None:
    class CarelessFiles:
        """A content store that hands back whatever bytes it holds, without hashing them."""

        async def exists(self, digest: str) -> bool:
            return True

        async def put(self, digest: str, source: Path) -> None:
            raise AssertionError("not used")

        async def get(self, digest: str, destination: Path) -> None:
            await asyncio.to_thread(destination.write_bytes, b"other bytes")

    copy = tmp_path / "copy.whl"
    with pytest.raises(DigestMismatch):
        await WheelStore(CarelessFiles()).copy_to(sha256_of(b"the real wheel"), copy)
    assert not copy.exists()


async def test_a_file_added_under_a_digest_it_does_not_have_is_refused(tmp_path: Path) -> None:
    store, workspace = new_store()
    local = tmp_path / "foo.whl"
    local.write_bytes(b"some other bytes")
    with pytest.raises(DigestMismatch):
        await store.add(sha256_of(b"the wheel the caller meant"), local)
    assert workspace.files == {}  # nothing reached the workspace


@pytest.mark.parametrize("bad", ["", "sha256:" + "a" * 64, "A" * 64, "a" * 63, "../" + "a" * 61])
async def test_a_digest_that_is_not_64_lower_case_hex_is_refused(bad: str, tmp_path: Path) -> None:
    store, _ = new_store()
    with pytest.raises(ValueError, match="64 lower-case hex"):
        await store.has(bad)
    with pytest.raises(ValueError, match="64 lower-case hex"):
        await store.copy_to(bad, tmp_path / "x.whl")


# --- the workspace source -----------------------------------------------------------------------


async def test_a_source_lists_only_the_wheel_files_of_its_folder() -> None:
    workspace = readable_workspace(
        {
            f"{SOURCE_FOLDER}/{FOO_PURE}": b"a",
            f"{SOURCE_FOLDER}/notes.txt": b"b",
            f"{SOURCE_FOLDER}/sub/deep-1.0-py3-none-any.whl": b"c",  # a folder is not searched
        }
    )
    workspace.add_other_object(f"{SOURCE_FOLDER}/notebook-1.0-py3-none-any.whl", "NOTEBOOK")
    assert await source_over(workspace).list_filenames() == [FOO_PURE]


async def test_a_source_reads_with_the_presser_token_and_nothing_else(tmp_path: Path) -> None:
    workspace = readable_workspace({f"{SOURCE_FOLDER}/{FOO_PURE}": b"wheel"})
    source = source_over(workspace)
    await source.list_filenames()
    await source.fetch(FOO_PURE, tmp_path / "x.whl")
    assert (tmp_path / "x.whl").read_bytes() == b"wheel"
    assert {token for _, _, token in workspace.calls} == {PRESSER_TOKEN}
    assert PRESSER_TOKEN not in repr(source)


async def test_a_folder_that_is_missing_is_a_source_error() -> None:
    workspace = FakeWorkspace(required_token=PRESSER_TOKEN)
    with pytest.raises(WheelSourceError, match="could not be read"):
        await source_over(workspace).list_filenames()


async def test_a_folder_the_presser_may_not_read_lists_as_empty_as_the_workspace_answers() -> None:
    # spike 15: no access is an empty listing, never a 403: it looks like an empty folder
    private = FakeWorkspace(required_token="someone-else")  # noqa: S106 - a fake label
    private.add_file(f"{SOURCE_FOLDER}/{FOO_PURE}", b"x")
    assert await source_over(private).list_filenames() == []


async def test_a_wheel_over_the_size_limit_is_a_source_error_not_a_crash(tmp_path: Path) -> None:
    workspace = readable_workspace({f"{SOURCE_FOLDER}/{FOO_PURE}": b"0123456789"})
    source = WorkspaceWheelSource(
        workspace, folder=SOURCE_FOLDER, token=PRESSER_TOKEN, max_file_bytes=4
    )
    with pytest.raises(WheelSourceError, match="larger than"):
        await source.fetch(FOO_PURE, tmp_path / "x.whl")


async def test_a_source_refuses_to_fetch_a_name_that_climbs_out_of_its_folder(
    tmp_path: Path,
) -> None:
    workspace = readable_workspace({f"{SOURCE_FOLDER}/{FOO_PURE}": b"x"})
    with pytest.raises(UnsafeWheelName):
        await source_over(workspace).fetch("../secret-1.0-py3-none-any.whl", tmp_path / "x.whl")
    assert [op for op, _, _ in workspace.calls if op == "read"] == []


async def test_a_file_that_vanished_from_the_source_is_a_source_error(tmp_path: Path) -> None:
    workspace = readable_workspace({f"{SOURCE_FOLDER}/{FOO_PURE}": b"x"})
    with pytest.raises(WheelSourceError, match="could not be read"):
        await source_over(workspace).fetch("gone-1.0-py3-none-any.whl", tmp_path / "x.whl")


async def test_an_ended_session_is_not_hidden_as_a_missing_wheel() -> None:
    workspace = readable_workspace({f"{SOURCE_FOLDER}/{FOO_PURE}": b"x"})
    workspace.expire_after_calls = 0
    with pytest.raises(WorkspaceSessionExpired):
        await source_over(workspace).list_filenames()


async def test_another_workspace_failure_is_not_hidden_as_a_missing_wheel() -> None:
    workspace = readable_workspace({f"{SOURCE_FOLDER}/{FOO_PURE}": b"x"})
    workspace.failing_status[SOURCE_FOLDER] = 500
    with pytest.raises(WorkspaceError):
        await source_over(workspace).list_filenames()


# --- fetching the wheels a release needs --------------------------------------------------------


async def test_a_wheel_in_the_store_is_used_and_nothing_is_fetched(tmp_path: Path) -> None:
    data = b"foo wheel"
    store, workspace = new_store()
    local = tmp_path / "in.whl"
    local.write_bytes(data)
    await store.add(sha256_of(data), local)
    workspace.calls.clear()  # the set-up above wrote the file; only the fetch is measured
    source = RecordingSource("src", {FOO_PURE: data})

    result = await fetch_wheels(
        [request_for("foo", "2.0", sha256_of(data))],
        store=store,
        sources=[source],
        stored_filenames={sha256_of(data): FOO_PURE},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )

    assert [match.wheel for match in result.matches] == [
        ResolvedWheel(sha256_of(data), FOO_PURE, STORE_SOURCE)
    ]
    assert source.log == []  # not even listed
    assert [op for op, _, _ in workspace.calls if op in {"write", "read"}] == []


async def test_a_wheel_missing_from_the_store_is_fetched_matched_by_name_and_verified(
    tmp_path: Path,
) -> None:
    data = b"foo wheel"
    store, _ = new_store()
    source = RecordingSource("src", {FOO_PURE: data, "bar-1.0-py3-none-any.whl": b"bar"})
    (tmp_path / "work").mkdir()

    result = await fetch_wheels(
        [request_for("foo", "2.0", sha256_of(data))],
        store=store,
        sources=[source],
        stored_filenames={},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )

    assert result.missing == ()
    assert [match.wheel for match in result.matches] == [
        ResolvedWheel(sha256_of(data), FOO_PURE, "src")
    ]
    assert source.fetched == [FOO_PURE]  # bar was never touched
    assert await store.has(sha256_of(data))  # and it is now in the store
    assert list((tmp_path / "work").iterdir()) == []  # no partial download is left


async def test_wheels_are_fetched_one_at_a_time_in_the_order_of_the_requirements(
    tmp_path: Path,
) -> None:
    alpha, beta, gamma = b"alpha", b"beta", b"gamma"
    log: list[str] = []
    first = RecordingSource("first", {"alpha-1.0-py3-none-any.whl": alpha}, log)
    second = RecordingSource(
        "second", {"beta-1.0-py3-none-any.whl": beta, "gamma-1.0-py3-none-any.whl": gamma}, log
    )
    store, _ = new_store()
    (tmp_path / "work").mkdir()

    result = await fetch_wheels(
        [
            request_for("gamma", "1.0", sha256_of(gamma), line=1),
            request_for("alpha", "1.0", sha256_of(alpha), line=2),
            request_for("beta", "1.0", sha256_of(beta), line=3),
        ],
        store=store,
        sources=[first, second],
        stored_filenames={},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )

    assert result.missing == ()
    fetches = [entry for entry in log if entry.startswith("fetch ")]
    assert fetches == [
        "fetch gamma-1.0-py3-none-any.whl",
        "fetch alpha-1.0-py3-none-any.whl",
        "fetch beta-1.0-py3-none-any.whl",
    ]
    # each source is listed once however many wheels are looked for
    assert log.count("list first") == 1
    assert log.count("list second") == 1


async def test_sources_are_searched_in_order_and_the_first_one_holding_the_wheel_wins(
    tmp_path: Path,
) -> None:
    data = b"foo wheel"
    own = RecordingSource("wheels/ of the app", {FOO_PURE: data})
    admin = RecordingSource("wheel_sources", {FOO_PURE: data})
    store, _ = new_store()
    (tmp_path / "work").mkdir()

    result = await fetch_wheels(
        [request_for("foo", "2.0", sha256_of(data))],
        store=store,
        sources=[own, admin],
        stored_filenames={},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )

    assert result.matches[0].wheel.source == "wheels/ of the app"
    assert admin.log == []


async def test_a_file_with_the_right_name_but_other_bytes_is_not_the_wheel(
    tmp_path: Path,
) -> None:
    wanted = b"the wheel the lock names"
    source = RecordingSource("src", {FOO_PURE: b"a different file with that name"})
    store, _ = new_store()
    (tmp_path / "work").mkdir()

    result = await fetch_wheels(
        [request_for("foo", "2.0", sha256_of(wanted))],
        store=store,
        sources=[source],
        stored_filenames={},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )

    assert result.matches == ()
    (missing,) = result.missing
    assert missing.request.requirement == "foo==2.0"
    assert missing.reason.startswith(
        f"no wheel for 'foo==2.0' (sha256 {sha256_of(wanted)[:4]}…) in the wheel sources"
    )
    assert "other bytes" in missing.reason
    assert not await store.has(sha256_of(b"a different file with that name"))  # never stored
    assert list((tmp_path / "work").iterdir()) == []


async def test_the_hash_decides_among_wheels_of_one_version(tmp_path: Path) -> None:
    pure, compiled = b"pure python build", b"compiled build"
    source = RecordingSource("src", {FOO_PURE: pure, FOO_COMPILED: compiled})
    store, _ = new_store()
    (tmp_path / "work").mkdir()

    result = await fetch_wheels(
        [request_for("foo", "2.0", sha256_of(pure))],
        store=store,
        sources=[source],
        stored_filenames={},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )

    # the compiled wheel is tried first (a more specific tag), found to be other bytes, set aside
    assert source.fetched == [FOO_COMPILED, FOO_PURE]
    assert result.matches[0].wheel.filename == FOO_PURE


async def test_a_lock_that_lists_several_hashes_takes_whichever_file_is_there(
    tmp_path: Path,
) -> None:
    compiled = b"compiled build"
    source = RecordingSource("src", {FOO_COMPILED: compiled})
    store, _ = new_store()
    (tmp_path / "work").mkdir()

    result = await fetch_wheels(
        [request_for("foo", "2.0", sha256_of(b"pure python build"), sha256_of(compiled))],
        store=store,
        sources=[source],
        stored_filenames={},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )

    assert result.matches[0].wheel == ResolvedWheel(sha256_of(compiled), FOO_COMPILED, "src")


async def test_a_wheel_for_another_python_or_platform_is_not_a_candidate(tmp_path: Path) -> None:
    data = b"cp312 build"
    source = RecordingSource("src", {"foo-2.0-cp312-cp312-manylinux2014_x86_64.whl": data})
    store, _ = new_store()
    (tmp_path / "work").mkdir()

    result = await fetch_wheels(
        [request_for("foo", "2.0", sha256_of(data))],
        store=store,
        sources=[source],
        stored_filenames={},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )

    assert source.fetched == []
    (missing,) = result.missing
    assert "another Python or platform" in missing.reason


async def test_a_stored_name_counts_only_if_it_names_the_pinned_distribution(
    tmp_path: Path,
) -> None:
    data = b"numpy wheel"
    store, _ = new_store()
    local = tmp_path / "in.whl"
    local.write_bytes(data)
    await store.add(sha256_of(data), local)
    source = RecordingSource("src", {})
    (tmp_path / "work").mkdir()

    # the registry says this digest is numpy; a pin for foo==2.0 naming it is not a store hit
    result = await fetch_wheels(
        [request_for("foo", "2.0", sha256_of(data))],
        store=store,
        sources=[source],
        stored_filenames={sha256_of(data): "numpy-2.0-py3-none-any.whl"},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )

    assert result.matches == ()
    assert len(result.missing) == 1


async def test_a_digest_the_registry_names_but_the_store_lost_is_fetched_again(
    tmp_path: Path,
) -> None:
    data = b"foo wheel"
    store, _ = new_store()  # empty
    source = RecordingSource("src", {FOO_PURE: data})
    (tmp_path / "work").mkdir()

    result = await fetch_wheels(
        [request_for("foo", "2.0", sha256_of(data))],
        store=store,
        sources=[source],
        stored_filenames={sha256_of(data): FOO_PURE},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )

    assert result.matches[0].wheel.source == "src"
    assert await store.has(sha256_of(data))


async def test_one_wheel_named_by_two_requirements_is_fetched_once(tmp_path: Path) -> None:
    data = b"foo wheel"
    source = RecordingSource("src", {FOO_PURE: data})
    store, _ = new_store()
    (tmp_path / "work").mkdir()

    result = await fetch_wheels(
        [
            request_for("foo", "2.0", sha256_of(data), line=1),
            request_for("foo", "2.0", sha256_of(data), line=7),
        ],
        store=store,
        sources=[source],
        stored_filenames={},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )

    assert source.fetched == [FOO_PURE]
    assert len(result.matches) == 2
    assert result.wheels == (result.matches[0].wheel,)


async def test_an_unreadable_source_is_named_and_the_search_goes_on(tmp_path: Path) -> None:
    data = b"foo wheel"
    gone = FakeWorkspace(required_token=PRESSER_TOKEN)  # the folder does not exist
    present = readable_workspace({f"{OTHER_FOLDER}/{FOO_PURE}": data})
    store, _ = new_store()
    (tmp_path / "work").mkdir()

    found = await fetch_wheels(
        [request_for("foo", "2.0", sha256_of(data))],
        store=store,
        sources=[source_over(gone), source_over(present, OTHER_FOLDER)],
        stored_filenames={},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )
    assert found.matches[0].wheel.source == OTHER_FOLDER

    lost = await fetch_wheels(
        [request_for("foo", "2.0", sha256_of(b"nowhere"))],
        store=store,
        sources=[source_over(gone)],
        stored_filenames={},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )
    (missing,) = lost.missing
    assert f"'{SOURCE_FOLDER}' could not be read" in missing.reason


async def test_a_source_that_cannot_be_listed_for_a_reason_other_than_access_stops_the_fetch(
    tmp_path: Path,
) -> None:
    workspace = readable_workspace({f"{SOURCE_FOLDER}/{FOO_PURE}": b"x"})
    workspace.failing_status[SOURCE_FOLDER] = 503
    (tmp_path / "work").mkdir()
    store, _ = new_store()
    with pytest.raises(WorkspaceError):
        await fetch_wheels(
            [request_for("foo", "2.0", sha256_of(b"x"))],
            store=store,
            sources=[source_over(workspace)],
            stored_filenames={},
            work_dir=tmp_path / "work",
            supported_tags=TAGS,
        )


async def test_with_no_sources_the_message_still_names_the_hash(tmp_path: Path) -> None:
    store, _ = new_store()
    (tmp_path / "work").mkdir()
    result = await fetch_wheels(
        [request_for("foo", "2.0", sha256_of(b"x"))],
        store=store,
        sources=[],
        stored_filenames={},
        work_dir=tmp_path / "work",
        supported_tags=TAGS,
    )
    (missing,) = result.missing
    assert missing.reason == (
        f"no wheel for 'foo==2.0' (sha256 {sha256_of(b'x')[:4]}…) in the wheel sources"
    )


# --- assembling the wheel directory for a build -------------------------------------------------


def wheel_entries(*named: tuple[str, bytes]) -> list[ResolvedWheel]:
    return [ResolvedWheel(sha256_of(data), filename, "src") for filename, data in named]


async def put_all(store: WheelStore, tmp_path: Path, *named: tuple[str, bytes]) -> None:
    for index, (_, data) in enumerate(named):
        local = tmp_path / f"upload-{index}"
        local.write_bytes(data)
        await store.add(sha256_of(data), local)


async def test_the_wheels_of_a_release_are_copied_from_the_store_under_their_file_names(
    tmp_path: Path,
) -> None:
    named: tuple[tuple[str, bytes], ...] = (
        ("alpha-1.0-py3-none-any.whl", b"alpha"),
        ("beta-1.0-py3-none-any.whl", b"beta"),
    )
    store, workspace = new_store()
    await put_all(store, tmp_path, *named)
    directory = tmp_path / "wheels"
    directory.mkdir()
    workspace.calls.clear()

    await stage_wheels(store, wheel_entries(*named), directory)

    assert {path.name: path.read_bytes() for path in directory.iterdir()} == dict(named)
    # one file at a time, in the order of the list (workspace rate limits)
    reads = [path.rsplit("/", 1)[1] for op, path, _ in workspace.calls if op == "read"]
    assert reads == [sha256_of(data) for _, data in named]


async def test_a_tampered_store_file_stops_the_staging_with_digest_mismatch(
    tmp_path: Path,
) -> None:
    named = (("alpha-1.0-py3-none-any.whl", b"alpha"),)
    store, workspace = new_store()
    await put_all(store, tmp_path, *named)
    workspace.add_file(f"{STORE_FOLDER}/{sha256_of(b'alpha')}", b"evil")
    directory = tmp_path / "wheels"
    directory.mkdir()

    with pytest.raises(DigestMismatch):
        await stage_wheels(store, wheel_entries(*named), directory)
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize(
    "filename", ["../alpha-1.0-py3-none-any.whl", "/etc/alpha-1.0-py3-none-any.whl", "x.txt"]
)
async def test_staging_refuses_a_file_name_outside_the_directory(
    filename: str, tmp_path: Path
) -> None:
    store, _ = new_store()
    directory = tmp_path / "wheels"
    directory.mkdir()
    entry = ResolvedWheel(sha256_of(b"alpha"), filename, "src")
    with pytest.raises(UnsafeWheelName):
        await stage_wheels(store, [entry], directory)
    assert [entry.name for entry in os.scandir(tmp_path)] == [directory.name]


async def test_two_wheels_with_one_file_name_are_refused_not_overwritten(tmp_path: Path) -> None:
    named = (("alpha-1.0-py3-none-any.whl", b"one"),)
    store, _ = new_store()
    await put_all(store, tmp_path, *named)
    directory = tmp_path / "wheels"
    directory.mkdir()
    clash = ResolvedWheel(sha256_of(b"two"), "alpha-1.0-py3-none-any.whl", "src")
    with pytest.raises(UnsafeWheelName, match="share the file name"):
        await stage_wheels(store, [*wheel_entries(*named), clash], directory)


def test_the_running_tags_are_the_interpreters_and_include_pure_python() -> None:
    tags: Sequence[Tag] = running_tags()
    assert tags
    assert Tag("py3", "none", "any") in tags
