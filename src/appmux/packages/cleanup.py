"""The daily content-store clean-up (T116; data model rule 11).

Snapshots and wheels pile up in the content store: deleting a release only drops its pointers, so
the files stay until this job removes them. Rule 11 says when that is allowed: **a file is removed
only when no live release references it and it is older than a day**. Removing a file a release
needs is unrecoverable (the source folder it was taken from may have changed since), while keeping
one for another day costs a few megabytes, so every doubt here resolves to *keep*:

* the age condition protects files of an Update that has stored them but not yet written its release
  row (the row is inserted after the build, design 5.2);
* a file with no modification time, or one dated in the future, counts as young;
* only entries named by a bare lower-case sha256 are ever candidates, so a folder or a note someone
  left in the store folder is left alone;
* a failed listing or an unreadable or malformed reference list removes nothing, because without
  the list of what releases need every old file looks unreferenced.

The job has two seams: ``StoredFiles`` (the store folder listing with modification times, and
delete; ``ContentStore`` implements it) and a reader for the digests live releases reference
(``registry_reference_reader``, built on the registry snapshot). ``run_cleanup_once`` ties them for
the daily schedule and never raises for an expected failure: it logs why it skipped and returns
``None``.

One race remains that this module cannot close, because it sits in the Update flow. The references
are read *after* the listing, so a release inserted in between protects the file it reuses. But an
Update that finds an *old* unreferenced file already in the store and reuses it is protected only
from the moment its release row names the file: the reuse should refresh the file (store it again)
or write the wheel list before the build starts.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

from appmux.packages.store import ContentStoreError, StoredFile, WorkspaceError
from appmux.registry.errors import RegistryUnavailable
from appmux.registry.protocol import Registry

logger = logging.getLogger(__name__)

# Rule 11's "older than a day". A release is created within one build timeout of its files being
# stored (build_timeout_s defaults to 600 s), so a day leaves a wide margin.
MIN_AGE: Final = timedelta(days=1)

_FILE_NAME: Final = re.compile(r"[0-9a-f]{64}")
_DIGEST_PREFIX: Final = "sha256:"
_NOT_READABLE: Final = (
    "the registry could not be read just now, so what releases need is unknown; "
    "the clean-up removed nothing and will try again at the next run"
)


class MalformedReference(ValueError):
    """A release reference is not a digest in the shape its column holds; nothing is removed."""


class CleanupSkipped(Exception):
    """The registry cannot say what live releases need, so the run removes nothing."""


class StoredFiles(Protocol):
    """The content-store folder as the clean-up needs it."""

    async def list_files(self) -> Sequence[StoredFile]: ...

    async def delete_file(self, name: str) -> None: ...


# Reads the file names (bare sha256 hex) that live releases reference, from the registry.
ReferenceReader = Callable[[], Awaitable[Iterable[str]]]


@dataclass(frozen=True, slots=True)
class CleanupFailure:
    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class CleanupReport:
    removed: tuple[str, ...]
    kept_referenced: int
    kept_recent: int  # includes files whose age is unknown or in the future
    ignored: int  # entries not named by a digest
    failed: tuple[CleanupFailure, ...]

    def summary(self) -> str:
        parts = [
            f"removed {_count(len(self.removed), 'file', 'files')}",
            f"kept {self.kept_referenced} in use and {self.kept_recent} under a day old",
        ]
        if self.ignored:
            parts.append(f"left {_count(self.ignored, 'other entry', 'other entries')} alone")
        if self.failed:
            parts.append(f"{_count(len(self.failed), 'file', 'files')} could not be removed")
        return "; ".join(parts)


def referenced_digests(
    *, snapshot_digests: Iterable[str], wheel_sha256s: Iterable[str]
) -> frozenset[str]:
    """The store file names that live releases reference.

    A snapshot digest is stored as ``sha256:<hex>`` (``releases.snapshot_digest``) and a wheel's as
    bare hex (``releases.wheels[].sha256``); the store names its files by the bare hex.
    """
    names: set[str] = set()
    for digest in snapshot_digests:
        if not digest.startswith(_DIGEST_PREFIX):
            raise MalformedReference(
                f"the snapshot digest '{digest}' does not start with '{_DIGEST_PREFIX}'; "
                "nothing was removed from the content store"
            )
        names.add(_require_file_name(digest.removeprefix(_DIGEST_PREFIX)))
    names.update(_require_file_name(sha256) for sha256 in wheel_sha256s)
    return frozenset(names)


async def clean_content_store(
    files: StoredFiles,
    read_references: ReferenceReader,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CleanupReport:
    """Remove the files rule 11 allows to go; raise, removing nothing, if it cannot tell."""
    listing = await files.list_files()
    # After the listing, so a release created in between protects the files it reuses.
    referenced = {_require_file_name(name) for name in await read_references()}
    current_time = now()

    to_remove: list[str] = []
    kept_referenced = kept_recent = ignored = 0
    for entry in listing:
        if _FILE_NAME.fullmatch(entry.name) is None:
            ignored += 1
        elif entry.name in referenced:
            kept_referenced += 1
        elif entry.modified_at is None or current_time - entry.modified_at <= MIN_AGE:
            kept_recent += 1  # an unknown or future time gives a negative or None age: keep
        else:
            to_remove.append(entry.name)

    removed: list[str] = []
    failed: list[CleanupFailure] = []
    for name in to_remove:  # one at a time: parallel workspace calls get 429s (spike 4)
        try:
            await files.delete_file(name)
        except (WorkspaceError, ContentStoreError) as exc:
            # One file that cannot go must not keep the rest: the next daily run tries it again.
            failed.append(CleanupFailure(name, _reason(exc)))
        else:
            removed.append(name)
    return CleanupReport(
        removed=tuple(removed),
        kept_referenced=kept_referenced,
        kept_recent=kept_recent,
        ignored=ignored,
        failed=tuple(failed),
    )


def registry_reference_reader(registry: Registry) -> ReferenceReader:
    """A ``ReferenceReader`` over the registry: what live releases' snapshots and wheels name.

    It reads the registry again first, because a stale snapshot misses a release created since.
    It raises ``CleanupSkipped`` (so nothing is removed) when the registry cannot be read, and when
    a release row failed its model: a quarantined row is left out of the snapshot but may still
    reference files, and without it those files look unreferenced.
    """

    async def read() -> frozenset[str]:
        try:
            await registry.refresh()
        except RegistryUnavailable as exc:
            raise CleanupSkipped(_NOT_READABLE) from exc
        if registry.last_error is not None:
            raise CleanupSkipped(_NOT_READABLE)
        snapshot = registry.snapshot
        if any(row.table == "releases" for row in snapshot.quarantined):
            raise CleanupSkipped(
                "a release row of the registry failed its checks and may still reference stored "
                "files; the clean-up removed nothing. Fix or delete the row, then wait for the "
                "next run"
            )
        live = [release for release in snapshot.releases if release.deleted_at is None]
        return referenced_digests(
            snapshot_digests=[r.snapshot_digest for r in live if r.snapshot_digest is not None],
            wheel_sha256s=[wheel.sha256 for r in live for wheel in r.wheels],
        )

    return read


async def run_cleanup_once(
    files: StoredFiles,
    registry: Registry,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CleanupReport | None:
    """One daily run: list the store, read what releases need, remove what rule 11 allows.

    Returns the report, or ``None`` when the run could not tell what is safe and removed nothing
    (the reason is logged). It does not raise for the failures it expects (the store or the
    registry not answering, a malformed reference), so a scheduler can call it in a loop; the next
    run tries again.
    """
    try:
        report = await clean_content_store(files, registry_reference_reader(registry), now=now)
    except (CleanupSkipped, MalformedReference, ContentStoreError, WorkspaceError) as exc:
        logger.warning("content-store clean-up skipped: %s", exc)
        return None
    if report.failed:
        for failure in report.failed:
            logger.warning(
                "content-store clean-up could not remove %s: %s", failure.name, failure.reason
            )
    logger.info("content-store clean-up: %s", report.summary())
    return report


def _require_file_name(text: str) -> str:
    if _FILE_NAME.fullmatch(text) is None:
        raise MalformedReference(
            f"'{text}' is not a lower-case sha256 digest; nothing was removed from the store"
        )
    return text


def _reason(exc: WorkspaceError | ContentStoreError) -> str:
    if isinstance(exc, WorkspaceError) and exc.status is not None:
        return f"{exc} (HTTP {exc.status})"
    return str(exc)


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"
