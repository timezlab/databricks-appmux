"""The child's run directory is capped, and what its pages cost (T060, US3, TD-004, FR-011).

``disk_filler`` writes into ``/run/appmux`` until the filesystem refuses. The directory is a
``tmpfs`` of ``RUN_DIR_SIZE_MB`` inside the sandbox, so:

* the writer is stopped with ``ENOSPC`` at that size, long before the fixture's own ceiling, and
  the child goes on answering (a full run directory is not a crash);
* the pages of that ``tmpfs`` are memory. Whether they show in the child's own figure
  (``rss_mb_max``, the process tree's resident memory, which the watchdog and the budget read) or
  only in the container's (``mem_used_mb`` of the ``_platform`` row) is the second half of TD-004.
  The test records both figures and asserts the answer that was measured, so a platform change
  that moves it fails here.

The host-backed run directory of a shared-service-principal child, and the size watchdog beside
it (``sandbox/run_dir.py``), are not driven here.

It does not run in the default gate: every test here is marked ``integration`` and skipped unless
``APPMUX_TEST_CONTAINER=1``. Run it with ``scripts/run-remote-tests.sh``; the setup is that of
``ops_session.py``. Everything the run registers is deleted afterwards.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
from ops_session import REQUEST_TIMEOUT_S, AdminSession, Target, load_target

from appmux.sandbox.run_dir import RUN_DIR_SIZE_MB

pytestmark = pytest.mark.integration

SLUG_SUFFIX_BYTES: Final = 3
# A tmpfs of this size holds the size minus nothing the file needs; allow a megabyte for rounding.
SIZE_TOLERANCE_MB: Final = 1
# The sampler reads the figures every 15 s and the writer flushes a minute a minute, so a figure
# for a whole minute after an event is there about two minutes later.
ROWS_WANTED: Final = 2
ROWS_WAIT_S: Final = 300.0
ROWS_POLL_S: Final = 10.0
PLATFORM_SLUG: Final = "_platform"
# The container reads more than the child's own figure: the pages written must show at least this
# share of what was written (the rest is noise from other work in the container).
CONTAINER_SHARE_OF_WRITE: Final = 0.5
# The child's own figure moves by its allocator, not by 64 MiB of tmpfs pages.
CHILD_RSS_NOISE_MB: Final = 16


@pytest.fixture(scope="module")
def target() -> Target:
    return load_target()


@pytest.fixture
def session(target: Target) -> Iterator[AdminSession]:
    with httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as client:
        opened = AdminSession(client, target)
        try:
            yield opened
        finally:
            opened.delete_created_apps()


def _start(session: AdminSession, slug: str) -> None:
    session.register(slug, fixture="disk_filler")
    session.wait_for_release(slug, session.press_update(slug), "active")
    session.wait_for_state(slug, "running")


def _fill(session: AdminSession, slug: str) -> dict[str, Any]:
    answer = session.open_app(slug, "/report")
    assert answer.status_code == 200, f"disk_filler/report answered {answer.status_code}"
    report: dict[str, Any] = answer.json()
    assert report["fixture"] == "disk_filler"
    return report


def _next_whole_minute() -> datetime:
    now = datetime.now(UTC)
    return now.replace(second=0, microsecond=0) + timedelta(minutes=1)


def _minute_of(row: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(row["minute"]).replace("Z", "+00:00"))


def _rows_since(session: AdminSession, slug: str, since: datetime) -> list[dict[str, Any]]:
    rows = session.read("/metrics?step=1m")["items"]
    return [row for row in rows if row["app_slug"] == slug and _minute_of(row) >= since]


def _wait_for_rows(session: AdminSession, slug: str, since: datetime) -> list[dict[str, Any]]:
    """The rows of ``slug`` for whole minutes from ``since`` on, once there are enough of them."""
    deadline = time.monotonic() + ROWS_WAIT_S
    rows: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        rows = _rows_since(session, slug, since)
        if len(rows) >= ROWS_WANTED:
            return rows
        time.sleep(ROWS_POLL_S)
    raise AssertionError(
        f"only {len(rows)} per-minute rows of {slug} since {since:%H:%M} after {ROWS_WAIT_S:.0f} s"
    )


def _peak(rows: list[dict[str, Any]], column: str) -> int:
    figures = [int(row[column]) for row in rows if row[column] is not None]
    assert figures, f"no row carries {column}"
    return max(figures)


def test_a_writer_is_stopped_at_the_size_of_the_run_directory(session: AdminSession) -> None:
    slug = f"zd-cap-{secrets.token_hex(SLUG_SUFFIX_BYTES)}"
    _start(session, slug)

    report = _fill(session, slug)
    print(
        f"run directory: {report['written_bytes']} bytes written, refused with "
        f"{report['refused_with']}, filesystem of {report['mount_size_mb']} MiB, "
        f"{report['elapsed_s']} s"
    )

    assert report["refused_with"] == "ENOSPC", report
    assert not report["stopped_by_ceiling"], "no cap was enforced"
    assert report["mount_size_mb"] == RUN_DIR_SIZE_MB, report
    assert RUN_DIR_SIZE_MB - SIZE_TOLERANCE_MB <= report["written_mb"] <= RUN_DIR_SIZE_MB, report
    # A full run directory is a failed write, not a dead child.
    assert session.open_app(slug, "/").status_code == 200


def test_the_pages_of_the_run_directory_show_in_the_container_and_not_in_the_child(
    session: AdminSession,
) -> None:
    slug = f"zd-mem-{secrets.token_hex(SLUG_SUFFIX_BYTES)}"
    _start(session, slug)

    before_since = _next_whole_minute()
    container_before = _peak(_wait_for_rows(session, PLATFORM_SLUG, before_since), "mem_used_mb")
    child_before = _peak(_wait_for_rows(session, slug, before_since), "rss_mb_max")

    report = _fill(session, slug)
    assert report["refused_with"] == "ENOSPC", report
    written_mb = int(report["written_mb"])

    after_since = _next_whole_minute()
    container_after = _peak(_wait_for_rows(session, PLATFORM_SLUG, after_since), "mem_used_mb")
    child_after = _peak(_wait_for_rows(session, slug, after_since), "rss_mb_max")
    session.open_app(slug, "/clear")

    print(
        f"wrote {written_mb} MiB: the container went from {container_before} to "
        f"{container_after} MiB, the child's own figure from {child_before} to {child_after} MiB"
    )
    assert container_after - container_before >= written_mb * CONTAINER_SHARE_OF_WRITE, (
        f"the container grew by {container_after - container_before} MiB for {written_mb} MiB of "
        "tmpfs pages"
    )
    assert child_after - child_before <= CHILD_RSS_NOISE_MB, (
        f"the child's own figure grew by {child_after - child_before} MiB: the pages count in it"
    )
