"""Every writable mount of a child refuses files at its limit (T219, TD-004, FR-011).

A ``tmpfs`` capped only in bytes takes empty files until the kernel's default limit (half the RAM
pages, millions) is reached, and each file is kernel memory charged to the container and shown in no
figure of the child's. The mounts of a sandbox therefore carry ``nr_inodes``
(``rootfs.tmpfs_mount_data``): one file for each 4 KiB page that the mount's size could hold.

``disk_filler``'s ``/files`` makes empty files in one of the five writable mounts until the
filesystem refuses. For each mount the test pins three things:

* the kernel took the option: ``statvfs`` reports the limit the plan asked for, not its own default;
* the refusal comes from that limit (``ENOSPC``), well before the fixture's own ceiling;
* a full mount is a failed write, not a dead child.

It also prints what the files cost (``MemFree`` and ``Slab`` of ``/proc/meminfo`` around the loop),
which is the evidence for the "about 1 KB a file" in TD-004; those figures are the node's and move
with other work, so they are printed, not asserted.

It does not run in the default gate: every test here is marked ``integration`` and skipped unless
``APPMUX_TEST_CONTAINER=1``. Run it with ``scripts/run-remote-tests.sh``; the setup is that of
``ops_session.py``. Everything the run registers is deleted afterwards.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
from ops_session import REQUEST_TIMEOUT_S, AdminSession, Target, load_target

from appmux.sandbox.rootfs import (
    HOME_PATH,
    HOME_SIZE_MB,
    SHM_PATH,
    SHM_SIZE_MB,
    TMP_PATH,
    TMP_SIZE_MB,
    TMPFS_FILES_PER_MB,
    VAR_TMP_PATH,
    VAR_TMP_SIZE_MB,
)
from appmux.sandbox.run_dir import RUN_DIR_MOUNT_PATH, RUN_DIR_SIZE_MB

pytestmark = pytest.mark.integration

SLUG_SUFFIX_BYTES: Final = 3
# The limits the plan asks for, by mount: one file for each page of the mount's size.
EXPECTED_FILE_LIMITS: Final = {
    RUN_DIR_MOUNT_PATH: RUN_DIR_SIZE_MB * TMPFS_FILES_PER_MB,
    TMP_PATH: TMP_SIZE_MB * TMPFS_FILES_PER_MB,
    VAR_TMP_PATH: VAR_TMP_SIZE_MB * TMPFS_FILES_PER_MB,
    SHM_PATH: SHM_SIZE_MB * TMPFS_FILES_PER_MB,
    HOME_PATH: HOME_SIZE_MB * TMPFS_FILES_PER_MB,
}
# A fresh mount already holds a few inodes (its root, and a socket or a token file in the run
# directory), so a loop that fills it makes a little fewer files than the limit.
INODES_ALREADY_IN_USE_MAX: Final = 64


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


def _fill_with_files(session: AdminSession, slug: str, directory: str) -> dict[str, Any]:
    answer = session.open_app(slug, f"/files?directory={directory}")
    assert answer.status_code == 200, f"disk_filler/files answered {answer.status_code}"
    report: dict[str, Any] = answer.json()
    assert report["fixture"] == "disk_filler"
    return report


def test_every_writable_mount_refuses_files_at_its_limit(session: AdminSession) -> None:
    slug = f"zd-ino-{secrets.token_hex(SLUG_SUFFIX_BYTES)}"
    session.register(slug, fixture="disk_filler")
    session.wait_for_release(slug, session.press_update(slug), "active")
    session.wait_for_state(slug, "running")

    reports: dict[str, dict[str, Any]] = {}
    for directory in EXPECTED_FILE_LIMITS:
        report = _fill_with_files(session, slug, directory)
        reports[directory] = report
        freed_kb = report["mem_free_kb_before"] - report["mem_free_kb_after"]
        slab_kb = report["slab_kb_after"] - report["slab_kb_before"]
        print(
            f"{directory}: {report['created']} files made in {report['elapsed_s']} s, "
            f"refused with {report['refused_with']}, limit {report['mount_file_limit']}; "
            f"MemFree fell {freed_kb} kB, Slab rose {slab_kb} kB"
        )
        assert session.open_app(slug, f"/files/clear?directory={directory}").status_code == 200

    problems: list[str] = []
    for directory, expected in EXPECTED_FILE_LIMITS.items():
        report = reports[directory]
        if report["mount_file_limit"] != expected:
            problems.append(
                f"{directory}: the kernel reports {report['mount_file_limit']} files, the plan "
                f"asks for {expected}"
            )
        if report["refused_with"] != "ENOSPC" or report["stopped_by_ceiling"]:
            problems.append(
                f"{directory}: made {report['created']} files and was refused with "
                f"{report['refused_with']} (ceiling reached: {report['stopped_by_ceiling']})"
            )
        elif not expected - INODES_ALREADY_IN_USE_MAX <= report["created"] <= expected:
            problems.append(
                f"{directory}: made {report['created']} files, expected between "
                f"{expected - INODES_ALREADY_IN_USE_MAX} and {expected}"
            )
    assert not problems, "\n".join(problems)

    # A full mount is a failed write, not a dead child.
    assert session.open_app(slug, "/").status_code == 200
