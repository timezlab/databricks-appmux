"""The failure table of design section 11, on a real container: the rows that need one (T180).

Design 11 lists 27 failures, each with its effect on other apps, what is shown and the recovery.
Every row has at least one test somewhere; this module holds the two that only a container can
run, and the list below says where the other 25 are, so a row cannot go missing unseen. Paths are
under ``tests/``; ``unit`` and ``contract`` run in the default gate, ``integration`` needs a
Databricks container (``scripts/run-container-tests.sh``, or ``scripts/run-remote-tests.sh``).

Rows tested in this module:

* 19, a child exceeds its memory: ``test_a_child_that_allocates_past_its_budget_is_stopped_...``
* 20, a child's pump or slirp dies: ``test_a_child_whose_slirp_dies_is_treated_as_a_crash_...``

Rows tested elsewhere (T180 added the ones marked *):

* 1  ``unit/test_update_flow.py::test_an_invalid_app_yaml_refuses_the_update``
* 2  ``unit/test_reconciler.py::test_missing_resources_fail_the_release_before_anything_is_built``
     and ``integration/test_resources_container.py``
* 3  *``unit/test_reconciler.py::test_a_new_release_whose_build_fails_leaves_the_old_one_and_its_
     route_alone`` (the build failing itself: ``test_a_build_that_fails_stores_its_reason_and_log_
     and_starts_nothing``)
* 4  ``unit/test_reconciler.py::test_a_child_that_never_answers_fails_the_release_with_the_reason_
     and_a_log_tail`` and ``integration/test_host_a_child.py``
* 5  ``unit/test_config_release.py::test_a_new_copy_that_is_not_healthy_leaves_the_first_one_
     serving``. Not covered: the app page showing the failed check; nothing in the admin API
     reads ``Reconciler.last_restart_failure`` (``docs/tech-debt.md`` TD-028)
* 6  ``unit/test_attach_check.py`` and ``contract/test_resources.py``
* 7  ``contract/test_requests_api.py::test_the_rules_of_a_new_request_answer_with_their_codes``
* 8  *``contract/test_requests_api.py::test_a_request_of_someone_who_lost_can_manage_is_not_
     approved_and_nothing_is_written``
* 9  ``contract/test_requests_api.py::test_a_slug_taken_at_approval_closes_the_request_as_denied_
     under_the_approving_admin``
* 10 ``unit/test_update_retention.py::test_a_full_cap_with_every_release_protected_refuses_the_
     update_and_writes_nothing``
* 11 ``contract/test_release_management.py::test_a_fourth_lock_is_409_and_the_release_stays_
     unlocked``
* 12 ``unit/test_update_packages.py::test_every_unresolved_requirement_is_listed_in_one_refusal``
* 13 *``unit/test_reconciler.py::test_a_secret_appmux_cannot_read_fails_the_release_before_
     anything_starts`` (the message: ``unit/test_secret_resolution.py``; the assembly:
     ``contract/test_service_assembly.py::test_an_unreadable_secret_refuses_the_environment_
     without_a_value``)
* 14 ``unit/test_reconciler.py::test_the_memory_that_does_not_fit_fails_the_release_with_the_
     numbers`` (the release fails after the Update; the design says the Update is refused)
* 15 ``unit/test_snapshot_digest.py`` and ``integration/test_host_a_child.py::test_a_folder_the_
     person_cannot_read_is_refused_and_creates_no_release``
* 16 *``unit/test_reconciler_build.py::test_a_snapshot_whose_bytes_do_not_match_its_digest_fails_
     the_build`` (the message is the store's, not ``snapshot sha256 mismatch``)
* 17 ``unit/test_groups_lookup.py::test_a_failed_refresh_falls_back_to_the_cached_groups``
* 18 ``integration/test_crash_and_rollback.py::test_a_crash_loop_ends_crashed_with_only_its_own_
     route_at_503`` and ``unit/test_crash_backoff.py``
* 21 *``unit/test_credentials.py::test_an_own_service_principal_is_unaffected_by_a_shared_token_
     that_cannot_be_minted``, ``test_a_round_that_cannot_mint_is_logged_without_the_token_and_the_
     loop_goes_on``. The code refuses a start that cannot mint a token (``test_a_start_that_
     cannot_get_a_shared_token_is_refused_with_what_to_do``) instead of starting a child that
     gets 401s; a token that cannot be renewed later is the 401 case
* 22 ``contract/test_startup_gate.py``
* 23 ``contract/test_self_check_gate.py`` and ``integration/test_self_check_fail_closed.py``
* 24 *``contract/test_edge_pipeline.py::test_requests_keep_flowing_from_memory_while_the_registry_
     is_down``; at startup: ``contract/test_startup_gate.py::test_a_registry_that_fails_at_start_
     blocks_the_gate_and_never_opens_it``; writes: ``contract/test_admin_conventions.py``
* 25 ``contract/test_register_update.py::test_an_admin_without_a_platform_database_role_gets_403_
     and_nothing_changes``
* 26 ``contract/test_platform_admin_support.py::test_a_list_that_would_leave_no_user_entry_is_
     refused_and_changes_nothing``
* 27 ``contract/test_service_wiring.py::test_sigterm_stops_the_children_within_the_budget_before_
     the_registry_closes``

Row 19. ``RLIMIT_AS`` is set from the budget and ``memory_hog`` allocates until something stops it.
The test asserts that the allocation is stopped before the memory held passes the address-space
limit and that a neighbour answers throughout. The RSS watchdog of the design (``sandbox/watchdog.
watch``) is not started by the service, so this test cannot show it acting; the test accepts either
way of stopping (``MemoryError`` inside the child, or the child gone) and prints which one it saw.
That the watchdog is not wired is TD-029.

Row 20. Killing ``slirp4netns`` leaves the child running with no way out. The design treats that
as a crash (restart with backoff, then ``crashed``). The supervisor waits only for the sandbox
process, so nothing notices (TD-030). The test asserts what the design says; it fails until the
supervisor watches its helpers.

It needs the container's ``/proc`` and ``APPMUX_TEST_EDGE_PID``, which ``scripts/run-container-
tests.sh`` provides, and what ``ops_session.load_target`` reads. It is marked ``integration`` and
skipped unless ``APPMUX_TEST_CONTAINER=1``. Everything the run registers is deleted afterwards.
"""

from __future__ import annotations

import os
import secrets
import signal
import time
from collections.abc import Callable, Iterator
from typing import Any, Final

import httpx
import pytest
from ops_session import BUDGET_MB, POLL_INTERVAL_S, REQUEST_TIMEOUT_S, AdminSession, load_target
from proc_view import SLIRP_COMMAND, command_name_of, processes_named, supervisor_of

from appmux.sandbox.watchdog import ADDRESS_SPACE_HEADROOM_FACTOR, process_tree_pids

pytestmark = pytest.mark.integration

SLUG_SUFFIX_BYTES: Final = 3
OK: Final = 200
# memory_hog allocates 32 MB every 0.1 s, so it meets its limit within seconds; the rest is slack
# for the build and for a slow container.
HOG_WAIT_S: Final = 120.0
HELPER_DEATH_WAIT_S: Final = 90.0


@pytest.fixture(scope="module")
def session() -> Iterator[AdminSession]:
    target = load_target()
    with httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as client:
        admin = AdminSession(client, target)
        try:
            yield admin
        finally:
            admin.delete_created_apps()


@pytest.fixture(scope="module")
def edge_pid() -> int:
    raw = os.environ.get("APPMUX_TEST_EDGE_PID", "").strip()
    if not raw.isdecimal():
        pytest.fail("set APPMUX_TEST_EDGE_PID to the edge's process id (see the module docstring)")
    return int(raw)


def _start(session: AdminSession, fixture: str, role: str) -> str:
    """Register ``fixture`` under a slug of this run, press Update and wait until it runs."""
    slug = f"it-ft-{role}-{secrets.token_hex(SLUG_SUFFIX_BYTES)}"
    session.register(slug, fixture=fixture, name=f"Failure table {role}")
    release_no = session.press_update(slug)
    session.wait_for_release(slug, release_no, "active")
    session.wait_for_state(slug, "running")
    return slug


@pytest.fixture(scope="module")
def neighbour(session: AdminSession) -> str:
    """An app that has nothing to do with the failures below and must answer through all of them."""
    return _start(session, "light_fastapi", "neighbour")


def _poll(condition: Callable[[], bool], *, limit_s: float) -> bool:
    deadline = time.monotonic() + limit_s
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL_S)


def test_a_child_that_allocates_past_its_budget_is_stopped_and_the_neighbour_keeps_answering(
    session: AdminSession, neighbour: str
) -> None:
    hog = _start(session, "memory_hog", "hog")
    limit_mb = int(BUDGET_MB * ADDRESS_SPACE_HEADROOM_FACTOR)
    neighbour_statuses: list[int] = []
    seen: dict[str, Any] = {}

    def stopped() -> bool:
        neighbour_statuses.append(session.open_app(neighbour).status_code)
        answer = session.open_app(hog, "/report")
        if answer.status_code != OK:
            seen.update(how="the child stopped answering", status=answer.status_code)
            return True
        report = answer.json()
        seen.update(held_mb=report["held_mb"], stopped_by=report["stopped_by"])
        if report["stopped_by"] is not None:
            seen["how"] = f"the allocation failed ({report['stopped_by']})"
            return True
        return False

    done = _poll(stopped, limit_s=HOG_WAIT_S)

    print(f"failure table row 19: budget {BUDGET_MB} MB, limit {limit_mb} MB, saw {seen}")
    assert done, f"after {HOG_WAIT_S:g} s the child was still allocating: {seen}"
    assert seen.get("held_mb", 0) <= limit_mb, f"the child held more than its limit: {seen}"
    assert set(neighbour_statuses) == {OK}, (
        f"the neighbour did not answer 200 throughout: {sorted(set(neighbour_statuses))}"
    )


def test_a_child_whose_slirp_dies_is_treated_as_a_crash_and_the_neighbour_keeps_answering(
    session: AdminSession, neighbour: str, edge_pid: int
) -> None:
    slug = _start(session, "light_fastapi", "slirp")
    supervisor = supervisor_of(edge_pid, slug)
    assert supervisor is not None, f"no process of {slug} was found below the edge {edge_pid}"
    helpers = processes_named(supervisor, SLIRP_COMMAND)
    below = sorted({command_name_of(pid) or "?" for pid in process_tree_pids(supervisor)})
    assert len(helpers) == 1, f"expected one {SLIRP_COMMAND} below the supervisor, found {below}"

    os.kill(helpers[0], signal.SIGKILL)

    def treated_as_a_crash() -> bool:
        replaced = supervisor_of(edge_pid, slug) != supervisor
        return replaced or session.app(slug)["state"] != "running"

    noticed = _poll(treated_as_a_crash, limit_s=HELPER_DEATH_WAIT_S)

    state = session.app(slug)["state"]
    print(f"failure table row 20: noticed={noticed}, state={state}")
    assert noticed, (
        f"{HELPER_DEATH_WAIT_S:g} s after {SLIRP_COMMAND} was killed the app was still running "
        "in the same sandbox with no network helper; the design treats this as a crash"
    )
    assert session.open_app(neighbour).status_code == OK
