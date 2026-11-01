"""A source folder becomes one archive named by its sha256 (FR-003, research D5, T075).

Every test reads a fake workspace and builds the archive in ``tmp_path``; nothing touches the
network. What the digest must hold: the same files give the same digest whatever order the
workspace lists them in, a changed byte or a renamed file changes it, and nothing the workspace
says about time or owners leaks into it.
"""

from __future__ import annotations

import asyncio
import hashlib
import tarfile
from pathlib import Path

import pytest
from fake_workspace import FakeWorkspace

from appmux.reconciler.snapshot_source import (
    EMPTY_FOLDER_REASON,
    SESSION_EXPIRED_REASON,
    BuiltArchive,
    SnapshotRefused,
    build_archive,
)

pytestmark = pytest.mark.anyio

FOLDER = "/Workspace/Users/owner@example.com/sales"
TOKEN = "obo-token-example"  # noqa: S105 - an obviously fake label the fake workspace compares
OTHER_TOKEN = "other-token-example"  # noqa: S105 - same: a token the fake workspace refuses


def workspace_with(files: dict[str, bytes], **options: bool) -> FakeWorkspace:
    workspace = FakeWorkspace(required_token=TOKEN)
    workspace.reverse_listing = options.get("reverse", False)
    for relative, data in files.items():
        workspace.add_file(f"{FOLDER}/{relative}", data)
    return workspace


async def archive_of(workspace: FakeWorkspace, work_dir: Path, **limits: int) -> BuiltArchive:
    return await build_archive(
        reader=workspace, token=TOKEN, source_path=FOLDER, work_dir=work_dir, **limits
    )


def members_of(archive: BuiltArchive) -> dict[str, bytes]:
    with tarfile.open(archive.path) as handle:
        contents: dict[str, bytes] = {}
        for member in handle.getmembers():
            extracted = handle.extractfile(member)
            assert extracted is not None
            contents[member.name] = extracted.read()
        return contents


SOURCE = {
    "app.py": b"print('hi')\n",
    "app.yaml": b'command: ["python", "app.py"]\n',
    "static/site.css": b"body { margin: 0 }\n",
    "static/img/logo.txt": b"logo",
}


async def test_a_folder_becomes_one_archive_named_by_its_sha256(tmp_path: Path) -> None:
    archive = await archive_of(workspace_with(SOURCE), tmp_path)
    assert archive.digest == "sha256:" + hashlib.sha256(archive.path.read_bytes()).hexdigest()
    assert archive.file_count == len(SOURCE)
    assert members_of(archive) == SOURCE


async def test_subfolders_are_read_and_names_are_relative_to_the_source_folder(
    tmp_path: Path,
) -> None:
    archive = await archive_of(workspace_with(SOURCE), tmp_path)
    assert set(members_of(archive)) == set(SOURCE)  # not '/Workspace/Users/...'


async def test_the_size_is_the_sum_of_the_files(tmp_path: Path) -> None:
    archive = await archive_of(workspace_with(SOURCE), tmp_path)
    assert archive.size_bytes == sum(len(data) for data in SOURCE.values())


async def test_the_same_content_gives_the_same_digest_whatever_the_listing_order(
    tmp_path: Path,
) -> None:
    first = await archive_of(workspace_with(SOURCE), tmp_path / "a")
    second = await archive_of(workspace_with(SOURCE, reverse=True), tmp_path / "b")
    assert first.digest == second.digest
    assert first.path.read_bytes() == second.path.read_bytes()


async def test_the_digest_is_stable_across_runs_and_work_folders(tmp_path: Path) -> None:
    # A fixed value would only restate the format; what matters is that nothing from the run
    # (time, owner, path of the work folder) is in the archive.
    digests = {
        (await archive_of(workspace_with(SOURCE), tmp_path / f"run{index}")).digest
        for index in range(3)
    }
    assert len(digests) == 1


async def test_one_changed_byte_changes_the_digest(tmp_path: Path) -> None:
    changed = {**SOURCE, "app.py": b"print('ho')\n"}
    before = await archive_of(workspace_with(SOURCE), tmp_path / "a")
    after = await archive_of(workspace_with(changed), tmp_path / "b")
    assert before.digest != after.digest


async def test_a_renamed_file_changes_the_digest(tmp_path: Path) -> None:
    renamed = {("main.py" if name == "app.py" else name): data for name, data in SOURCE.items()}
    before = await archive_of(workspace_with(SOURCE), tmp_path / "a")
    after = await archive_of(workspace_with(renamed), tmp_path / "b")
    assert before.digest != after.digest


async def test_the_folder_name_and_its_owner_are_not_part_of_the_digest(tmp_path: Path) -> None:
    other = FakeWorkspace(required_token=TOKEN)
    for relative, data in SOURCE.items():
        other.add_file(f"/Workspace/Users/someone-else@example.com/copy/{relative}", data)
    moved = await build_archive(
        reader=other,
        token=TOKEN,
        source_path="/Workspace/Users/someone-else@example.com/copy",
        work_dir=tmp_path / "b",
    )
    original = await archive_of(workspace_with(SOURCE), tmp_path / "a")
    assert moved.digest == original.digest


async def test_a_trailing_slash_on_the_source_path_changes_nothing(tmp_path: Path) -> None:
    archive = await build_archive(
        reader=workspace_with(SOURCE),
        token=TOKEN,
        source_path=FOLDER + "/",
        work_dir=tmp_path,
    )
    assert set(members_of(archive)) == set(SOURCE)


async def test_an_empty_file_is_a_file(tmp_path: Path) -> None:
    archive = await archive_of(workspace_with({"__init__.py": b""}), tmp_path)
    assert members_of(archive) == {"__init__.py": b""}


async def test_the_archive_holds_no_ownership_time_or_permission_that_varies(
    tmp_path: Path,
) -> None:
    archive = await archive_of(workspace_with(SOURCE), tmp_path)
    with tarfile.open(archive.path) as handle:
        shapes = {(m.mtime, m.uid, m.gid, m.uname, m.gname, m.mode, m.isreg()) for m in handle}
    assert shapes == {(0, 0, 0, "", "", 0o644, True)}


# --- refusals -----------------------------------------------------------------------------------


async def test_an_empty_folder_is_refused_as_empty_or_unreadable(tmp_path: Path) -> None:
    workspace = FakeWorkspace(required_token=TOKEN)
    workspace.add_folder(FOLDER)
    with pytest.raises(SnapshotRefused) as caught:
        await archive_of(workspace, tmp_path)
    assert str(caught.value) == "empty, or you cannot read it" == EMPTY_FOLDER_REASON


async def test_a_folder_holding_only_folders_is_empty(tmp_path: Path) -> None:
    workspace = FakeWorkspace(required_token=TOKEN)
    workspace.add_folder(f"{FOLDER}/static/img")
    with pytest.raises(SnapshotRefused) as caught:
        await archive_of(workspace, tmp_path)
    assert str(caught.value) == EMPTY_FOLDER_REASON


async def test_a_caller_with_no_access_gets_the_same_refusal_as_an_empty_folder(
    tmp_path: Path,
) -> None:
    workspace = workspace_with(SOURCE)
    with pytest.raises(SnapshotRefused) as caught:
        await build_archive(
            reader=workspace, token=OTHER_TOKEN, source_path=FOLDER, work_dir=tmp_path
        )
    assert str(caught.value) == EMPTY_FOLDER_REASON


@pytest.mark.parametrize("status", [403, 404])
async def test_a_workspace_that_refuses_or_does_not_know_the_folder_gives_that_refusal_too(
    tmp_path: Path, status: int
) -> None:
    workspace = workspace_with(SOURCE)
    workspace.failing_status[FOLDER] = status
    with pytest.raises(SnapshotRefused) as caught:
        await archive_of(workspace, tmp_path)
    assert str(caught.value) == EMPTY_FOLDER_REASON


async def test_a_workspace_failure_is_refused_with_its_status_and_nothing_else(
    tmp_path: Path,
) -> None:
    workspace = workspace_with(SOURCE)
    workspace.failing_status[FOLDER] = 500
    with pytest.raises(SnapshotRefused) as caught:
        await archive_of(workspace, tmp_path)
    assert "500" in str(caught.value)
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("calls_before_expiry", [1, 2, 5])
async def test_a_session_that_ends_mid_snapshot_says_to_press_update_again(
    tmp_path: Path, calls_before_expiry: int
) -> None:
    # Expiry can strike a listing (1 and 2) or an export (5): the reason is the same.
    workspace = workspace_with(SOURCE)
    workspace.expire_after_calls = calls_before_expiry
    with pytest.raises(SnapshotRefused) as caught:
        await archive_of(workspace, tmp_path)
    assert str(caught.value) == "session expired during the snapshot; press Update again"
    assert str(caught.value) == SESSION_EXPIRED_REASON


async def test_a_file_over_the_limit_is_refused_naming_the_file(tmp_path: Path) -> None:
    # Every other file is under the 10-byte limit, so only the big one can be the one named.
    source = {"app.py": b"x", "static/site.css": b"y", "models/big.bin": b"x" * 11}
    with pytest.raises(SnapshotRefused) as caught:
        await archive_of(workspace_with(source), tmp_path, max_file_bytes=10)
    assert "models/big.bin" in str(caught.value)
    assert "10 bytes" in str(caught.value)


async def test_a_file_exactly_at_the_limit_is_accepted(tmp_path: Path) -> None:
    archive = await archive_of(workspace_with({"a.bin": b"x" * 10}), tmp_path, max_file_bytes=10)
    assert archive.file_count == 1


async def test_a_file_the_listing_says_is_over_500_mb_is_refused_before_it_is_read(
    tmp_path: Path,
) -> None:
    workspace = workspace_with({"app.py": b"x"})
    workspace.add_file(f"{FOLDER}/weights.bin", b"stand-in", claimed_size=500 * 1024 * 1024 + 1)
    with pytest.raises(SnapshotRefused) as caught:
        await archive_of(workspace, tmp_path)
    assert "weights.bin" in str(caught.value)
    assert "500 MB" in str(caught.value)
    assert [op for op, path, _ in workspace.calls if path.endswith("weights.bin")] == []


async def test_a_notebook_or_other_object_is_refused_by_name_and_type(tmp_path: Path) -> None:
    workspace = workspace_with(SOURCE)
    workspace.add_other_object(f"{FOLDER}/analysis", "NOTEBOOK")
    with pytest.raises(SnapshotRefused) as caught:
        await archive_of(workspace, tmp_path)
    assert "analysis" in str(caught.value)
    assert "NOTEBOOK" in str(caught.value)


async def test_too_many_files_are_refused_before_they_are_all_read(tmp_path: Path) -> None:
    source = {f"f{number}.txt": b"x" for number in range(6)}
    workspace = workspace_with(source)
    with pytest.raises(SnapshotRefused) as caught:
        await archive_of(workspace, tmp_path, max_file_count=5)
    assert "5 files" in str(caught.value)
    assert sum(1 for op, _, _ in workspace.calls if op == "read") < len(source)


async def test_the_work_folder_keeps_only_the_archive(tmp_path: Path) -> None:
    archive = await archive_of(workspace_with(SOURCE), tmp_path)
    left_behind = await asyncio.to_thread(lambda: list(tmp_path.iterdir()))
    assert left_behind == [archive.path]
