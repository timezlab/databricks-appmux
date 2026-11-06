"""A child that fills its tmpfs mounts is stopped by the memory watchdog like one that grows (T219).

The pages of a tmpfs are memory in the container's cgroup but in no process's RSS, so a child could
keep its files in ``/tmp`` and ``/dev/shm`` and never meet the RSS watchdog (TD-004). The watchdog
now reads the used bytes of the child's memory-backed mounts through ``/proc/<pid>/root`` and adds
them to the resident memory before it compares with ``budget * 1.25 + 64 MB``.

``disk_filler`` is the child. The test keeps its numbers apart so that the stop can only come from
the sum:

* the five mounts together hold 272 MB (the sizes of ``rootfs`` and ``run_dir``), under the limit
  of 384 MB for the 256 MB budget;
* its resident memory plus a 100 MB heap is far under the limit too;
* the mounts, the heap and the resident memory together are over it.

Before that, a child holding about half of the mounts is left alone for several watchdog rounds:
the watchdog does not stop an app for using its temporary directories. What stops the child is
read from the app's logs, where the reconciler writes the reason the watchdog gave.

If the logs have no such line, check the App's own log for ``cannot read the tmpfs mounts``: the
container then refused ``/proc/<pid>/root`` to appmux, and the watchdog fell back to the resident
memory alone.

It is marked ``integration`` and skipped unless ``APPMUX_TEST_CONTAINER=1``. It plays through the
proxy, so ``scripts/run-remote-tests.sh`` runs it; the setup is that of ``ops_session.py``.
Everything the run registers is deleted afterwards.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Iterator
from typing import Any, Final

import httpx
import pytest
from ops_session import (
    BUDGET_MB,
    POLL_INTERVAL_S,
    REQUEST_TIMEOUT_S,
    AdminSession,
    Target,
    load_target,
)

from appmux.reconciler.admission import OVERHEAD_PER_CHILD_MB
from appmux.sandbox.rootfs import (
    HOME_PATH,
    HOME_SIZE_MB,
    SHM_PATH,
    SHM_SIZE_MB,
    TMP_PATH,
    TMP_SIZE_MB,
    VAR_TMP_PATH,
    VAR_TMP_SIZE_MB,
)
from appmux.sandbox.run_dir import RUN_DIR_MOUNT_PATH, RUN_DIR_SIZE_MB
from appmux.sandbox.watchdog import RSS_HEADROOM_FACTOR

pytestmark = pytest.mark.integration

SLUG_SUFFIX_BYTES: Final = 3
OK: Final = 200
LIMIT_MB: Final = int(BUDGET_MB * RSS_HEADROOM_FACTOR + OVERHEAD_PER_CHILD_MB)
# The mounts and their sizes: the whole of them is what a child can keep in memory this way.
MOUNT_SIZES_MB: Final = {
    TMP_PATH: TMP_SIZE_MB,
    VAR_TMP_PATH: VAR_TMP_SIZE_MB,
    SHM_PATH: SHM_SIZE_MB,
    HOME_PATH: HOME_SIZE_MB,
    RUN_DIR_MOUNT_PATH: RUN_DIR_SIZE_MB,
}
# About half of the mounts (144 MB): well under the limit even beside a child's resident memory.
FIRST_MOUNTS: Final = (TMP_PATH, HOME_PATH, VAR_TMP_PATH)
HOLD_MB: Final = 100
# The watchdog reads once a second; this is several rounds, for the "left alone" check.
QUIET_PERIOD_S: Final = 6.0
# The stop is seconds away once the heap is held; the rest is slack for a slow container.
STOP_WAIT_S: Final = 60.0
# What the reconciler logs when the watchdog stopped the child (``Offence.reason``).
LIMIT_TEXT: Final = "over its limit of"
TMPFS_TEXT: Final = "in its temporary directories"


@pytest.fixture(scope="module")
def target() -> Target:
    return load_target()


@pytest.fixture(scope="module")
def session(target: Target) -> Iterator[AdminSession]:
    with httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as client:
        opened = AdminSession(client, target)
        try:
            yield opened
        finally:
            opened.delete_created_apps()


def _start(session: AdminSession, fixture: str, role: str) -> str:
    slug = f"it-tm-{role}-{secrets.token_hex(SLUG_SUFFIX_BYTES)}"
    session.register(slug, fixture=fixture, name=f"Tmpfs memory {role}")
    session.wait_for_release(slug, session.press_update(slug), "active")
    session.wait_for_state(slug, "running")
    return slug


def _ask(session: AdminSession, slug: str, path: str) -> dict[str, Any]:
    answer = session.open_app(slug, path)
    assert answer.status_code == OK, f"{slug}{path} answered {answer.status_code}"
    body: dict[str, Any] = answer.json()
    return body


def _fill(session: AdminSession, slug: str, directory: str) -> None:
    report = _ask(session, slug, f"/fill?directory={directory}")
    expected = MOUNT_SIZES_MB[directory]
    # A full mount answers ENOSPC at its size; the fill file is about the whole mount.
    assert report["refused_with"] == "ENOSPC", report
    assert expected - 1 <= report["written_mb"] <= expected, (directory, report)


def _poll(condition: Callable[[], bool], *, limit_s: float) -> bool:
    deadline = time.monotonic() + limit_s
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL_S)


def test_a_child_whose_files_and_memory_together_pass_its_limit_is_stopped(
    session: AdminSession,
) -> None:
    neighbour = _start(session, "light_fastapi", "neighbour")
    filler = _start(session, "disk_filler", "filler")
    neighbour_statuses: list[int] = []

    # A child using half of its mounts is not stopped: temporary files are allowed.
    for directory in FIRST_MOUNTS:
        _fill(session, filler, directory)
    deadline = time.monotonic() + QUIET_PERIOD_S
    while time.monotonic() < deadline:
        neighbour_statuses.append(session.open_app(neighbour).status_code)
        assert session.open_app(filler, "/usage").status_code == OK, (
            "the child was stopped while holding only part of its mounts"
        )
        time.sleep(POLL_INTERVAL_S)

    for directory in (SHM_PATH, RUN_DIR_MOUNT_PATH):
        _fill(session, filler, directory)
    usage = _ask(session, filler, "/usage")
    files_mb = usage["tmpfs_used_total_mb"]
    # The premises of the test; if one fails the fixture, the budget or the sizes changed, not
    # the watchdog. Neither part is over the limit, the sum is (the sandbox's own processes, which
    # the watchdog also counts, only add to it).
    assert files_mb < LIMIT_MB, usage
    assert usage["rss_mb"] + HOLD_MB < LIMIT_MB, usage
    assert usage["rss_mb"] + HOLD_MB + files_mb > LIMIT_MB, usage

    held = session.open_app(filler, f"/hold?mb={HOLD_MB}")
    reasons: list[str] = []

    def stopped_for_memory() -> bool:
        neighbour_statuses.append(session.open_app(neighbour).status_code)
        lines = session.read(f"/apps/{filler}/logs")["lines"]
        reasons[:] = [
            line["text"]
            for line in lines
            if LIMIT_TEXT in line["text"] and TMPFS_TEXT in line["text"]
        ]
        return bool(reasons)

    done = _poll(stopped_for_memory, limit_s=STOP_WAIT_S)

    print(f"T219 watchdog: limit {LIMIT_MB} MB, usage before the heap {usage}, saw {reasons[:1]}")
    assert done, (
        f"after {STOP_WAIT_S:g} s the logs of {filler} have no line with {LIMIT_TEXT!r} and "
        f"{TMPFS_TEXT!r} (the /hold answered {held.status_code}): the watchdog did not count the "
        "child's tmpfs files, or the container does not let appmux read /proc/<pid>/root"
    )
    assert set(neighbour_statuses) == {OK}, (
        f"the neighbour did not answer 200 throughout: {sorted(set(neighbour_statuses))}"
    )
