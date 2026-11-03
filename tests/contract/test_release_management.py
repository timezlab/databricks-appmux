"""Name, tag, lock, unlock and delete a release from the admin API (T113; US5; admin-api.md).

Four routes run behind the real guard and the real authorizer on the in-memory registry:

* ``PUT /apps/{app}/releases/{id}`` (``release.label``, ``release.tag``, ``release.untag``):
  replaces the name and the tags together; one audit row per kind of change; tags and a name
  protect nothing;
* ``PUT /apps/{app}/releases/{id}/lock`` (``release.lock``): refused ``409`` at
  ``max_locked_releases``;
* ``DELETE /apps/{app}/releases/{id}/lock`` (``release.unlock``);
* ``DELETE /apps/{app}/releases/{id}`` (``release.delete``): ``204``, and ``409`` for the active,
  the target, an in-progress and a locked release.

All of them need ``CAN_MANAGE`` on the app, answer with the release as ``ReleaseDetail`` (except the
delete), and are written by the appmux service principal, never with the caller's token.

The Update flow is not part of these routes, so the provider gets an updater that fails the test if
it is called. Starting data: see ``edge_harness`` (``sales`` owned by alice, bob ``CAN_USE``, carol
``CAN_MANAGE``; dana is the platform admin). Erin is a stranger to all.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

import pytest
from edge_harness import (
    ALICE,
    BOB,
    CAROL,
    DANA,
    NOW,
    VISITOR_TOKEN,
    FakeGroups,
    Reply,
    call,
    seed_standard_rows,
    seeded_registry,
)
from pydantic import SecretStr
from stub_registry import RacedRegistry, StubRegistry

from appmux.admin.api import create_admin_app
from appmux.admin.apps import AppRoutes
from appmux.admin.authorizer import make_authorizer
from appmux.admin.releases import ReleaseRoutes
from appmux.admin.schemas import ReleaseDetail
from appmux.identity.actor import Actor
from appmux.reconciler.ports import ChangeWriter, UpdateResult
from appmux.registry.models import Release
from appmux.registry.protocol import Change
from appmux.registry.releases import soft_delete_change
from appmux.registry.writers import Identity, WriteKind

pytestmark = pytest.mark.anyio

ERIN: Final = "erin@example.com"
DIGEST: Final = "sha256:" + "ab" * 32
LEAK_MARKERS: Final = (VISITOR_TOKEN, "Traceback", "SELECT")
FOURTH_LOCK: Final = "app 'sales' already has 3 locked releases; unlock one first"


class NoUpdater:
    """The Update flow, which none of these routes may start."""

    async def update(
        self, *, actor: Actor, app_slug: str, user_token: SecretStr, write: ChangeWriter
    ) -> UpdateResult:
        raise AssertionError("a release management route started an Update")


class Harness:
    def __init__(self, registry: StubRegistry | None = None) -> None:
        self.registry = registry if registry is not None else seeded_registry()
        self.woken: list[str] = []
        self.app = create_admin_app(
            registry=self.registry,
            authorizer=make_authorizer(self.registry, FakeGroups()),
            route_providers=[
                ReleaseRoutes(NoUpdater(), on_release_created=self.woken.append),
                AppRoutes(),  # the app page lists releases; a deleted one must not appear
            ],
        )

    async def request(
        self,
        method: str,
        path: str,
        email: str | None,
        json_body: object = None,
        *,
        same_origin: bool | None = None,
    ) -> Reply:
        return await call(
            self.app, method, path, email=email, json_body=json_body, same_origin=same_origin
        )

    async def put(self, release_no: object, body: object, email: str | None = CAROL) -> Reply:
        return await self.request("PUT", f"/apps/sales/releases/{release_no}", email, body)

    async def lock(self, release_no: object, email: str | None = CAROL) -> Reply:
        return await self.request("PUT", f"/apps/sales/releases/{release_no}/lock", email)

    async def unlock(self, release_no: object, email: str | None = CAROL) -> Reply:
        return await self.request("DELETE", f"/apps/sales/releases/{release_no}/lock", email)

    async def delete(self, release_no: object, email: str | None = CAROL) -> Reply:
        return await self.request("DELETE", f"/apps/sales/releases/{release_no}", email)

    def is_untouched(self) -> bool:
        return self.registry.writes == () and self.registry.audit_rows == ()

    def release(self, release_no: int) -> Release:
        row = self.registry.snapshot.release("sales", release_no)
        assert row is not None
        return row

    def locked_numbers(self) -> list[int]:
        return [
            release.release_no
            for release in self.registry.snapshot.releases_for("sales")
            if release.locked_at is not None
        ]

    def audit(self) -> list[tuple[str, str, dict[str, object]]]:
        return [(row.action, row.actor, row.detail) for row in self.registry.audit_rows]


def seed_release(
    registry: StubRegistry,
    release_no: int,
    status: str = "superseded",
    *,
    locked: bool = False,
    label: str | None = None,
    tags: Sequence[str] = (),
    deleted: bool = False,
) -> None:
    columns: dict[str, object] = {
        "app_slug": "sales",
        "release_no": release_no,
        "snapshot_digest": None if deleted else DIGEST,
        "detected": {"runtime": "python", "command": ["python", "app.py"]},
        "status": status,
        "status_at": NOW,
        "label": label,
        "tags": list(tags),
        "created_by": CAROL,
        "created_at": NOW,
    }
    if locked:
        columns.update(locked_by=ALICE, locked_at=NOW)
    if deleted:
        columns.update(deleted_by=CAROL, deleted_at=NOW)
    registry.seed("releases", columns)


def set_target(registry: StubRegistry, release_no: int | None) -> None:
    registry.seed(
        "app_config",
        {
            "app_slug": "sales",
            "desired_state": "running",
            "target_release_no": release_no,
            "updated_by": DANA,
            "updated_at": NOW,
        },
    )


def harness_with(
    statuses: dict[int, str], *, target: int | None = None, locked: Sequence[int] = ()
) -> Harness:
    harness = Harness()
    for release_no, status in statuses.items():
        seed_release(harness.registry, release_no, status, locked=release_no in locked)
    set_target(harness.registry, target)
    return harness


def error_of(reply: Reply) -> tuple[str, str]:
    error = reply.json()["error"]
    return error["code"], error["message"]


def detail_of(reply: Reply) -> ReleaseDetail:
    assert reply.status == 200, reply.text
    return ReleaseDetail.model_validate_json(reply.body)


def assert_no_leak(reply: Reply) -> None:
    for marker in LEAK_MARKERS:
        assert marker not in reply.text


# --- PUT /apps/{app}/releases/{id}: name and tags -----------------------------------------------


async def test_the_name_and_the_tags_are_replaced_and_the_release_comes_back() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    reply = await harness.put(1, {"name": "before pagination", "tags": ["legacy-ui", "stable"]})

    detail = detail_of(reply)
    assert (detail.release_no, detail.name, detail.tags) == (
        1,
        "before pagination",
        ["legacy-ui", "stable"],
    )
    assert (harness.release(1).label, harness.release(1).tags) == (
        "before pagination",
        ["legacy-ui", "stable"],
    )


async def test_a_new_name_and_new_tags_are_two_audit_rows_naming_the_release() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    await harness.put(1, {"name": "first version", "tags": ["stable"]})

    assert harness.audit() == [
        ("release.label", CAROL, {"release": 1}),
        ("release.tag", CAROL, {"release": 1}),
    ]
    assert {row.app_slug for row in harness.registry.audit_rows} == {"sales"}


async def test_removing_a_tag_is_an_untag_row_and_nothing_else() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)
    seed_release(harness.registry, 1, "superseded", label="keep", tags=["a", "b"])

    reply = await harness.put(1, {"name": "keep", "tags": ["a"]})

    assert detail_of(reply).tags == ["a"]
    assert harness.audit() == [("release.untag", CAROL, {"release": 1})]


async def test_adding_one_tag_and_removing_another_records_both() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)
    seed_release(harness.registry, 1, "superseded", label="keep", tags=["old"])

    await harness.put(1, {"name": "keep", "tags": ["new"]})

    assert [action for action, _, _ in harness.audit()] == ["release.tag", "release.untag"]
    assert harness.release(1).tags == ["new"]


async def test_a_null_name_clears_the_name() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)
    seed_release(harness.registry, 1, "superseded", label="old name")

    reply = await harness.put(1, {"name": None, "tags": []})

    assert detail_of(reply).name is None
    assert harness.release(1).label is None
    assert harness.audit() == [("release.label", CAROL, {"release": 1})]


async def test_a_request_that_changes_nothing_writes_and_records_nothing() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)
    seed_release(harness.registry, 1, "superseded", label="same", tags=["a"])

    reply = await harness.put(1, {"name": "same", "tags": ["a"]})

    assert detail_of(reply).name == "same"
    assert harness.is_untouched()


async def test_a_repeated_tag_is_stored_once_in_the_order_given() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    reply = await harness.put(1, {"name": None, "tags": ["b", "a", "b"]})

    assert detail_of(reply).tags == ["b", "a"]


async def test_tags_and_a_name_protect_nothing_so_the_release_is_still_deletable() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)
    await harness.put(1, {"name": "stable one", "tags": ["stable"]})

    reply = await harness.delete(1)

    assert reply.status == 204


async def test_naming_a_release_is_written_by_the_service_principal_not_the_callers_token() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    await harness.put(1, {"name": "named", "tags": []})

    (write,) = harness.registry.writes
    assert write.kinds == (WriteKind.RELEASE_UPDATE,)
    assert (write.identity, write.admin_token) == (Identity.APPMUX_SP, None)


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ({"name": "", "tags": []}, "name is not valid"),
        ({"name": "x" * 81, "tags": []}, "name is not valid"),
        ({"name": "   ", "tags": []}, "name is not valid"),
        ({"name": "ok", "tags": ["Stable"]}, "tags.0 is not valid"),
        ({"name": "ok", "tags": ["x" * 33]}, "tags.0 is not valid"),
        ({"name": "ok", "tags": [f"t{n}" for n in range(11)]}, "tags is not valid"),
        ({"name": "ok", "tags": ["fine", "  "]}, "tags.1 is not valid"),
        ({"name": "ok", "tags": [""]}, "tags.0 is not valid"),
        ({"name": "ok", "tags": "stable"}, "tags is not valid"),
        ({"name": "ok"}, "tags is required"),
        ({"tags": []}, "name is required"),
        ({"name": "ok", "tags": [], "locked": True}, "locked is not allowed"),
    ],
)
async def test_a_body_that_fails_its_schema_is_400_and_nothing_is_written(
    body: dict[str, object], problem: str
) -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    reply = await harness.put(1, body)

    assert reply.status == 400
    code, message = error_of(reply)
    assert (code, problem in message) == ("bad_request", True)
    assert harness.is_untouched()


async def test_a_refused_body_never_comes_back_to_the_client() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    reply = await harness.put(1, {"name": "ok", "tags": ["Marker-Value-From-The-Client"]})

    assert reply.status == 400
    assert "Marker-Value-From-The-Client" not in reply.text


async def test_a_put_without_a_json_body_is_400() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    reply = await harness.request("PUT", "/apps/sales/releases/1", CAROL)

    assert reply.status == 400
    assert harness.is_untouched()


# --- PUT /apps/{app}/releases/{id}/lock and DELETE .../lock -------------------------------------


async def test_a_lock_names_who_and_when_and_how_many_of_the_locks_are_taken() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    reply = await harness.lock(1)

    detail = detail_of(reply)
    assert detail.locked_by == CAROL
    assert detail.locked_at is not None
    assert harness.release(1).locked_by == CAROL
    assert harness.audit() == [("release.lock", CAROL, {"release": 1, "locks": "1 of 3"})]


async def test_the_active_release_may_be_locked_too_and_counts_as_a_lock() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    reply = await harness.lock(2)

    assert detail_of(reply).locked_by == CAROL
    assert harness.audit() == [("release.lock", CAROL, {"release": 2, "locks": "1 of 3"})]


async def test_a_fourth_lock_is_409_and_the_release_stays_unlocked() -> None:
    harness = harness_with(
        {1: "superseded", 2: "superseded", 3: "superseded", 4: "superseded", 5: "active"},
        target=5,
        locked=(1, 2, 3),
    )

    reply = await harness.lock(4)

    assert reply.status == 409
    assert error_of(reply) == ("lock_limit_reached", FOURTH_LOCK)
    assert harness.release(4).locked_by is None
    assert harness.is_untouched()


async def test_locking_a_locked_release_again_changes_nothing_and_records_nothing() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2, locked=(1,))

    reply = await harness.lock(1)

    assert detail_of(reply).locked_by == ALICE
    assert harness.is_untouched()


async def test_two_simultaneous_locks_at_the_cap_admit_one() -> None:
    harness = harness_with(
        {1: "superseded", 2: "superseded", 3: "superseded", 4: "superseded", 5: "active"},
        target=5,
        locked=(1, 2),
    )

    first, second = await asyncio.gather(harness.lock(3, CAROL), harness.lock(4, ALICE))

    assert sorted([first.status, second.status]) == [200, 409]
    assert len(harness.locked_numbers()) == 3
    assert [action for action, _, _ in harness.audit()] == ["release.lock"]
    assert harness.audit()[0][2]["locks"] == "3 of 3"


async def test_unlocking_clears_the_lock_and_records_how_many_remain() -> None:
    harness = harness_with({1: "superseded", 2: "superseded", 3: "active"}, target=3, locked=(1, 2))

    reply = await harness.unlock(1)

    detail = detail_of(reply)
    assert (detail.locked_by, detail.locked_at) == (None, None)
    assert harness.locked_numbers() == [2]
    assert harness.audit() == [("release.unlock", CAROL, {"release": 1, "locks": "1 of 3"})]


async def test_unlocking_a_release_that_is_not_locked_changes_nothing() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    reply = await harness.unlock(1)

    assert detail_of(reply).locked_by is None
    assert harness.is_untouched()


async def test_a_freed_place_can_be_taken_by_another_release() -> None:
    harness = harness_with(
        {1: "superseded", 2: "superseded", 3: "superseded", 4: "superseded", 5: "active"},
        target=5,
        locked=(1, 2, 3),
    )
    assert (await harness.lock(4)).status == 409

    await harness.unlock(1)
    reply = await harness.lock(4)

    assert reply.status == 200
    assert harness.locked_numbers() == [2, 3, 4]


# --- DELETE /apps/{app}/releases/{id} -----------------------------------------------------------


async def test_an_unprotected_release_is_deleted_and_reads_like_a_missing_one() -> None:
    harness = harness_with({1: "superseded", 2: "failed", 3: "active"}, target=3)

    reply = await harness.delete(2)

    assert (reply.status, reply.body) == (204, b"")
    row = harness.release(2)
    assert row.deleted_by == CAROL
    assert (row.snapshot_digest, row.wheels, row.tags) == (None, [], [])
    assert (await harness.request("GET", "/apps/sales/releases/2", CAROL)).status == 404
    assert harness.audit() == [("release.delete", CAROL, {"release": 2})]


async def test_a_deleted_release_is_not_listed_on_the_app_page() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)
    await harness.delete(1)

    page = await harness.request("GET", "/apps/sales", CAROL)

    assert [release["release_no"] for release in page.json()["releases"]] == [2]


async def test_deleting_does_not_wake_the_reconciler() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    await harness.delete(1)

    assert harness.woken == []


@pytest.mark.parametrize(
    ("statuses", "target", "locked", "release_no", "message"),
    [
        (
            {1: "active"},
            None,
            (),
            1,
            "release 1 is the active release; activate another release first",
        ),
        (
            {1: "active", 2: "failed"},
            2,
            (),
            2,
            "release 2 is the target of the app; activate another release first",
        ),
        (
            {1: "active", 2: "pending"},
            None,
            (),
            2,
            "release 2 is in progress (pending); wait for it to finish",
        ),
        (
            {1: "active", 2: "building"},
            None,
            (),
            2,
            "release 2 is in progress (building); wait for it to finish",
        ),
        (
            {1: "active", 2: "starting"},
            None,
            (),
            2,
            "release 2 is in progress (starting); wait for it to finish",
        ),
        (
            {1: "active", 2: "healthy"},
            None,
            (),
            2,
            "release 2 is in progress (healthy); wait for it to finish",
        ),
        ({1: "active", 2: "superseded"}, None, (2,), 2, "release 2 is locked; unlock it first"),
    ],
)
async def test_a_protected_release_is_409_with_the_reason_and_stays_as_it_was(
    statuses: dict[int, str],
    target: int | None,
    locked: Sequence[int],
    release_no: int,
    message: str,
) -> None:
    harness = harness_with(statuses, target=target, locked=locked)

    reply = await harness.delete(release_no)

    assert reply.status == 409
    assert error_of(reply) == ("release_protected", message)
    assert harness.release(release_no).deleted_at is None
    assert harness.is_untouched()
    assert_no_leak(reply)


async def test_a_release_is_deletable_the_moment_it_is_unlocked() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2, locked=(1,))
    assert (await harness.delete(1)).status == 409

    await harness.unlock(1)

    assert (await harness.delete(1)).status == 204


async def test_a_deleted_release_cannot_be_deleted_locked_or_named_again() -> None:
    harness = harness_with({2: "active"}, target=2)
    seed_release(harness.registry, 1, "superseded", deleted=True)

    replies = [
        await harness.delete(1),
        await harness.lock(1),
        await harness.unlock(1),
        await harness.put(1, {"name": "again", "tags": []}),
    ]

    assert [reply.status for reply in replies] == [404, 404, 404, 404]
    assert {error_of(reply) for reply in replies} == {("not_found", "no such release")}
    assert harness.is_untouched()


# --- who may, and what the caller never sees ----------------------------------------------------


@pytest.mark.parametrize("person", [CAROL, ALICE, DANA])
async def test_the_owner_a_can_manage_holder_and_a_platform_admin_may_manage_releases(
    person: str,
) -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    replies = [
        await harness.put(1, {"name": "n", "tags": []}, person),
        await harness.lock(1, person),
        await harness.unlock(1, person),
        await harness.delete(1, person),
    ]

    assert [reply.status for reply in replies] == [200, 200, 200, 204]


async def test_a_person_who_can_only_use_the_app_gets_403_and_nothing_is_written() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2, locked=(1,))

    replies = [
        await harness.put(1, {"name": "n", "tags": []}, BOB),
        await harness.lock(2, BOB),
        await harness.unlock(1, BOB),
        await harness.delete(1, BOB),
    ]

    assert [reply.status for reply in replies] == [403, 403, 403, 403]
    assert {error_of(reply)[0] for reply in replies} == {"forbidden"}
    assert harness.is_untouched()


async def test_a_stranger_gets_the_404_of_an_app_that_does_not_exist() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    replies = [
        await harness.put(1, {"name": "n", "tags": []}, ERIN),
        await harness.lock(1, ERIN),
        await harness.unlock(1, ERIN),
        await harness.delete(1, ERIN),
    ]
    missing = await harness.request("DELETE", "/apps/nothing/releases/1", ERIN)

    assert {reply.status for reply in replies} == {404}
    assert all(reply.json() == missing.json() for reply in replies)
    assert harness.is_untouched()


@pytest.mark.parametrize("release_id", ["99", "0", "-1", "abc", "1.5", "01x", "9" * 11])
async def test_a_release_that_does_not_exist_is_404_on_every_route(release_id: str) -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    replies = [
        await harness.put(release_id, {"name": "n", "tags": []}),
        await harness.lock(release_id),
        await harness.unlock(release_id),
        await harness.delete(release_id),
    ]

    assert {reply.status for reply in replies} == {404}
    assert harness.is_untouched()


async def test_a_change_that_is_not_same_origin_is_refused_before_anything_is_read() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    reply = await harness.request("DELETE", "/apps/sales/releases/1", CAROL, same_origin=False)

    assert reply.status == 403
    assert harness.release(1).deleted_at is None
    assert harness.is_untouched()


async def test_a_request_with_no_identity_is_401() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)

    reply = await harness.delete(1, None)

    assert reply.status == 401
    assert harness.is_untouched()


async def test_while_the_registry_is_down_every_change_is_503_and_nothing_is_stored() -> None:
    harness = harness_with({1: "superseded", 2: "active"}, target=2)
    harness.registry.go_down()

    replies = [
        await harness.put(1, {"name": "n", "tags": []}),
        await harness.lock(1),
        await harness.unlock(1),
        await harness.delete(1),
    ]

    harness.registry.come_back()
    assert {reply.status for reply in replies} == {503}
    assert {error_of(reply)[0] for reply in replies} == {"registry_unavailable"}
    assert harness.is_untouched()
    assert harness.release(1).deleted_at is None


# --- another request's write between the route's write and its answer -------------------------

ANOTHER_ADMIN_TOKEN: Final = SecretStr("another-admins-token")
DELETE_SALES: Final = Change(WriteKind.APP_DELETE, key={"slug": "sales"})


def raced_harness() -> tuple[Harness, RacedRegistry]:
    """Release 1 is older, 2 is locked, 3 is active and the target."""
    registry = seed_standard_rows(RacedRegistry(clock=lambda: NOW))
    for release_no, status in ((1, "superseded"), (2, "superseded"), (3, "active")):
        seed_release(registry, release_no, status, locked=release_no == 2)
    set_target(registry, 3)
    return Harness(registry), registry


RACED_WRITES: Final[dict[str, tuple[Callable[[Harness], Awaitable[Reply]], str]]] = {
    "name": (lambda harness: harness.put(1, {"name": "named", "tags": []}), "release.label"),
    "lock": (lambda harness: harness.lock(1), "release.lock"),
    "unlock": (lambda harness: harness.unlock(2), "release.unlock"),
}


@pytest.mark.parametrize("route", sorted(RACED_WRITES))
async def test_an_app_deleted_right_after_the_write_is_404_and_the_write_stays_recorded(
    route: str,
) -> None:
    # The write committed and was audited; the app it changed is gone, so there is no release to
    # show, and a 404 is what the same request a moment later gets. A 500 would say it failed.
    harness, registry = raced_harness()
    send, action = RACED_WRITES[route]
    registry.after_next_write(DELETE_SALES, admin_token=ANOTHER_ADMIN_TOKEN)

    reply = await send(harness)

    assert (reply.status, error_of(reply)) == (404, ("not_found", "no such app"))
    assert [row.action for row in registry.audit_rows] == [action]
    assert registry.snapshot.app("sales") is None


async def test_a_release_deleted_right_after_it_was_named_is_404_like_a_deleted_one() -> None:
    harness, registry = raced_harness()
    registry.after_next_write(
        soft_delete_change(harness.release(1), deleted_by=ALICE, deleted_at=NOW)
    )

    reply = await harness.put(1, {"name": "named", "tags": []})

    assert (reply.status, error_of(reply)) == (404, ("not_found", "no such release"))
    assert [row.action for row in registry.audit_rows] == ["release.label"]
