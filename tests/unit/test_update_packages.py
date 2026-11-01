"""The Update flow resolves a release's packages and records its wheel list (FR-031, design 5.3).

After the snapshot is stored and before a release row is written, the flow reads the snapshot's
``requirements.txt`` and decides, per requirement, whether the environment, a hashed wheel or the
index supplies it (``packages.resolve``). The wheels it needs are looked for in this order: the
app's own ``wheels/`` folder (inside the snapshot), the release config's ``wheel_sources``, then the
platform's ``default_wheel_sources``; the workspace folders are read with the presser's token. Each
wheel ends up in the content store, and the release row records ``WheelEntry(sha256, filename,
source)`` for the ones it installs. What the environment supplies is neither fetched nor recorded.

An unresolved requirement refuses the Update with **every** requirement in one message (design 5.3:
"the Update fails and lists every requirement it could not resolve at once"), before the cap's
clean-up deletes anything. The workspace, the content store and the environment are fakes; the
registry is the in-memory one. Wheel files hold arbitrary bytes: the store checks only their sha256.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Collection, Mapping, Sequence
from importlib.metadata import distributions
from pathlib import Path
from typing import Any

import pytest
from fake_workspace import FakeWorkspace
from pydantic import SecretStr
from reconciler_fakes import (
    NOW,
    FakeArchiveStore,
    archive_bytes,
    digest_of,
    seed_app,
    seed_release,
    stub_registry_class,
)

from appmux.identity.actor import Actor
from appmux.packages.environment import Environment, read_environment
from appmux.packages.store import ContentStore, ContentStoreError
from appmux.packages.wheels import STORE_SOURCE, WheelStore
from appmux.reconciler.ports import UpdateRefused, UpdateResult
from appmux.reconciler.snapshot_source import Snapshot
from appmux.reconciler.update import UpdateService
from appmux.reconciler.update_packages import UpdatePackages
from appmux.registry.errors import ChangedByOthers
from appmux.registry.models import DetectedManifest, ReleaseConfig, RowModel, WheelEntry
from appmux.registry.protocol import Change
from appmux.registry.releases import insert_release_change
from appmux.registry.writers import WriteKind

pytestmark = pytest.mark.anyio

ACTOR = Actor(email="dana@example.com")
PRESSER_TOKEN = "tok-test"  # noqa: S105 - an obviously fake label
SP_TOKEN = "sp-token-example"  # noqa: S105 - an obviously fake label
STORE_FOLDER = "/Workspace/Users/00000000-example/appmux-store"
CONFIG_FOLDER = "/Workspace/Users/dana@example.com/wheels/sales"
DEFAULT_FOLDER = "/Workspace/Shared/appmux/wheelhouse"
NEW_DIGEST = digest_of(900)
APP_YAML = b"command:\n  - python\n  - main.py\n"
ALPHA = b"alpha wheel bytes"
BETA = b"beta wheel bytes"
GAMMA = b"gamma wheel bytes"
ENVIRONMENT_PACKAGES = {"fastapi": "0.115.0", "uvicorn": "0.30.6", "appdirs": "1.4.4"}


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wheel_name(name: str, version: str) -> str:
    return f"{name.replace('-', '_')}-{version}-py3-none-any.whl"


def pin(name: str, version: str, *wheels: bytes) -> str:
    hashes = " ".join(f"--hash=sha256:{sha256_of(data)}" for data in wheels)
    return f"{name}=={version} {hashes}"


def make_environment(root: Path, packages: Mapping[str, str]) -> Environment:
    root.mkdir(parents=True, exist_ok=True)
    for name, version in packages.items():
        folder = root / f"{name.replace('-', '_')}-{version}.dist-info"
        folder.mkdir()
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        (folder / "METADATA").write_text(metadata, encoding="utf-8")
    return read_environment(distributions(path=[str(root)]))


class FakeSnapshots:
    def __init__(self) -> None:
        self.digest = NEW_DIGEST

    async def take(
        self, *, source_path: str, token: str, live_digests: Collection[str]
    ) -> Snapshot:
        return Snapshot(
            digest=self.digest,
            file_count=2,
            size_bytes=100,
            already_up_to_date=self.digest in live_digests,
        )


class FlakyRegistry(stub_registry_class()):  # type: ignore[misc]
    """The in-memory registry whose next ``write`` can be made to lose a race."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_write_with: Exception | None = None

    async def write(self, changes: Any, audit: Any = (), **kwargs: Any) -> Any:
        if self.fail_next_write_with is not None:
            failure, self.fail_next_write_with = self.fail_next_write_with, None
            raise failure
        return await super().write(changes, audit, **kwargs)


class RecordingWriter:
    def __init__(self, registry: Any) -> None:
        self.registry = registry
        self.calls: list[Sequence[Change]] = []

    async def __call__(
        self, changes: Sequence[Change], *, release_no: int
    ) -> tuple[RowModel | None, ...]:
        self.calls.append(changes)
        return await self.registry.write(changes)


class FailingStore(WheelStore):
    """A wheel store that cannot be written to."""

    async def add(self, sha256: str, source: Path) -> None:
        raise ContentStoreError("the content store could not store the file (workspace status 500)")


class Flow:
    """App `sales`, a snapshot holding ``files``, and the service with package resolution."""

    def __init__(
        self,
        tmp_path: Path,
        requirements: str | bytes | None,
        *,
        wheels_in_snapshot: Mapping[str, bytes] | None = None,
        environment: Mapping[str, str] | None = None,
        store_class: type[WheelStore] = WheelStore,
        with_packages: bool = True,
    ) -> None:
        self.registry = FlakyRegistry()
        seed_app(self.registry, target_release_no=None, desired_state="stopped")
        self.workspace = FakeWorkspace(required_token=None)
        content = ContentStore(self.workspace, folder=STORE_FOLDER, token_source=lambda: SP_TOKEN)
        self.wheel_store = store_class(content)
        files: dict[str, bytes] = {"app.yaml": APP_YAML, "main.py": b"print(1)\n"}
        if requirements is not None:
            files["requirements.txt"] = (
                requirements if isinstance(requirements, bytes) else requirements.encode()
            )
        for filename, data in (wheels_in_snapshot or {}).items():
            files[f"wheels/{filename}"] = data
        self.archives = FakeArchiveStore()
        self.archives.archives[NEW_DIGEST] = archive_bytes(files)
        env = make_environment(tmp_path / "site", environment or ENVIRONMENT_PACKAGES)
        self.writer = RecordingWriter(self.registry)
        self.service = UpdateService(
            registry=self.registry,
            snapshots=FakeSnapshots(),
            store=self.archives,
            packages=(
                UpdatePackages(
                    store=self.wheel_store, workspace=self.workspace, environment=lambda: env
                )
                if with_packages
                else None
            ),
        )

    async def press(self) -> UpdateResult:
        return await self.service.update(
            actor=ACTOR, app_slug="sales", user_token=SecretStr(PRESSER_TOKEN), write=self.writer
        )

    def release(self, number: int) -> Any:
        return self.registry.snapshot.release("sales", number)

    def workspace_reads(self) -> list[tuple[str, str, str]]:
        return [call for call in self.workspace.calls if call[0] in {"list", "read"}]

    def wheel_reads(self) -> list[str]:
        return [path for op, path, _ in self.workspace.calls if op == "read" and ".whl" in path]

    def put_in_workspace(self, folder: str, filename: str, data: bytes) -> None:
        self.workspace.add_file(f"{folder}/{filename}", data)

    def target_config(self, *, wheel_sources: Sequence[str]) -> None:
        """Release 1 is the target and carries ``wheel_sources`` in its config."""
        self.registry.seed(
            "releases",
            {
                "app_slug": "sales",
                "release_no": 1,
                "snapshot_digest": digest_of(1),
                "detected": {"runtime": "python", "command": ["python", "app.py"]},
                "config": {"wheel_sources": list(wheel_sources)},
                "status": "active",
                "status_at": NOW,
                "created_by": "alice@example.com",
                "created_at": NOW,
            },
        )
        self.registry.seed(
            "app_config",
            {
                "app_slug": "sales",
                "desired_state": "running",
                "target_release_no": 1,
                "updated_by": "dana@example.com",
                "updated_at": NOW,
            },
        )


def set_setting(registry: Any, key: str, value: object) -> None:
    registry.seed(
        "platform_settings",
        {"key": key, "value": value, "updated_by": "dana@example.com", "updated_at": NOW},
    )


# --- the app's own wheels/ folder -----------------------------------------------------------------


async def test_a_wheel_in_the_apps_own_folder_is_stored_and_recorded(tmp_path: Path) -> None:
    flow = Flow(
        tmp_path,
        pin("alpha", "1.0.0", ALPHA),
        wheels_in_snapshot={wheel_name("alpha", "1.0.0"): ALPHA},
    )

    result = await flow.press()

    assert result == UpdateResult(release_no=1, is_up_to_date=False)
    release = flow.release(1)
    assert release.status == "pending"
    assert [(w.sha256, w.filename, w.source) for w in release.wheels] == [
        (sha256_of(ALPHA), wheel_name("alpha", "1.0.0"), "wheels/")
    ]
    assert await flow.wheel_store.has(sha256_of(ALPHA))


async def test_the_apps_own_folder_is_read_from_the_snapshot_not_from_the_workspace(
    tmp_path: Path,
) -> None:
    flow = Flow(
        tmp_path,
        pin("alpha", "1.0.0", ALPHA),
        wheels_in_snapshot={wheel_name("alpha", "1.0.0"): ALPHA},
    )

    await flow.press()

    assert flow.workspace_reads() == []


async def test_files_that_are_not_wheels_of_the_top_folder_are_not_candidates(
    tmp_path: Path,
) -> None:
    flow = Flow(
        tmp_path,
        pin("alpha", "1.0.0", ALPHA),
        wheels_in_snapshot={
            "notes.txt": ALPHA,
            f"nested/{wheel_name('alpha', '1.0.0')}": ALPHA,
        },
    )

    with pytest.raises(UpdateRefused) as refused:
        await flow.press()

    assert refused.value.kind == "refused"
    assert "no wheel for 'alpha==1.0.0'" in refused.value.message


# --- what the environment supplies ---------------------------------------------------------------


async def test_a_lock_the_environment_satisfies_records_no_wheel_and_reads_nothing(
    tmp_path: Path,
) -> None:
    flow = Flow(tmp_path, "fastapi==0.115.0\nuvicorn==0.30.6\n")

    await flow.press()

    release = flow.release(1)
    assert release.wheels == []
    assert flow.workspace_reads() == []
    assert "packages: 2 of 2 from the environment, 0 from wheels" in release.detected.warnings


async def test_a_wheel_that_replaces_the_environments_version_is_said_on_the_release(
    tmp_path: Path,
) -> None:
    flow = Flow(
        tmp_path,
        pin("appdirs", "99.0.0", ALPHA) + "\nfastapi==0.115.0\n",
        wheels_in_snapshot={wheel_name("appdirs", "99.0.0"): ALPHA},
    )

    await flow.press()

    release = flow.release(1)
    assert [w.filename for w in release.wheels] == [wheel_name("appdirs", "99.0.0")]
    assert "packages: 1 of 2 from the environment, 1 from wheels" in release.detected.warnings
    assert (
        "packages: appdirs 99.0.0 from a wheel replaces the environment's appdirs 1.4.4 "
        "for this release only"
    ) in release.detected.warnings


async def test_the_coverage_follows_the_warnings_the_manifest_already_has(tmp_path: Path) -> None:
    flow = Flow(tmp_path, "fastapi==0.115.0\n")
    flow.archives.archives[NEW_DIGEST] = archive_bytes(
        {
            "app.yaml": APP_YAML + b"env:\n  - name: MODE\n    value: demo\n    colour: red\n",
            "requirements.txt": b"fastapi==0.115.0\n",
        }
    )

    await flow.press()

    warnings = flow.release(1).detected.warnings
    assert len(warnings) == 2
    assert "colour" in warnings[0]
    assert warnings[1].startswith("packages: ")


async def test_a_snapshot_without_requirements_gets_no_resolution_and_no_line(
    tmp_path: Path,
) -> None:
    flow = Flow(tmp_path, None)

    await flow.press()

    release = flow.release(1)
    assert release.wheels == []
    assert release.detected.warnings == []
    assert flow.workspace_reads() == []


# --- where wheels are looked for -----------------------------------------------------------------


async def test_a_wheel_in_a_config_wheel_source_is_read_with_the_presser_token(
    tmp_path: Path,
) -> None:
    flow = Flow(tmp_path, pin("alpha", "1.0.0", ALPHA))
    flow.target_config(wheel_sources=[CONFIG_FOLDER])
    flow.put_in_workspace(CONFIG_FOLDER, wheel_name("alpha", "1.0.0"), ALPHA)

    await flow.press()

    release = flow.release(2)
    assert [(w.filename, w.source) for w in release.wheels] == [
        (wheel_name("alpha", "1.0.0"), CONFIG_FOLDER)
    ]
    assert {token for _, _, token in flow.workspace_reads()} == {PRESSER_TOKEN}
    assert await flow.wheel_store.has(sha256_of(ALPHA))


async def test_a_wheel_in_a_default_wheel_source_is_found(tmp_path: Path) -> None:
    flow = Flow(tmp_path, pin("alpha", "1.0.0", ALPHA))
    set_setting(flow.registry, "default_wheel_sources", [DEFAULT_FOLDER])
    flow.put_in_workspace(DEFAULT_FOLDER, wheel_name("alpha", "1.0.0"), ALPHA)

    await flow.press()

    assert [w.source for w in flow.release(1).wheels] == [DEFAULT_FOLDER]
    assert {token for _, _, token in flow.workspace_reads()} == {PRESSER_TOKEN}


async def test_the_order_is_the_apps_folder_then_config_sources_then_defaults(
    tmp_path: Path,
) -> None:
    flow = Flow(
        tmp_path,
        pin("alpha", "1.0.0", ALPHA)
        + "\n"
        + pin("beta", "1.0.0", BETA)
        + "\n"
        + pin("gamma", "1.0.0", GAMMA),
        wheels_in_snapshot={wheel_name("alpha", "1.0.0"): ALPHA},
    )
    flow.target_config(wheel_sources=[CONFIG_FOLDER])
    set_setting(flow.registry, "default_wheel_sources", [DEFAULT_FOLDER])
    flow.put_in_workspace(CONFIG_FOLDER, wheel_name("alpha", "1.0.0"), ALPHA)  # also on offer
    flow.put_in_workspace(CONFIG_FOLDER, wheel_name("beta", "1.0.0"), BETA)
    flow.put_in_workspace(DEFAULT_FOLDER, wheel_name("beta", "1.0.0"), BETA)  # also on offer
    flow.put_in_workspace(DEFAULT_FOLDER, wheel_name("gamma", "1.0.0"), GAMMA)

    await flow.press()

    assert [(w.filename.split("-")[0], w.source) for w in flow.release(2).wheels] == [
        ("alpha", "wheels/"),
        ("beta", CONFIG_FOLDER),
        ("gamma", DEFAULT_FOLDER),
    ]


async def test_a_folder_named_twice_is_read_once(tmp_path: Path) -> None:
    flow = Flow(tmp_path, pin("alpha", "1.0.0", ALPHA))
    flow.target_config(wheel_sources=[CONFIG_FOLDER])
    set_setting(flow.registry, "default_wheel_sources", [CONFIG_FOLDER])
    flow.put_in_workspace(CONFIG_FOLDER, wheel_name("beta", "1.0.0"), BETA)  # not the one asked

    with pytest.raises(UpdateRefused):
        await flow.press()

    listings = [path for op, path, _ in flow.workspace.calls if op == "list"]
    assert listings == [CONFIG_FOLDER]


async def test_a_wheel_the_store_already_holds_is_not_fetched_again(tmp_path: Path) -> None:
    flow = Flow(tmp_path, pin("alpha", "1.0.0", ALPHA))
    flow.target_config(wheel_sources=[CONFIG_FOLDER])
    # Release 1 installed this wheel; its bytes are in the content store.
    flow.registry.seed(
        "releases",
        {
            "app_slug": "sales",
            "release_no": 3,
            "snapshot_digest": digest_of(3),
            "detected": {"runtime": "python", "command": ["python", "app.py"]},
            "wheels": [
                {
                    "sha256": sha256_of(ALPHA),
                    "filename": wheel_name("alpha", "1.0.0"),
                    "source": CONFIG_FOLDER,
                }
            ],
            "status": "superseded",
            "status_at": NOW,
            "created_by": "alice@example.com",
            "created_at": NOW,
        },
    )
    holder = flow.wheel_store
    source = tmp_path / "alpha.whl"
    await asyncio.to_thread(source.write_bytes, ALPHA)
    await holder.add(sha256_of(ALPHA), source)

    await flow.press()

    assert [(w.filename, w.source) for w in flow.release(4).wheels] == [
        (wheel_name("alpha", "1.0.0"), STORE_SOURCE)
    ]
    assert flow.wheel_reads() == []


# --- what is refused ------------------------------------------------------------------------------


async def test_every_unresolved_requirement_is_listed_in_one_refusal(tmp_path: Path) -> None:
    flow = Flow(
        tmp_path,
        pin("beta", "2.0.0", BETA) + "\n" + pin("gamma", "3.0.0", GAMMA) + "\ndelta\n",
    )

    with pytest.raises(UpdateRefused) as refused:
        await flow.press()

    assert refused.value.kind == "refused"
    message = refused.value.message
    assert message.startswith("3 requirements cannot be installed: ")
    assert "no wheel for 'beta==2.0.0'" in message
    assert "no wheel for 'gamma==3.0.0'" in message
    assert "delta" in message
    assert "press Update again" in message
    assert PRESSER_TOKEN not in message


async def test_a_refused_update_writes_nothing(tmp_path: Path) -> None:
    flow = Flow(tmp_path, pin("beta", "2.0.0", BETA))

    with pytest.raises(UpdateRefused):
        await flow.press()

    assert flow.writer.calls == []
    assert flow.registry.writes == ()
    assert flow.registry.snapshot.releases_for("sales") == ()


async def test_a_refused_update_does_not_delete_a_release_to_make_room(tmp_path: Path) -> None:
    flow = Flow(tmp_path, pin("beta", "2.0.0", BETA))
    for number in range(1, 10):
        seed_release(flow.registry, "sales", number, "superseded")
    seed_release(flow.registry, "sales", 10, "active")

    with pytest.raises(UpdateRefused):
        await flow.press()

    assert all(r.deleted_at is None for r in flow.registry.snapshot.releases_for("sales"))
    assert flow.registry.writes == ()


async def test_a_pin_without_a_hash_is_refused_with_what_to_add(tmp_path: Path) -> None:
    flow = Flow(tmp_path, "beta==2.0.0\n")

    with pytest.raises(UpdateRefused) as refused:
        await flow.press()

    assert "--hash=sha256" in refused.value.message


async def test_a_line_no_build_can_install_is_refused_in_every_mode(tmp_path: Path) -> None:
    for mode in ("wheelhouse", "index", "wheelhouse+index"):
        flow = Flow(tmp_path / mode, "-r other.txt\n")
        set_setting(flow.registry, "package_mode", mode)

        with pytest.raises(UpdateRefused) as refused:
            await flow.press()

        assert refused.value.kind == "refused", mode
        assert "-r" in refused.value.message, mode


async def test_a_requirements_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    flow = Flow(tmp_path, b"\xff\xfe not text")

    with pytest.raises(UpdateRefused) as refused:
        await flow.press()

    assert refused.value.kind == "refused"
    assert "requirements.txt is not UTF-8 text" in refused.value.message


# --- the package modes ----------------------------------------------------------------------------


async def test_index_mode_reads_no_wheel_and_leaves_the_rest_to_the_index(tmp_path: Path) -> None:
    flow = Flow(
        tmp_path,
        "fastapi==0.115.0\nbeta\n",
        wheels_in_snapshot={wheel_name("beta", "1.0.0"): BETA},
    )
    set_setting(flow.registry, "package_mode", "index")

    await flow.press()

    release = flow.release(1)
    assert release.wheels == []
    assert flow.workspace_reads() == []
    assert "packages: 1 of 2 from the environment, 0 from wheels, 1 from the index" in (
        release.detected.warnings
    )


async def test_wheelhouse_with_index_takes_the_rest_from_the_index(tmp_path: Path) -> None:
    flow = Flow(
        tmp_path,
        pin("alpha", "1.0.0", ALPHA) + "\ndelta\n",
        wheels_in_snapshot={wheel_name("alpha", "1.0.0"): ALPHA},
    )
    set_setting(flow.registry, "package_mode", "wheelhouse+index")

    await flow.press()

    release = flow.release(1)
    assert [w.filename for w in release.wheels] == [wheel_name("alpha", "1.0.0")]
    assert (
        "packages: 0 of 2 from the environment, 1 from wheels, 1 from the index"
        in release.detected.warnings
    )


# --- failures that may pass ---------------------------------------------------------------------


async def test_a_session_that_ends_while_wheels_are_read_asks_to_press_again(
    tmp_path: Path,
) -> None:
    flow = Flow(tmp_path, pin("alpha", "1.0.0", ALPHA))
    flow.target_config(wheel_sources=[CONFIG_FOLDER])
    flow.put_in_workspace(CONFIG_FOLDER, wheel_name("alpha", "1.0.0"), ALPHA)
    flow.workspace.expire_after_calls = 0

    with pytest.raises(UpdateRefused) as refused:
        await flow.press()

    assert refused.value.kind == "unavailable"
    assert "session expired" in refused.value.message
    assert "press Update again" in refused.value.message
    assert PRESSER_TOKEN not in refused.value.message
    assert flow.writer.calls == []


async def test_a_workspace_that_fails_while_wheels_are_read_is_unavailable(
    tmp_path: Path,
) -> None:
    flow = Flow(tmp_path, pin("alpha", "1.0.0", ALPHA))
    flow.target_config(wheel_sources=[CONFIG_FOLDER])
    flow.put_in_workspace(CONFIG_FOLDER, wheel_name("alpha", "1.0.0"), ALPHA)
    flow.workspace.failing_status[f"{CONFIG_FOLDER}/{wheel_name('alpha', '1.0.0')}"] = 500

    with pytest.raises(UpdateRefused) as refused:
        await flow.press()

    assert refused.value.kind == "unavailable"
    assert "the workspace answered 500" in refused.value.message
    assert flow.writer.calls == []


async def test_a_content_store_that_cannot_be_written_is_unavailable(tmp_path: Path) -> None:
    flow = Flow(
        tmp_path,
        pin("alpha", "1.0.0", ALPHA),
        wheels_in_snapshot={wheel_name("alpha", "1.0.0"): ALPHA},
        store_class=FailingStore,
    )

    with pytest.raises(UpdateRefused) as refused:
        await flow.press()

    assert refused.value.kind == "unavailable"
    assert "content store" in refused.value.message
    assert flow.writer.calls == []


# --- a retried write ------------------------------------------------------------------------------


async def test_a_write_that_lost_a_race_does_not_fetch_the_wheels_again(tmp_path: Path) -> None:
    flow = Flow(tmp_path, pin("alpha", "1.0.0", ALPHA))
    flow.target_config(wheel_sources=[CONFIG_FOLDER])
    flow.put_in_workspace(CONFIG_FOLDER, wheel_name("alpha", "1.0.0"), ALPHA)
    flow.registry.fail_next_write_with = ChangedByOthers()

    await flow.press()

    assert len(flow.wheel_reads()) == 1
    assert [w.filename for w in flow.release(2).wheels] == [wheel_name("alpha", "1.0.0")]


# --- the row that is written ----------------------------------------------------------------------


async def test_the_release_insert_carries_the_wheel_list(tmp_path: Path) -> None:
    flow = Flow(
        tmp_path,
        pin("alpha", "1.0.0", ALPHA),
        wheels_in_snapshot={wheel_name("alpha", "1.0.0"): ALPHA},
    )

    await flow.press()

    insert = flow.writer.calls[0][0]
    assert insert.kind is WriteKind.RELEASE_INSERT
    assert insert.values["wheels"] == [
        {
            "sha256": sha256_of(ALPHA),
            "filename": wheel_name("alpha", "1.0.0"),
            "source": "wheels/",
        }
    ]


def test_insert_release_change_omits_the_wheel_column_when_there_are_none() -> None:
    change = insert_release_change(
        app_slug="sales",
        release_no=1,
        snapshot_digest=NEW_DIGEST,
        detected=DetectedManifest(runtime="python"),
        config=ReleaseConfig(),
        created_by="dana@example.com",
    )

    assert "wheels" not in change.values


def test_insert_release_change_stores_the_wheels_as_json_data() -> None:
    entry = WheelEntry(sha256=sha256_of(ALPHA), filename="a-1-py3-none-any.whl", source="wheels/")

    change = insert_release_change(
        app_slug="sales",
        release_no=1,
        snapshot_digest=NEW_DIGEST,
        detected=DetectedManifest(runtime="python"),
        config=ReleaseConfig(),
        created_by="dana@example.com",
        wheels=[entry],
    )

    assert change.values["wheels"] == [entry.model_dump(mode="json")]


# --- an Update without package resolution stays as it was -----------------------------------------


async def test_a_service_without_packages_records_no_wheels(tmp_path: Path) -> None:
    flow = Flow(tmp_path, pin("beta", "2.0.0", BETA), with_packages=False)

    result = await flow.press()

    assert result.is_up_to_date is False
    assert flow.release(1).wheels == []
    assert flow.release(1).detected.warnings == []
