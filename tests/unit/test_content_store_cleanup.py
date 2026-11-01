"""The daily content-store clean-up (T112, T116; data model rule 11).

Rule 11: a content-store file is removed only when no live release references it *and* it is
older than a day. Every test here is a way for that to go wrong: deleting a file a release needs
(unrecoverable if the source folder has changed) is far worse than keeping one for another day, so
whenever the job is unsure -- an unknown age, a reference it cannot read, a name it does not
recognise -- it keeps the file.

The store and the registry are fakes: the real workspace listing (with modification times and
delete) and the query for live releases' digests belong to the wiring, which is not proven here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from appmux.packages.cleanup import (
    MIN_AGE,
    CleanupReport,
    MalformedReference,
    ReferenceReader,
    StoredFile,
    clean_content_store,
    referenced_digests,
)
from appmux.packages.store import WorkspaceError

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
SNAPSHOT = "1" * 64
WHEEL = "2" * 64
LOOSE = "3" * 64  # nothing references it
OTHER_LOOSE = "4" * 64


def aged(name: str, age: timedelta | None) -> StoredFile:
    return StoredFile(name=name, modified_at=None if age is None else NOW - age)


@dataclass
class FakeStoreFiles:
    files: list[StoredFile]
    deleted: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    failing_listing: Exception | None = None
    failing_deletes: dict[str, Exception] = field(default_factory=dict)
    running: int = 0
    most_running: int = 0

    async def list_files(self) -> list[StoredFile]:
        self.events.append("list")
        if self.failing_listing is not None:
            raise self.failing_listing
        return list(self.files)

    async def delete_file(self, name: str) -> None:
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            await asyncio.sleep(0)  # let a concurrent delete overlap, if there were one
            if name in self.failing_deletes:
                raise self.failing_deletes[name]
            self.deleted.append(name)
        finally:
            self.running -= 1


def references(
    *names: str, events: list[str] | None = None, error: Exception | None = None
) -> ReferenceReader:
    async def read() -> Iterable[str]:
        if events is not None:
            events.append("references")
        if error is not None:
            raise error
        return names

    return read


async def clean(store: FakeStoreFiles, *referenced: str) -> CleanupReport:
    return await clean_content_store(
        store, references(*referenced, events=store.events), now=lambda: NOW
    )


# --- the two conditions of rule 11 --------------------------------------------------------------


async def test_a_file_nothing_references_and_older_than_a_day_is_removed() -> None:
    store = FakeStoreFiles([aged(LOOSE, timedelta(days=3))])
    report = await clean(store)
    assert store.deleted == [LOOSE]
    assert report.removed == (LOOSE,)


async def test_a_referenced_file_stays_however_old_it_is() -> None:
    store = FakeStoreFiles([aged(SNAPSHOT, timedelta(days=400)), aged(WHEEL, timedelta(days=400))])
    report = await clean(store, SNAPSHOT, WHEEL)
    assert store.deleted == []
    assert report.removed == ()
    assert report.kept_referenced == 2


async def test_an_unreferenced_file_under_a_day_old_stays() -> None:
    # It may belong to an Update that has stored its files but not yet inserted its release row.
    store = FakeStoreFiles([aged(LOOSE, timedelta(hours=23, minutes=59))])
    report = await clean(store)
    assert store.deleted == []
    assert report.kept_recent == 1


async def test_a_file_exactly_a_day_old_stays_and_one_second_older_goes() -> None:
    store = FakeStoreFiles(
        [aged(LOOSE, timedelta(days=1)), aged(OTHER_LOOSE, timedelta(days=1, seconds=1))]
    )
    await clean(store)
    assert store.deleted == [OTHER_LOOSE]


async def test_the_minimum_age_is_one_day() -> None:
    assert timedelta(days=1) == MIN_AGE


async def test_only_the_unreferenced_old_files_among_many_are_removed() -> None:
    store = FakeStoreFiles(
        [
            aged(SNAPSHOT, timedelta(days=9)),
            aged(LOOSE, timedelta(days=9)),
            aged(WHEEL, timedelta(days=9)),
            aged(OTHER_LOOSE, timedelta(hours=2)),
        ]
    )
    report = await clean(store, SNAPSHOT, WHEEL)
    assert store.deleted == [LOOSE]
    assert (report.kept_referenced, report.kept_recent, len(report.removed)) == (2, 1, 1)


# --- when the job is unsure, it keeps the file ---------------------------------------------------


async def test_a_file_with_an_unknown_age_is_kept() -> None:
    store = FakeStoreFiles([aged(LOOSE, None)])
    report = await clean(store)
    assert store.deleted == []
    assert report.kept_recent == 1


async def test_a_file_dated_in_the_future_is_kept() -> None:
    store = FakeStoreFiles([StoredFile(LOOSE, NOW + timedelta(days=5))])
    await clean(store)
    assert store.deleted == []


@pytest.mark.parametrize(
    "name",
    [
        "README.txt",
        "sha256:" + "5" * 64,  # a digest with its prefix is not a file name in the store
        "A" * 64,  # upper-case hex is not how the store names a file
        "5" * 63,
        "5" * 65,
        "5" * 64 + ".whl",
        "../" + "5" * 61,
        "",
    ],
)
async def test_an_entry_that_is_not_named_by_a_digest_is_never_touched(name: str) -> None:
    store = FakeStoreFiles([aged(name, timedelta(days=100))])
    report = await clean(store)
    assert store.deleted == []
    assert report.ignored == 1


async def test_a_listing_that_fails_removes_nothing_and_says_why() -> None:
    store = FakeStoreFiles([aged(LOOSE, timedelta(days=9))], failing_listing=WorkspaceError("down"))
    with pytest.raises(WorkspaceError, match="down"):
        await clean(store)
    assert store.deleted == []


async def test_unreadable_references_remove_nothing() -> None:
    # Without the list of what releases need, every old file looks unreferenced.
    store = FakeStoreFiles([aged(LOOSE, timedelta(days=9)), aged(SNAPSHOT, timedelta(days=9))])
    with pytest.raises(RuntimeError, match="database is down"):
        await clean_content_store(
            store, references(error=RuntimeError("database is down")), now=lambda: NOW
        )
    assert store.deleted == []


async def test_a_malformed_reference_removes_nothing() -> None:
    store = FakeStoreFiles([aged(LOOSE, timedelta(days=9)), aged(SNAPSHOT, timedelta(days=9))])
    with pytest.raises(MalformedReference, match="not-a-digest"):
        await clean(store, SNAPSHOT, "not-a-digest")
    assert store.deleted == []


async def test_a_naive_modification_time_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="time zone"):
        StoredFile(LOOSE, datetime(2026, 1, 1))  # the point of the test: no tzinfo


# --- order and pace -----------------------------------------------------------------------------


async def test_references_are_read_after_the_listing() -> None:
    # A release inserted between the two calls must still protect the file it reuses, so the
    # references are the later of the two reads.
    store = FakeStoreFiles([aged(LOOSE, timedelta(days=9))])
    await clean(store)
    assert store.events == ["list", "references"]


async def test_files_are_removed_one_at_a_time() -> None:
    # Parallel workspace calls are answered with 429 (spike 4).
    store = FakeStoreFiles([aged(f"{n:064x}", timedelta(days=9)) for n in range(1, 8)])
    await clean(store)
    assert len(store.deleted) == 7
    assert store.most_running == 1


# --- one failure does not hide the others -------------------------------------------------------


async def test_a_file_that_cannot_be_removed_is_reported_and_the_rest_go() -> None:
    store = FakeStoreFiles(
        [aged(LOOSE, timedelta(days=9)), aged(OTHER_LOOSE, timedelta(days=9))],
        failing_deletes={LOOSE: WorkspaceError("too many requests", status=429)},
    )
    report = await clean(store)
    assert store.deleted == [OTHER_LOOSE]
    assert report.removed == (OTHER_LOOSE,)
    assert [failure.name for failure in report.failed] == [LOOSE]
    assert "too many requests" in report.failed[0].reason
    assert "429" in report.failed[0].reason


async def test_a_bug_in_the_store_adapter_is_not_swallowed() -> None:
    store = FakeStoreFiles(
        [aged(LOOSE, timedelta(days=9))], failing_deletes={LOOSE: TypeError("bad adapter")}
    )
    with pytest.raises(TypeError, match="bad adapter"):
        await clean(store)


# --- the report ----------------------------------------------------------------------------------


async def test_the_summary_says_what_happened_in_one_line() -> None:
    store = FakeStoreFiles(
        [
            aged(LOOSE, timedelta(days=9)),
            aged(SNAPSHOT, timedelta(days=9)),
            aged(OTHER_LOOSE, timedelta(hours=1)),
            aged("notes.txt", timedelta(days=9)),
        ]
    )
    summary = (await clean(store, SNAPSHOT)).summary()
    assert "\n" not in summary
    assert summary == (
        "removed 1 file; kept 1 in use and 1 under a day old; left 1 other entry alone"
    )


async def test_an_empty_store_is_a_quiet_run() -> None:
    report = await clean(FakeStoreFiles([]))
    assert report.summary() == "removed 0 files; kept 0 in use and 0 under a day old"


# --- naming what a release references -------------------------------------------------------------


def test_references_are_the_snapshot_digests_and_wheel_hashes_as_file_names() -> None:
    assert referenced_digests(
        snapshot_digests=[f"sha256:{SNAPSHOT}"], wheel_sha256s=[WHEEL, WHEEL]
    ) == frozenset({SNAPSHOT, WHEEL})


@pytest.mark.parametrize(
    ("snapshots", "wheels"),
    [
        ([SNAPSHOT], []),  # a snapshot digest carries its sha256: prefix
        (["sha256:" + "A" * 64], []),
        ([], ["sha256:" + WHEEL]),  # a wheel hash is bare hex
        ([], [WHEEL[:-1]]),
        ([""], []),
    ],
)
def test_a_reference_in_the_wrong_shape_is_refused(snapshots: list[str], wheels: list[str]) -> None:
    with pytest.raises(MalformedReference):
        referenced_digests(snapshot_digests=snapshots, wheel_sha256s=wheels)
