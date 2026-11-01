"""The snapshot step of an Update: read with the presser's token, store with appmux's (T089).

The source folder and the content store are two in-memory workspaces that accept only their own
token, so a call made with the wrong identity fails the test: the owner's folder is read with the
owner's OBO token (the service principal has no grant there, spike 15), and the store is written
with the service principal's token (the owner has no grant there).
"""

from __future__ import annotations

import hashlib

import pytest
from fake_workspace import FakeWorkspace

from appmux.packages.store import ContentStore
from appmux.reconciler.snapshot_source import (
    EMPTY_FOLDER_REASON,
    Snapshot,
    SnapshotRefused,
    SnapshotSource,
)

pytestmark = pytest.mark.anyio

OBO_TOKEN = "obo-token-example"  # noqa: S105 - an obviously fake label the fakes compare
SP_TOKEN = "sp-token-example"  # noqa: S105 - same
OTHER_TOKEN = "someone-else-example"  # noqa: S105 - same: a token the store refuses
SOURCE_FOLDER = "/Workspace/Users/owner@example.com/sales"
STORE_FOLDER = "/Workspace/Users/00000000-example/appmux-store"


class Books:
    """The two workspaces and the source under test."""

    def __init__(self) -> None:
        self.source = FakeWorkspace(required_token=OBO_TOKEN)
        self.store_workspace = FakeWorkspace(required_token=SP_TOKEN)
        self.store = ContentStore(
            self.store_workspace, folder=STORE_FOLDER, token_source=lambda: SP_TOKEN
        )
        self.snapshots = SnapshotSource(self.source, self.store)

    def add_source(self, **files: bytes) -> None:
        for name, data in files.items():
            self.source.add_file(f"{SOURCE_FOLDER}/{name.replace('__', '/')}", data)

    async def take(self, live: frozenset[str] = frozenset()) -> Snapshot:
        return await self.snapshots.take(
            source_path=SOURCE_FOLDER, token=OBO_TOKEN, live_digests=live
        )


async def test_an_update_stores_the_snapshot_named_by_its_digest() -> None:
    books = Books()
    books.add_source(app__py=b"print(1)\n", app__yaml=b"command: [python]\n")
    snapshot = await books.take()
    assert snapshot.file_count == 2
    assert snapshot.digest.startswith("sha256:")
    assert list(books.store_workspace.files) == [
        f"{STORE_FOLDER}/{snapshot.digest.removeprefix('sha256:')}"
    ]
    stored = next(iter(books.store_workspace.files.values()))
    assert snapshot.digest == "sha256:" + hashlib.sha256(stored).hexdigest()
    assert snapshot.already_up_to_date is False


async def test_the_source_is_read_with_the_owners_token_and_the_store_with_appmuxs() -> None:
    books = Books()
    books.add_source(app__py=b"x")
    await books.take()
    assert {token for _, _, token in books.source.calls} == {OBO_TOKEN}
    assert {token for _, _, token in books.store_workspace.calls} == {SP_TOKEN}
    assert any(operation == "write" for operation, _, _ in books.store_workspace.calls)


async def test_the_same_folder_twice_gives_the_same_digest() -> None:
    books = Books()
    books.add_source(app__py=b"x")
    first = await books.take()
    second = await books.take()
    assert first.digest == second.digest


async def test_a_digest_of_a_live_release_is_already_up_to_date() -> None:
    books = Books()
    books.add_source(app__py=b"x")
    digest = (await books.take()).digest
    again = await books.take(live=frozenset({digest}))
    assert again.already_up_to_date is True
    assert again.digest == digest


async def test_a_live_digest_of_another_content_does_not_count() -> None:
    books = Books()
    books.add_source(app__py=b"x")
    snapshot = await books.take(live=frozenset({"sha256:" + "0" * 64}))
    assert snapshot.already_up_to_date is False


async def test_content_already_in_the_store_is_not_uploaded_again() -> None:
    # Another app (or an earlier Update) stored the same bytes: one file per digest.
    books = Books()
    books.add_source(app__py=b"x")
    await books.take()
    writes_before = [c for c in books.store_workspace.calls if c[0] == "write"]
    await books.take()
    writes_after = [c for c in books.store_workspace.calls if c[0] == "write"]
    assert len(writes_before) == len(writes_after) == 1


async def test_a_live_digest_whose_file_left_the_store_is_stored_again() -> None:
    # "Up to date" must not hide a store that lost the file: the next start would fail its check.
    books = Books()
    books.add_source(app__py=b"x")
    digest = (await books.take()).digest
    books.store_workspace.files.clear()
    await books.take(live=frozenset({digest}))
    assert len(books.store_workspace.files) == 1


async def test_an_empty_folder_is_refused_and_nothing_is_stored() -> None:
    books = Books()
    books.source.add_folder(SOURCE_FOLDER)
    with pytest.raises(SnapshotRefused) as caught:
        await books.take()
    assert str(caught.value) == EMPTY_FOLDER_REASON
    assert books.store_workspace.calls == []


async def test_a_refused_folder_stores_nothing() -> None:
    books = Books()
    books.add_source(app__py=b"x")
    books.source.add_other_object(f"{SOURCE_FOLDER}/nb", "NOTEBOOK")
    with pytest.raises(SnapshotRefused):
        await books.take()
    assert books.store_workspace.files == {}


async def test_a_store_that_cannot_be_written_fails_the_update_with_a_reason() -> None:
    books = Books()
    books.add_source(app__py=b"x")
    books.store_workspace.required_token = OTHER_TOKEN
    with pytest.raises(SnapshotRefused) as caught:
        await books.take()
    assert "content store" in str(caught.value)
    assert SP_TOKEN not in str(caught.value)
    assert OBO_TOKEN not in str(caught.value)


async def test_a_snapshot_over_the_file_limit_is_refused_by_the_store_naming_the_digest() -> None:
    books = Books()
    books.add_source(app__py=b"x" * 2000)
    small_store = ContentStore(
        books.store_workspace,
        folder=STORE_FOLDER,
        token_source=lambda: SP_TOKEN,
        max_file_bytes=100,
    )
    snapshots = SnapshotSource(books.source, small_store)
    with pytest.raises(SnapshotRefused) as caught:
        await snapshots.take(source_path=SOURCE_FOLDER, token=OBO_TOKEN, live_digests=frozenset())
    assert "sha256:" in str(caught.value)
    assert "100 bytes" in str(caught.value)
