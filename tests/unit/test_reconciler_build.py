"""The build step (T093): a stored snapshot becomes a release directory the sandbox mounts.

The archive comes from the content store, which a holder of the shared service principal's token
can write to, so extraction treats it as hostile: only regular files, only names inside the
directory, bounded in count and size, and nothing half-extracted is left behind on a refusal.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from reconciler_fakes import (
    FakeArchiveStore,
    archive_bytes,
    digest_of,
    seed_app,
    seed_release,
    stub_registry_class,
)

from appmux.packages.store import ContentStoreError
from appmux.reconciler import build as build_module
from appmux.reconciler.build import BuildFailed, StoreReleaseBuilder
from appmux.registry.models import Release

pytestmark = pytest.mark.anyio


def release(number: int = 3, *, deleted: bool = False) -> Release:
    registry = stub_registry_class()()
    seed_app(registry)
    seed_release(registry, "sales", number, "pending", deleted=deleted)
    found = registry.snapshot.release("sales", number)
    assert found is not None
    return found


def make(tmp_path: Path) -> tuple[StoreReleaseBuilder, FakeArchiveStore, Path]:
    store = FakeArchiveStore()
    releases_dir = tmp_path / "releases"
    return StoreReleaseBuilder(store, releases_dir), store, releases_dir


async def test_the_snapshot_is_extracted_into_a_directory_of_the_release(tmp_path: Path) -> None:
    builder, store, releases_dir = make(tmp_path)
    store.archives[digest_of(3)] = archive_bytes({"app.py": b"print(1)\n", "static/a.css": b"x{}"})

    built = await builder.build(release(3))

    assert built.release_dir == releases_dir / "sales" / "3"
    assert (built.release_dir / "app.py").read_bytes() == b"print(1)\n"
    assert (built.release_dir / "static" / "a.css").read_bytes() == b"x{}"
    assert not list((releases_dir / "sales").glob(".*")), "no scratch directory is left behind"


async def test_building_again_replaces_what_an_earlier_build_left(tmp_path: Path) -> None:
    builder, store, releases_dir = make(tmp_path)
    stale = releases_dir / "sales" / "3"
    stale.mkdir(parents=True)
    (stale / "old.py").write_text("stale")
    store.archives[digest_of(3)] = archive_bytes({"app.py": b"new"})

    built = await builder.build(release(3))

    assert sorted(path.name for path in built.release_dir.iterdir()) == ["app.py"]


@pytest.mark.parametrize("name", ["../escape.py", "/etc/escape.py", "a/../../escape.py", "a//b"])
async def test_an_entry_that_leaves_the_directory_refuses_the_whole_snapshot(
    tmp_path: Path, name: str
) -> None:
    builder, store, releases_dir = make(tmp_path)
    store.archives[digest_of(3)] = archive_bytes({"ok.py": b"1", name: b"2"})

    with pytest.raises(BuildFailed, match="not a safe path"):
        await builder.build(release(3))

    assert not (tmp_path / "escape.py").exists()
    assert not (releases_dir / "sales" / "3").exists(), "a refused snapshot leaves no release dir"
    assert not list((releases_dir / "sales").glob(".*"))


async def test_a_link_in_the_snapshot_is_refused(tmp_path: Path) -> None:
    builder, store, releases_dir = make(tmp_path)
    link = tarfile.TarInfo()
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    store.archives[digest_of(3)] = archive_bytes({"passwd": link})

    with pytest.raises(BuildFailed, match="only files"):
        await builder.build(release(3))

    assert not (releases_dir / "sales" / "3").exists()


async def test_a_snapshot_with_too_many_files_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_module, "MAX_EXTRACTED_FILES", 2)
    builder, store, _ = make(tmp_path)
    store.archives[digest_of(3)] = archive_bytes({"a": b"1", "b": b"2", "c": b"3"})

    with pytest.raises(BuildFailed, match="more than 2 files"):
        await builder.build(release(3))


async def test_a_snapshot_that_unpacks_too_large_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_module, "MAX_EXTRACTED_BYTES", 10)
    builder, store, releases_dir = make(tmp_path)
    store.archives[digest_of(3)] = archive_bytes({"big": b"x" * 11})

    with pytest.raises(BuildFailed, match="unpacks to more than"):
        await builder.build(release(3))

    assert not (releases_dir / "sales" / "3").exists()


async def test_a_snapshot_the_store_lost_fails_the_build_with_a_reason(tmp_path: Path) -> None:
    builder, _, _ = make(tmp_path)

    with pytest.raises(BuildFailed) as failure:
        await builder.build(release(3))

    assert failure.value.reason == f"{digest_of(3)} is not in the content store"


async def test_a_store_that_cannot_be_read_fails_the_build_without_the_workspace_text(
    tmp_path: Path,
) -> None:
    builder, store, _ = make(tmp_path)
    store.error = ContentStoreError("the content store could not read x (workspace status 500)")

    with pytest.raises(BuildFailed, match="workspace status 500"):
        await builder.build(release(3))


async def test_a_deleted_release_has_no_snapshot_to_build_from(tmp_path: Path) -> None:
    builder, _, _ = make(tmp_path)

    with pytest.raises(BuildFailed, match="no snapshot"):
        await builder.build(release(3, deleted=True))


async def test_something_that_is_not_an_archive_fails_the_build(tmp_path: Path) -> None:
    builder, store, _ = make(tmp_path)
    store.archives[digest_of(3)] = b"this is not a tar file"

    with pytest.raises(BuildFailed, match="not a readable archive"):
        await builder.build(release(3))
