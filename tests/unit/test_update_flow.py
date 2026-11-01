"""The Update flow (T092, design 5.2 steps 1 to 6): from a press of the button to a pending release.

The workspace and the content store are fakes (the real ones are tested with their own seams); the
registry is the in-memory one, so a test sees the rows the flow's single ``write`` produces. The
``write`` handed to the flow is the one the admin route builds, reduced to what the flow may rely
on: it applies the changes and remembers them.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

import pytest
from pydantic import SecretStr
from reconciler_fakes import (
    FakeArchiveStore,
    archive_bytes,
    digest_of,
    seed_app,
    seed_release,
    stub_registry_class,
)

from appmux.identity.actor import Actor
from appmux.packages.store import ContentStoreError
from appmux.reconciler.ports import UpdateRefused, UpdateResult
from appmux.reconciler.snapshot_source import (
    EMPTY_FOLDER_REASON,
    SESSION_EXPIRED_REASON,
    Snapshot,
    SnapshotRefused,
)
from appmux.reconciler.update import UpdateService
from appmux.registry.errors import ChangedByOthers, RegistryUnavailable, RowAlreadyExists
from appmux.registry.models import EnvEntry, RowModel
from appmux.registry.protocol import Change
from appmux.registry.writers import WriteKind

pytestmark = pytest.mark.anyio

ACTOR = Actor(email="dana@example.com")
TOKEN = SecretStr("tok-test")
NEW_DIGEST = digest_of(900)
APP_YAML = b"command:\n  - python\n  - main.py\nenv:\n  - name: MODE\n    value: demo\n"


class FakeSnapshots:
    """The workspace reader and the content store as one: answers a digest for the folder."""

    def __init__(self, digest: str = NEW_DIGEST) -> None:
        self.digest = digest
        self.error: Exception | None = None
        self.calls: list[tuple[str, str, frozenset[str]]] = []

    async def take(
        self, *, source_path: str, token: str, live_digests: Collection[str]
    ) -> Snapshot:
        self.calls.append((source_path, token, frozenset(live_digests)))
        if self.error is not None:
            raise self.error
        return Snapshot(
            digest=self.digest,
            file_count=2,
            size_bytes=100,
            already_up_to_date=self.digest in live_digests,
        )


class RecordingWriter:
    """What the admin route passes as ``write``: it applies the changes, like the real one."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry
        self.calls: list[tuple[Sequence[Change], int]] = []
        self.failures: list[Exception] = []

    async def __call__(
        self, changes: Sequence[Change], *, release_no: int
    ) -> tuple[RowModel | None, ...]:
        self.calls.append((changes, release_no))
        if self.failures:
            raise self.failures.pop(0)
        return await self.registry.write(changes)


class Flow:
    """An app `sales` with the given releases, and a service over fakes."""

    def __init__(
        self, releases: dict[int, str] | None = None, *, target: int | None = None
    ) -> None:
        self.registry = stub_registry_class()()
        seed_app(self.registry, target_release_no=target, desired_state="stopped")
        for number, status in (releases or {}).items():
            seed_release(self.registry, "sales", number, status)
        self.snapshots = FakeSnapshots()
        self.store = FakeArchiveStore()
        self.store.archives[NEW_DIGEST] = archive_bytes(
            {"app.yaml": APP_YAML, "main.py": b"print(1)\n", "requirements.txt": b"fastapi\n"}
        )
        self.writer = RecordingWriter(self.registry)
        self.service = UpdateService(
            registry=self.registry, snapshots=self.snapshots, store=self.store
        )

    async def press(self, slug: str = "sales") -> UpdateResult:
        return await self.service.update(
            actor=ACTOR, app_slug=slug, user_token=TOKEN, write=self.writer
        )

    def release(self, number: int) -> Any:
        return self.registry.snapshot.release("sales", number)

    def config(self) -> Any:
        return self.registry.snapshot.app_config("sales")


# --- a new release --------------------------------------------------------------------------------


async def test_a_new_folder_creates_one_pending_release_and_makes_it_the_target() -> None:
    flow = Flow()

    result = await flow.press()

    assert result == UpdateResult(release_no=1, is_up_to_date=False)
    ((changes, release_no),) = flow.writer.calls
    assert release_no == 1
    assert [change.kind for change in changes] == [
        WriteKind.RELEASE_INSERT,
        WriteKind.APP_CONFIG_UPDATE,
    ]
    release = flow.release(1)
    assert release.status == "pending"
    assert release.snapshot_digest == NEW_DIGEST
    assert release.created_by == "dana@example.com"
    assert flow.config().target_release_no == 1


async def test_the_manifest_of_the_snapshot_is_stored_with_the_release() -> None:
    flow = Flow()

    await flow.press()

    detected = flow.release(1).detected
    assert detected.runtime == "python"
    assert detected.command == ["python", "main.py"]
    assert [(entry.name, entry.value) for entry in detected.env] == [("MODE", "demo")]
    assert flow.store.gets == [NEW_DIGEST]


async def test_an_update_starts_a_stopped_app() -> None:
    flow = Flow()
    assert flow.config().desired_state == "stopped"

    await flow.press()

    assert flow.config().desired_state == "running"


async def test_the_config_update_carries_the_version_the_flow_read() -> None:
    flow = Flow()
    version = flow.config().version

    await flow.press()

    ((changes, _),) = flow.writer.calls
    assert changes[1].expected_version == version
    assert changes[1].key == {"app_slug": "sales"}
    assert flow.config().version == version + 1


async def test_the_folder_is_read_with_the_presser_token_and_the_apps_source_path() -> None:
    flow = Flow()

    await flow.press()

    ((source_path, token, _),) = flow.snapshots.calls
    assert source_path == "/Workspace/Shared/apps/sales"
    assert token == TOKEN.get_secret_value()


async def test_release_numbers_never_reuse_a_deleted_one() -> None:
    flow = Flow({1: "superseded", 2: "active"}, target=2)
    seed_release(flow.registry, "sales", 3, "failed", deleted=True)

    result = await flow.press()

    assert result.release_no == 4


async def test_the_config_is_copied_from_the_target_release() -> None:
    flow = Flow({1: "active", 2: "failed"}, target=2)
    await flow.registry.write(
        [
            Change(
                WriteKind.RELEASE_UPDATE,
                key={"app_slug": "sales", "release_no": 2},
                values={
                    "config": {"env": [{"name": "REGION", "value": "eu"}], "wheel_sources": []}
                },
            )
        ]
    )

    await flow.press()

    assert [(e.name, e.value) for e in flow.release(3).config.env] == [("REGION", "eu")]


async def test_without_a_usable_target_the_config_comes_from_the_active_release() -> None:
    flow = Flow({1: "active"}, target=None)
    await flow.registry.write(
        [
            Change(
                WriteKind.RELEASE_UPDATE,
                key={"app_slug": "sales", "release_no": 1},
                values={
                    "config": {"env": [{"name": "REGION", "value": "us"}], "wheel_sources": []}
                },
            )
        ]
    )

    await flow.press()

    assert flow.release(2).config.env == [EnvEntry(name="REGION", value="us")]


async def test_the_first_release_of_an_app_starts_with_an_empty_config() -> None:
    flow = Flow()

    await flow.press()

    assert flow.release(1).config.env == []
    assert flow.release(1).config.wheel_sources == []


# --- already up to date ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "building", "starting", "healthy", "active"])
async def test_a_folder_equal_to_a_live_release_creates_nothing(status: str) -> None:
    flow = Flow({4: status}, target=4)
    flow.snapshots.digest = digest_of(4)

    result = await flow.press()

    assert result == UpdateResult(release_no=4, is_up_to_date=True)
    assert flow.writer.calls == []
    assert flow.config().target_release_no == 4


async def test_the_active_release_is_the_one_reported_when_several_share_the_digest() -> None:
    flow = Flow({1: "superseded", 2: "active", 3: "pending"}, target=3)
    for number in (1, 3):
        await flow.registry.write(
            [
                Change(
                    WriteKind.RELEASE_UPDATE,
                    key={"app_slug": "sales", "release_no": number},
                    values={"snapshot_digest": digest_of(2)},
                )
            ]
        )
    flow.snapshots.digest = digest_of(2)

    result = await flow.press()

    assert result == UpdateResult(release_no=2, is_up_to_date=True)


@pytest.mark.parametrize("status", ["failed", "superseded"])
async def test_a_folder_equal_to_a_failed_or_replaced_release_makes_a_new_release(
    status: str,
) -> None:
    flow = Flow({4: status})
    flow.snapshots.digest = digest_of(4)
    flow.store.archives[digest_of(4)] = flow.store.archives[NEW_DIGEST]

    result = await flow.press()

    assert result == UpdateResult(release_no=5, is_up_to_date=False)
    assert flow.release(5).snapshot_digest == digest_of(4)


async def test_only_the_digests_of_live_releases_are_offered_as_up_to_date() -> None:
    flow = Flow({1: "failed", 2: "superseded", 3: "active", 4: "pending"})
    seed_release(flow.registry, "sales", 5, "pending", deleted=True)

    await flow.press()

    ((_, _, live),) = flow.snapshots.calls
    assert live == {digest_of(3), digest_of(4)}


# --- refusals: nothing is written -----------------------------------------------------------------


async def test_an_unknown_app_is_not_found_and_nothing_is_read() -> None:
    flow = Flow()

    with pytest.raises(UpdateRefused) as refusal:
        await flow.press("nope")

    assert refusal.value.kind == "not_found"
    assert flow.snapshots.calls == []


async def test_a_folder_that_cannot_be_snapshotted_is_refused_with_its_own_message() -> None:
    flow = Flow()
    flow.snapshots.error = SnapshotRefused(EMPTY_FOLDER_REASON)

    with pytest.raises(UpdateRefused) as refusal:
        await flow.press()

    assert (refusal.value.kind, refusal.value.message) == ("refused", EMPTY_FOLDER_REASON)
    assert flow.writer.calls == []


async def test_an_expired_session_is_unavailable_and_says_to_press_update_again() -> None:
    flow = Flow()
    flow.snapshots.error = SnapshotRefused(SESSION_EXPIRED_REASON)

    with pytest.raises(UpdateRefused) as refusal:
        await flow.press()

    assert (refusal.value.kind, refusal.value.message) == ("unavailable", SESSION_EXPIRED_REASON)


async def test_a_content_store_that_cannot_return_the_snapshot_is_unavailable() -> None:
    flow = Flow()
    flow.store.error = ContentStoreError("the content store could not read x (no answer)")

    with pytest.raises(UpdateRefused) as refusal:
        await flow.press()

    assert refusal.value.kind == "unavailable"
    assert "content store" in refusal.value.message
    assert flow.writer.calls == []


async def test_an_invalid_app_yaml_refuses_the_update() -> None:
    flow = Flow()
    flow.store.archives[NEW_DIGEST] = archive_bytes({"app.yaml": b"command: [unclosed\n"})

    with pytest.raises(UpdateRefused) as refusal:
        await flow.press()

    assert refusal.value.kind == "refused"
    assert "app.yaml" in refusal.value.message
    assert flow.writer.calls == []


async def test_a_snapshot_that_is_not_an_archive_is_unavailable_not_a_crash() -> None:
    flow = Flow()
    flow.store.archives[NEW_DIGEST] = b"not a tar file"

    with pytest.raises(UpdateRefused) as refusal:
        await flow.press()

    assert refusal.value.kind == "unavailable"
    assert flow.writer.calls == []


# --- a write that meets another Update ------------------------------------------------------------


async def test_a_release_number_taken_meanwhile_is_asked_for_again_after_a_refresh() -> None:
    flow = Flow()
    refreshes = 0
    real_refresh = flow.registry.refresh

    async def counting_refresh() -> None:
        nonlocal refreshes
        refreshes += 1
        await real_refresh()

    flow.registry.refresh = counting_refresh

    async def other_update_wins() -> None:
        seed_release(flow.registry, "sales", 1, "pending")

    class RacingWriter(RecordingWriter):
        async def __call__(
            self, changes: Sequence[Change], *, release_no: int
        ) -> tuple[RowModel | None, ...]:
            if not self.calls:
                self.calls.append((changes, release_no))
                await other_update_wins()
                raise RowAlreadyExists
            return await super().__call__(changes, release_no=release_no)

    flow.writer = RacingWriter(flow.registry)

    result = await flow.press()

    assert result == UpdateResult(release_no=2, is_up_to_date=False)
    assert [number for _, number in flow.writer.calls] == [1, 2]
    assert refreshes == 1
    assert flow.config().target_release_no == 2


async def test_a_config_changed_meanwhile_is_read_again_and_the_write_repeated() -> None:
    flow = Flow()
    flow.writer.failures = [ChangedByOthers()]

    result = await flow.press()

    assert result == UpdateResult(release_no=1, is_up_to_date=False)
    assert len(flow.writer.calls) == 2


async def test_when_the_other_update_made_the_same_folder_live_the_answer_is_up_to_date() -> None:
    flow = Flow()
    flow.snapshots.digest = digest_of(1)
    flow.store.archives[digest_of(1)] = flow.store.archives[NEW_DIGEST]

    class SameFolderWins(RecordingWriter):
        async def __call__(
            self, changes: Sequence[Change], *, release_no: int
        ) -> tuple[RowModel | None, ...]:
            self.calls.append((changes, release_no))
            seed_release(flow.registry, "sales", 1, "pending")
            raise RowAlreadyExists

    flow.writer = SameFolderWins(flow.registry)

    result = await flow.press()

    assert result == UpdateResult(release_no=1, is_up_to_date=True)
    assert len(flow.writer.calls) == 1


async def test_an_update_that_keeps_meeting_other_changes_is_a_conflict_after_three_tries() -> None:
    flow = Flow()
    flow.writer.failures = [
        ChangedByOthers(),
        ChangedByOthers(),
        ChangedByOthers(),
        ChangedByOthers(),
    ]

    with pytest.raises(UpdateRefused) as refusal:
        await flow.press()

    assert refusal.value.kind == "conflict"
    assert "press Update again" in refusal.value.message
    assert len(flow.writer.calls) == 3


async def test_a_registry_that_is_down_is_not_hidden() -> None:
    flow = Flow()
    flow.writer.failures = [RegistryUnavailable()]

    with pytest.raises(RegistryUnavailable):
        await flow.press()

    assert len(flow.writer.calls) == 1
    assert flow.release(1) is None
