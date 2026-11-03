"""``GET /apps/{app}/logs``: the app's output ring, read by an app admin (T189).

The route reads the ring the reconciler fills (``reconciler/output.py``); here the ring is filled
by hand. It needs ``app.view_logs`` (``CAN_MANAGE``); polling writes no audit row except the first
read of an app by an actor in 10 minutes, which writes one ``app.view_logs`` row with no content.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

import pytest
from edge_harness import BOB, CAROL, DANA, FRANK, NOW, AdminApi, seeded_registry
from stub_registry import StubRegistry

from appmux.admin.logs import LogRoutes
from appmux.admin.schemas import AppLogs
from appmux.reconciler.app_control import RunState
from appmux.reconciler.output import OutputRing

LOGS: Final = "/apps/sales/logs"


class FakeApps:
    """What the reconciler answers about the apps: their state and their rings."""

    def __init__(self) -> None:
        self.rings: dict[str, OutputRing] = {}
        self.state: RunState = "running"

    def run_state(self, slug: str, desired_state: str) -> RunState:
        return self.state

    def output(self, slug: str) -> OutputRing | None:
        return self.rings.get(slug)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Logs:
    def __init__(self) -> None:
        self.registry: StubRegistry = seeded_registry()
        self.apps = FakeApps()
        self.clock = Clock()
        self.api = AdminApi(
            self.registry, lambda authorizer: [LogRoutes(self.apps, now=self.clock)]
        )

    def ring(self, *lines: str) -> OutputRing:
        ring = OutputRing(now=self.clock)
        writer = ring.begin()
        writer.note("started")
        for line in lines:
            writer.add("stdout", line)
        self.apps.rings["sales"] = ring
        return ring

    def get(self, path: str = LOGS, email: str = CAROL) -> AppLogs:
        reply = self.api.call("GET", path, email)
        assert reply.status_code == 200, reply.text
        return AppLogs.model_validate_json(reply.content)

    @property
    def view_rows(self) -> list[tuple[str, str | None, dict[str, object]]]:
        return [
            (row.actor, row.app_slug, dict(row.detail))
            for row in self.registry.audit_rows
            if row.action == "app.view_logs"
        ]


@pytest.fixture
def logs() -> Logs:
    return Logs()


def test_an_app_admin_reads_the_lines_oldest_first_with_the_cursor_and_the_state(
    logs: Logs,
) -> None:
    logs.ring("listening on 8000", "GET / 200")
    logs.apps.state = "running"

    page = logs.get()

    assert page.state == "running"
    assert page.instance is not None
    assert [(line.seq, line.stream, line.text) for line in page.lines] == [
        (1, "appmux", "started"),
        (2, "stdout", "listening on 8000"),
        (3, "stdout", "GET / 200"),
    ]
    assert page.next_after == 3
    assert page.dropped == 0
    assert page.last_exit is None


def test_after_returns_only_the_newer_lines_and_limit_keeps_the_newest(logs: Logs) -> None:
    logs.ring("one", "two", "three", "four")

    newer = logs.get(f"{LOGS}?after=3")
    newest = logs.get(f"{LOGS}?limit=2")

    assert [line.text for line in newer.lines] == ["three", "four"]
    assert newer.next_after == 5
    assert [line.text for line in newest.lines] == ["three", "four"]
    assert newest.dropped == 3


def test_the_last_exit_of_the_app_is_shown(logs: Logs) -> None:
    ring = logs.ring("bye")
    logs.clock.now = NOW + timedelta(seconds=2)
    writer = ring.begin()
    writer.note_exit(code=None, signal="SIGKILL", ran_for_s=2.0)
    logs.apps.state = "crashed"

    page = logs.get()

    assert page.state == "crashed"
    assert page.last_exit is not None
    assert (page.last_exit.code, page.last_exit.signal, page.last_exit.ran_for_s) == (
        None,
        "SIGKILL",
        2.0,
    )
    assert page.last_exit.at == NOW + timedelta(seconds=2)


def test_an_app_never_started_in_this_process_has_an_empty_page(logs: Logs) -> None:
    page = logs.get()

    assert page.state == "never_started"
    assert (page.instance, page.lines, page.next_after, page.dropped, page.last_exit) == (
        None,
        [],
        0,
        0,
        None,
    )


def test_a_credential_in_the_output_is_masked(logs: Logs) -> None:
    logs.ring("connecting with Authorization: Bearer abcdefghijklmnop1234")

    page = logs.get()

    assert "abcdefghijklmnop1234" not in page.model_dump_json()


def test_a_platform_admin_reads_the_logs(logs: Logs) -> None:
    logs.ring("hello")

    assert [line.text for line in logs.get(email=DANA).lines][-1] == "hello"


@pytest.mark.parametrize(
    ("email", "status"),
    [(BOB, 403), (FRANK, 404)],
    ids=["can-use-is-forbidden", "a-stranger-sees-no-such-app"],
)
def test_who_may_not_read_the_logs(logs: Logs, email: str, status: int) -> None:
    logs.ring("secret plans")

    reply = logs.api.call("GET", LOGS, email)

    assert reply.status_code == status
    assert "secret plans" not in reply.text
    assert logs.view_rows == []


def test_a_missing_app_is_404(logs: Logs) -> None:
    assert logs.api.call("GET", "/apps/nope/logs", CAROL).status_code == 404


@pytest.mark.parametrize(
    "query", ["after=-1", "after=x", "limit=0", "limit=1001", "limit=", "after=1.5"]
)
def test_a_bad_cursor_or_limit_is_400(logs: Logs, query: str) -> None:
    assert logs.api.call("GET", f"{LOGS}?{query}", CAROL).status_code == 400


def test_the_first_read_in_ten_minutes_writes_one_audit_row_with_no_content(logs: Logs) -> None:
    logs.ring("hello")

    logs.get()
    logs.clock.now = NOW + timedelta(minutes=9)
    logs.get()

    assert logs.view_rows == [(CAROL, "sales", {})]


def test_a_read_after_ten_minutes_and_a_read_by_another_admin_each_write_a_row(
    logs: Logs,
) -> None:
    logs.ring("hello")

    logs.get()
    logs.get(email=DANA)
    logs.clock.now = NOW + timedelta(minutes=10)
    logs.get()

    assert logs.view_rows == [(CAROL, "sales", {}), (DANA, "sales", {}), (CAROL, "sales", {})]


def test_a_read_that_cannot_be_audited_fails_and_is_audited_on_the_next_try(logs: Logs) -> None:
    logs.ring("hello")
    logs.registry.go_down()

    refused = logs.api.call("GET", LOGS, CAROL)
    logs.registry.come_back()
    logs.get()

    assert refused.status_code == 503
    assert "hello" not in refused.text
    assert logs.view_rows == [(CAROL, "sales", {})]
