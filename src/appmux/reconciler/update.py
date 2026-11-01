"""The Update flow of design 5.2, steps 1 to 5: from a press of the button to a pending release.

``authorize()`` and the audit entry belong to the admin route (it calls this flow through
``ReleaseUpdater`` and supplies ``write``); this module does the rest:

1. snapshot the app's folder with the presser's own token (``SnapshotSource``) and learn whether it
   equals a live release;
2. read the snapshot's ``app.yaml`` (and ``databricks.yml`` offers) back from the content store and
   build the manifest; an invalid ``app.yaml`` refuses the Update;
3. resolve the release's packages (FR-031, ``reconciler/update_packages.py``): store the wheels it
   needs in the content store and learn the wheel list the release will record; a requirement no
   source can supply refuses the Update, listing every such requirement at once, *before* anything
   is deleted or written (so a refused Update changes nothing but the stored wheels);
4. make room: at the release cap, delete the oldest unprotected release first (its own
   transaction, recorded as ``release.delete`` by ``appmux``), or refuse the Update when every
   release is protected (``reconciler/retention.py``);
5. check that the app has every resource the release's ``valueFrom`` keys name (FR-035, before any
   build): a release that lacks some is stored ``failed`` with the missing keys as its reason,
   so the reconciler, which builds only ``pending`` releases, never starts it;
6. write one release row (status ``pending``, or ``failed`` by step 5, with its wheel list) and
   point ``app_config.target_release_no`` at it, in the single ``write`` the route turns into one
   audited transaction.

Building and starting the release are the reconciler's: the route wakes it after a release was
written (``ReleaseRoutes(on_release_created=reconciler.wake)``), so this flow never signals it.

"Live" for the up-to-date answer means a release that is, or is about to be, serving: ``pending``
through ``active``. A folder equal to a ``failed`` or ``superseded`` release gets a new release
instead, because "already up to date" would be false for the person: that code is not running.
"""

from __future__ import annotations

import asyncio
import shutil
import tarfile
import tempfile
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from pydantic import SecretStr

from appmux.appyaml.appyaml import MAX_APP_YAML_BYTES, AppYamlInvalid, read_manifest
from appmux.appyaml.bundle import MAX_BUNDLE_BYTES
from appmux.appyaml.requirements import check_requirements
from appmux.identity.actor import Actor
from appmux.packages.store import ContentStoreError
from appmux.reconciler.build import ArchiveStore
from appmux.reconciler.ports import ChangeWriter, UpdateRefused, UpdateResult
from appmux.reconciler.retention import (
    RETENTION_ACTOR,
    RetentionRefused,
    cleanup_audit,
    cleanup_changes,
    releases_to_delete,
    retention_limits,
)
from appmux.reconciler.snapshot_source import (
    ARCHIVE_NAME,
    SESSION_EXPIRED_REASON,
    Snapshot,
    SnapshotRefused,
)
from appmux.reconciler.update_packages import PackagePlan, PackageSession, UpdatePackages
from appmux.registry.errors import ChangedByOthers, RowAlreadyExists
from appmux.registry.models import (
    AppConfig,
    DetectedManifest,
    Release,
    ReleaseConfig,
    WheelEntry,
)
from appmux.registry.protocol import Change, Registry
from appmux.registry.releases import insert_release_change, next_release_no
from appmux.registry.snapshot import RegistrySnapshot
from appmux.registry.writers import WriteKind

# The Update writes against a registry another Update may be writing to at the same moment; a
# clash is answered by reading again, a few times, and then by asking the person to press again.
MAX_WRITE_ATTEMPTS: Final = 3
SERVING_STATUSES: Final = frozenset({"pending", "building", "starting", "healthy", "active"})
# One byte more than the larger limit, so the parser sees "too large" instead of a cut-off file.
_MAX_MANIFEST_READ_BYTES: Final = max(MAX_APP_YAML_BYTES, MAX_BUNDLE_BYTES) + 1


class SnapshotTaker(Protocol):
    """``SnapshotSource.take``, as the flow uses it."""

    async def take(
        self, *, source_path: str, token: str, live_digests: Collection[str]
    ) -> Snapshot: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class UpdateService:
    def __init__(
        self,
        *,
        registry: Registry,
        snapshots: SnapshotTaker,
        store: ArchiveStore,
        packages: UpdatePackages | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._registry = registry
        self._snapshots = snapshots
        self._store = store
        # ``None`` records no wheels and no package lines (a service built without the wheel store).
        self._packages = packages
        self._now = now

    async def update(
        self, *, actor: Actor, app_slug: str, user_token: SecretStr, write: ChangeWriter
    ) -> UpdateResult:
        app = self._registry.snapshot.app(app_slug)
        if app is None:
            raise UpdateRefused("not_found", f"there is no app '{app_slug}'")

        serving = _serving_releases(self._registry.snapshot, app_slug)
        taken = await self._take(
            app.source_path, user_token, {r.snapshot_digest for r in serving if r.snapshot_digest}
        )
        same_folder = _release_with_digest(serving, taken.digest)
        if same_folder is not None:
            return UpdateResult(release_no=same_folder.release_no, is_up_to_date=True)

        async with self._stored_archive(taken.digest) as archive:
            detected = await self._manifest_of(archive, taken.digest)
            session = await self._packages_of(archive, user_token)
            for _ in range(MAX_WRITE_ATTEMPTS):
                snapshot = self._registry.snapshot
                if snapshot.app(app_slug) is None:
                    raise UpdateRefused("not_found", f"there is no app '{app_slug}'")
                same_folder = _release_with_digest(
                    _serving_releases(snapshot, app_slug), taken.digest
                )
                if same_folder is not None:
                    # Another Update of this same folder got there first.
                    return UpdateResult(release_no=same_folder.release_no, is_up_to_date=True)
                try:
                    config = snapshot.app_config(app_slug)
                    if config is None:
                        raise _no_settings_row(app_slug)
                    release_config = _config_to_copy(snapshot, app_slug, config)
                    # Before the clean-up on purpose: a refused Update must not delete a release.
                    # The config it is read from is the target's, which the clean-up never deletes.
                    plan = await self._plan_packages(session, snapshot, app_slug, release_config)
                    snapshot = await self._make_room(snapshot, app_slug)
                    release_no = next_release_no(snapshot, app_slug)
                    changes = _changes_of_an_update(
                        snapshot,
                        app_slug,
                        release_no,
                        taken.digest,
                        _with_notes(detected, plan),
                        actor.email,
                        release_config=release_config,
                        wheels=plan.wheels,
                    )
                    await write(changes, release_no=release_no)
                except (RowAlreadyExists, ChangedByOthers):
                    await self._registry.refresh()
                    continue
                return UpdateResult(release_no=release_no, is_up_to_date=False)
        raise UpdateRefused(
            "conflict",
            f"'{app_slug}' was changed by someone else while this Update was saved; "
            "press Update again",
        )

    async def _packages_of(self, archive: Path, user_token: SecretStr) -> PackageSession | None:
        """The package session of this Update, or ``None`` if it has nothing to resolve."""
        if self._packages is None:
            return None
        return await self._packages.open(archive=archive, token=user_token)

    async def _plan_packages(
        self,
        session: PackageSession | None,
        snapshot: RegistrySnapshot,
        app_slug: str,
        release_config: ReleaseConfig,
    ) -> PackagePlan:
        if session is None:
            return PackagePlan(wheels=(), notes=())
        return await session.plan_for(snapshot, app_slug, release_config)

    async def _make_room(self, snapshot: RegistrySnapshot, app_slug: str) -> RegistrySnapshot:
        """Delete what the release cap requires before a release is added; the fresh snapshot.

        This is its own transaction, written by the service principal and recorded as
        ``appmux``, and it comes first on purpose (design 5.2): the old target is still the
        target, so it stays protected. An Update that fails after this step leaves the deleted
        release deleted, which the cap would have required for the next Update anyway.
        """
        try:
            doomed = releases_to_delete(snapshot, app_slug)
        except RetentionRefused as exc:
            raise UpdateRefused("conflict", str(exc)) from exc
        if not doomed:
            return snapshot
        if snapshot.app_config(app_slug) is None:
            raise _no_settings_row(app_slug)
        changes = cleanup_changes(
            snapshot, app_slug, doomed, deleted_by=RETENTION_ACTOR, now=self._now()
        )
        audit = cleanup_audit(
            app_slug, doomed, max_releases=retention_limits(snapshot).max_releases
        )
        await self._registry.write(changes, audit)
        return self._registry.snapshot

    async def _take(
        self, source_path: str, user_token: SecretStr, live_digests: Collection[str]
    ) -> Snapshot:
        try:
            return await self._snapshots.take(
                source_path=source_path,
                token=user_token.get_secret_value(),
                live_digests=live_digests,
            )
        except SnapshotRefused as exc:
            # The snapshot step words its refusals for the person pressing Update.
            if str(exc) == SESSION_EXPIRED_REASON:
                raise UpdateRefused("unavailable", str(exc)) from exc
            raise UpdateRefused("refused", str(exc)) from exc

    @asynccontextmanager
    async def _stored_archive(self, digest: str) -> AsyncIterator[Path]:
        """The stored snapshot as a local file, for as long as the Update needs to read it."""
        scratch = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="appmux-update-"))
        try:
            archive = scratch / ARCHIVE_NAME
            try:
                await self._store.get(digest, archive)
            except ContentStoreError as exc:
                raise UpdateRefused("unavailable", str(exc)) from exc
            yield archive
        finally:
            await asyncio.shield(asyncio.to_thread(shutil.rmtree, scratch, True))

    async def _manifest_of(self, archive: Path, digest: str) -> DetectedManifest:
        """The manifest of the stored snapshot: the files at its root, read from the archive."""
        try:
            return await asyncio.to_thread(_read_manifest_from, archive)
        except AppYamlInvalid as exc:
            raise UpdateRefused("refused", str(exc)) from exc
        except tarfile.TarError as exc:
            raise UpdateRefused(
                "unavailable",
                f"the stored snapshot {digest} is not a readable archive; press Update again",
            ) from exc


def _read_manifest_from(archive_path: Path) -> DetectedManifest:
    with tarfile.open(archive_path, mode="r:") as archive:
        root_files = {
            member.name
            for member in archive.getmembers()
            if member.isreg() and "/" not in member.name
        }

        def read_file(name: str) -> bytes:
            source = archive.extractfile(name)
            if source is None:  # a root file was listed above, so this is a defensive guard
                raise AppYamlInvalid(f"{name} could not be read from the snapshot")
            with source:
                return source.read(_MAX_MANIFEST_READ_BYTES)

        return read_manifest(root_files, read_file)


def _serving_releases(snapshot: RegistrySnapshot, app_slug: str) -> list[Release]:
    return [
        release
        for release in snapshot.releases_for(app_slug)
        if release.deleted_at is None and release.status in SERVING_STATUSES
    ]


def _release_with_digest(releases: list[Release], digest: str) -> Release | None:
    """The release holding ``digest``: the active one if it does, else the newest."""
    matching = [release for release in releases if release.snapshot_digest == digest]
    if not matching:
        return None
    return max(matching, key=lambda release: (release.status == "active", release.release_no))


def _with_notes(detected: DetectedManifest, plan: PackagePlan) -> DetectedManifest:
    """The manifest with the package lines after its own warnings (the release page lists both)."""
    if not plan.notes:
        return detected
    return detected.model_copy(update={"warnings": [*detected.warnings, *plan.notes]})


def _changes_of_an_update(
    snapshot: RegistrySnapshot,
    app_slug: str,
    release_no: int,
    digest: str,
    detected: DetectedManifest,
    created_by: str,
    *,
    release_config: ReleaseConfig,
    wheels: Sequence[WheelEntry],
) -> list[Change]:
    config = snapshot.app_config(app_slug)
    if config is None:
        raise _no_settings_row(app_slug)
    check = check_requirements(
        detected.env,
        release_config.env,
        resource_keys={resource.key for resource in snapshot.resources_for(app_slug)},
    )
    return [
        insert_release_change(
            app_slug=app_slug,
            release_no=release_no,
            snapshot_digest=digest,
            detected=detected,
            config=release_config,
            created_by=created_by,
            failed_reason=None if check.is_met else check.message(),
            wheels=wheels,
        ),
        _make_target(app_slug, release_no, config, created_by),
    ]


def _no_settings_row(app_slug: str) -> UpdateRefused:
    return UpdateRefused(
        "unavailable", f"'{app_slug}' has no settings row yet; try again in a moment"
    )


def _config_to_copy(snapshot: RegistrySnapshot, app_slug: str, config: AppConfig) -> ReleaseConfig:
    """The target release's config; failing that the active release's; failing that, none."""
    candidates: list[Release | None] = []
    if config.target_release_no is not None:
        candidates.append(snapshot.release(app_slug, config.target_release_no))
    candidates.extend(
        release for release in snapshot.releases_for(app_slug) if release.status == "active"
    )
    for release in candidates:
        if release is not None and release.deleted_at is None:
            return release.config
    return ReleaseConfig()


def _make_target(app_slug: str, release_no: int, config: AppConfig, updated_by: str) -> Change:
    values: Mapping[str, object] = {
        "target_release_no": release_no,
        # Pressing Update is asking for the app to run: a new app starts stopped, and nothing else
        # would start it (design 5.2 step 5 sets only the target; the example flow T096 needs both).
        "desired_state": "running",
        "updated_by": updated_by,
    }
    return Change(
        WriteKind.APP_CONFIG_UPDATE,
        key={"app_slug": app_slug},
        values=values,
        expected_version=config.version,
    )
