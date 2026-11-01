"""Delete an app from the admin API (``DELETE /apps/{app}``, ``app.delete``).

``contracts/admin-api.md``: "stops and unregisters; configuration, releases, resources, secrets and
requests go with it". The route runs behind the real guard and authorizer on the in-memory
registry; the reconciler is replaced by ``FakeControl``, which records what the route asks of it,
so what is pinned here is the route's side of the ``AppControl`` seam and its order:

1. identity, same-origin, ``app.delete`` (a platform admin only), the admin's own token, and a
   registry that is up, all before anything runs;
2. ``stop_app``: the child stops and the route closes while the app is still registered, so a child
   never outlives its registration;
3. one registry write: the ``apps`` delete (its rows follow by cascade) and the ``app.delete``
   audit row, under the admin's own role.

A write that fails after the stop leaves the app stopped and every row where it was; nothing
tries to start it again from here. Starting data: see ``edge_harness`` (``sales`` owned by alice,
bob ``CAN_USE``, carol ``CAN_MANAGE``; ``docs`` owned by frank; dana is the platform admin).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Final

import pytest
from edge_harness import (
    ALICE,
    BOB,
    CAROL,
    DANA,
    FRANK,
    NOW,
    VISITOR_TOKEN,
    FakeGroups,
    Reply,
    call,
    seed_standard_rows,
)
from pydantic import SecretStr
from stub_registry import StubRegistry

from appmux.admin.api import create_admin_app
from appmux.admin.apps import AppRoutes
from appmux.admin.authorizer import make_authorizer
from appmux.admin.operations import OperationRoutes
from appmux.reconciler.app_control import RunState
from appmux.registry.audit import AuditEntry
from appmux.registry.errors import RegistryUnavailable
from appmux.registry.models import RowModel
from appmux.registry.protocol import Change
from appmux.registry.writers import Identity, WriteKind

pytestmark = pytest.mark.anyio

ERIN: Final = "erin@example.com"
DIGEST: Final = "sha256:" + "ab" * 32


class OrderedRegistry(StubRegistry):
    """The stub registry that notes in ``events`` when a write reaches it (even one that fails)."""

    def __init__(
        self, events: list[str], *, platform_writer_tokens: Collection[str] | None = None
    ) -> None:
        super().__init__(clock=lambda: NOW, platform_writer_tokens=platform_writer_tokens)
        self._events = events
        self.write_error: Exception | None = None

    async def write(
        self,
        changes: Sequence[Change],
        audit: Sequence[AuditEntry] = (),
        *,
        admin_token: SecretStr | None = None,
    ) -> tuple[RowModel | None, ...]:
        self._events.append("write " + ",".join(change.kind.value for change in changes))
        if self.write_error is not None:
            raise self.write_error
        return await super().write(changes, audit, admin_token=admin_token)


class FakeControl:
    """The reconciler as the routes see it: it records the order of what it is asked."""

    def __init__(self, registry: StubRegistry, events: list[str]) -> None:
        self._registry = registry
        self._events = events
        self.stopped: list[str] = []
        self.was_registered_when_stopped: list[bool] = []
        self.woken: list[str] = []
        self.stop_error: Exception | None = None

    async def stop_app(self, slug: str) -> None:
        self._events.append(f"stop {slug}")
        self.stopped.append(slug)
        self.was_registered_when_stopped.append(self._registry.snapshot.app(slug) is not None)
        if self.stop_error is not None:
            raise self.stop_error

    def wake(self, slug: str) -> None:
        self.woken.append(slug)

    def run_state(self, slug: str, desired_state: str) -> RunState:
        return "stopped" if desired_state == "stopped" else "running"

    def request_restart(self, slug: str) -> None:
        raise AssertionError("deleting an app restarted something")

    def app_renamed(self, old_slug: str, new_slug: str) -> None:
        raise AssertionError("deleting an app renamed something")


class Harness:
    def __init__(self, *, platform_writer_tokens: Collection[str] | None = None) -> None:
        self.events: list[str] = []
        self.registry = OrderedRegistry(self.events, platform_writer_tokens=platform_writer_tokens)
        seed_standard_rows(self.registry)
        seed_sales_content(self.registry)
        self.control = FakeControl(self.registry, self.events)
        self.app = create_admin_app(
            registry=self.registry,
            authorizer=make_authorizer(self.registry, FakeGroups()),
            route_providers=[AppRoutes(), OperationRoutes(self.control)],
        )

    async def delete(
        self, email: str | None, slug: str = "sales", *, same_origin: bool | None = None
    ) -> Reply:
        return await call(self.app, "DELETE", f"/apps/{slug}", email=email, same_origin=same_origin)

    def is_untouched(self) -> bool:
        """Nothing was stopped, woken, written or recorded."""
        return (
            self.control.stopped == []
            and self.control.woken == []
            and self.registry.writes == ()
            and self.registry.audit_rows == ()
        )

    def rows_of(self, slug: str) -> dict[str, int]:
        snapshot = self.registry.snapshot
        return {
            "apps": int(snapshot.app(slug) is not None),
            "app_config": int(snapshot.app_config(slug) is not None),
            "app_grants": len(snapshot.grants_for(slug)),
            "releases": len(snapshot.releases_for(slug)),
            "app_resources": len(snapshot.resources_for(slug)),
            "permission_requests": sum(
                1 for row in snapshot.permission_requests if row.app_slug == slug
            ),
        }


def seed_sales_content(registry: StubRegistry) -> None:
    """A release, a secret resource and a pending request on ``sales``, beside the standard rows."""
    registry.seed(
        "releases",
        {
            "app_slug": "sales",
            "release_no": 1,
            "snapshot_digest": DIGEST,
            "detected": {"runtime": "python", "command": ["python", "app.py"]},
            "status": "active",
            "status_at": NOW,
            "created_by": CAROL,
            "created_at": NOW,
        },
    )
    registry.seed(
        "app_resources",
        {
            "app_slug": "sales",
            "key": "api-token",
            "type": "secret",
            "target": "scope/api-token",
            "permission": "READ",
            "access_as": None,
            "check_result": "ok",
            "attached_by": ALICE,
            "attached_at": NOW,
        },
    )
    registry.seed(
        "permission_requests",
        {
            "request_id": 1,
            "app_slug": "sales",
            "kind": "settings",
            "payload": {"display_name": "Sales app"},
            "reason": "a clearer name",
            "requested_by": ALICE,
            "requested_at": NOW,
            "status": "pending",
        },
    )


def error_of(reply: Reply) -> tuple[str, str]:
    error = reply.json()["error"]
    return error["code"], error["message"]


# --- the delete ----------------------------------------------------------------------------------


async def test_a_platform_admin_deletes_an_app_and_the_answer_is_204_with_no_body() -> None:
    harness = Harness()

    reply = await harness.delete(DANA)

    assert (reply.status, reply.body) == (204, b"")
    assert harness.registry.snapshot.app("sales") is None


async def test_the_rows_of_the_app_go_with_it_and_the_other_apps_stay() -> None:
    harness = Harness()
    before = harness.rows_of("sales")
    assert all(count >= 1 for count in before.values()), before
    docs_before = harness.rows_of("docs")

    await harness.delete(DANA)

    assert set(harness.rows_of("sales").values()) == {0}
    assert harness.rows_of("docs") == docs_before


async def test_the_app_page_and_a_second_delete_say_there_is_no_such_app() -> None:
    harness = Harness()
    await harness.delete(DANA)

    page = await call(harness.app, "GET", "/apps/sales", email=DANA)
    again = await harness.delete(DANA)

    assert page.status == 404
    assert (again.status, error_of(again)[0]) == (404, "not_found")


async def test_a_repeated_delete_stops_and_records_nothing_more() -> None:
    harness = Harness()
    await harness.delete(DANA)
    events = list(harness.events)

    await harness.delete(DANA)

    assert harness.events == events
    assert harness.control.stopped == ["sales"]
    assert len(harness.registry.audit_rows) == 1


async def test_the_audit_row_is_written_once_for_the_deleting_admin() -> None:
    harness = Harness()

    await harness.delete(DANA)

    (row,) = harness.registry.audit_rows
    assert (row.actor, row.action, row.app_slug) == (DANA, "app.delete", "sales")
    assert row.detail == {}


async def test_the_delete_and_its_audit_row_are_one_write_under_the_admins_own_role() -> None:
    harness = Harness()

    await harness.delete(DANA)

    (write,) = harness.registry.writes
    assert write.kinds == (WriteKind.APP_DELETE,)
    assert write.identity is Identity.PLATFORM_ADMIN
    assert write.admin_token is not None
    assert write.admin_token.get_secret_value() == VISITOR_TOKEN


# --- the order: stop first, while the app is still registered --------------------------------


async def test_the_child_is_stopped_before_the_rows_go() -> None:
    harness = Harness()

    await harness.delete(DANA)

    assert harness.events == ["stop sales", "write apps.delete"]


async def test_the_app_is_still_registered_when_the_reconciler_is_asked_to_stop_it() -> None:
    harness = Harness()

    await harness.delete(DANA)

    assert harness.control.was_registered_when_stopped == [True]


async def test_the_reconciler_is_woken_after_the_delete_so_nothing_started_in_between_stays() -> (
    None
):
    harness = Harness()

    await harness.delete(DANA)

    assert harness.control.woken == ["sales"]


# --- who may, and what happens first -----------------------------------------------------------


@pytest.mark.parametrize("email", [ALICE, CAROL, BOB])
async def test_only_a_platform_admin_deletes_not_even_the_owner(email: str) -> None:
    harness = Harness()

    reply = await harness.delete(email)

    assert reply.status == 403
    assert harness.is_untouched()
    assert harness.registry.snapshot.app("sales") is not None


async def test_an_app_that_does_not_exist_is_not_found_and_nothing_is_stopped() -> None:
    harness = Harness()

    reply = await harness.delete(DANA, "nothing")

    assert (reply.status, error_of(reply)[0]) == (404, "not_found")
    assert harness.is_untouched()


async def test_a_stranger_cannot_tell_the_app_exists() -> None:
    harness = Harness()

    stranger = await harness.delete(ERIN)
    missing = await harness.delete(ERIN, "nothing")

    assert stranger.status == 404
    assert (stranger.status, stranger.body) == (missing.status, missing.body)
    assert harness.is_untouched()


async def test_nobody_without_an_identity_deletes() -> None:
    harness = Harness()

    reply = await harness.delete(None)

    assert reply.status == 401
    assert harness.is_untouched()


async def test_a_delete_from_another_origin_is_refused_before_anything_is_stopped() -> None:
    harness = Harness()

    reply = await harness.delete(DANA, same_origin=False)

    assert (reply.status, error_of(reply)[0]) == (403, "cross_origin")
    assert harness.is_untouched()


async def test_an_admin_whose_token_did_not_arrive_is_told_why_and_nothing_is_stopped() -> None:
    harness = Harness()

    reply = await call(
        harness.app,
        "DELETE",
        "/apps/sales",
        headers=[("X-Forwarded-Email", DANA)],
        same_origin=True,
    )

    assert reply.status == 403
    code, message = error_of(reply)
    assert code == "user_token_missing"
    assert "access token" in message
    assert harness.is_untouched()


async def test_while_the_registry_is_down_nothing_is_stopped_and_the_answer_is_503() -> None:
    harness = Harness()
    harness.registry.go_down()

    reply = await harness.delete(DANA)

    assert reply.status == 503
    assert harness.is_untouched()


# --- a write that fails after the stop ---------------------------------------------------------


async def test_a_registry_that_fails_during_the_write_leaves_every_row_and_answers_503() -> None:
    harness = Harness()
    before = harness.rows_of("sales")
    harness.registry.write_error = RegistryUnavailable()

    reply = await harness.delete(DANA)

    assert reply.status == 503
    assert harness.events == ["stop sales", "write apps.delete"]
    assert harness.rows_of("sales") == before
    assert harness.registry.audit_rows == ()
    assert harness.control.woken == []


async def test_an_admin_without_a_platform_database_role_gets_403_and_every_row_stays() -> None:
    harness = Harness(platform_writer_tokens=["a-token-nobody-sends"])
    before = harness.rows_of("sales")

    reply = await harness.delete(DANA)

    assert reply.status == 403
    assert harness.rows_of("sales") == before
    assert harness.registry.audit_rows == ()


async def test_a_stop_that_fails_deletes_nothing() -> None:
    harness = Harness()
    before = harness.rows_of("sales")
    harness.control.stop_error = RuntimeError("could not stop")

    reply = await harness.delete(DANA)

    assert (reply.status, error_of(reply)[0]) == (500, "internal_error")
    assert harness.events == ["stop sales"]
    assert harness.rows_of("sales") == before
    assert harness.registry.audit_rows == ()


async def test_the_owner_of_another_app_is_not_a_platform_admin_either() -> None:
    harness = Harness()

    reply = await harness.delete(FRANK, "docs")

    assert reply.status == 403
    assert harness.is_untouched()
