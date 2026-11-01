"""Register an app and start an Update from the admin API (T079; US1; contracts/admin-api.md).

Four routes run behind the real guard and the real authorizer on the in-memory registry:

* ``POST /apps`` (``app.register``): a platform admin only; the ``apps`` row and the first
  ``app_config`` row are written together under that admin's own token, the app is stopped with no
  release, and one ``app.register`` audit row names the admin;
* ``GET /apps/{app}`` (``app.view``, ``CAN_MANAGE``): the app page; an app the caller cannot open
  looks exactly like one that does not exist;
* ``POST /apps/{app}/releases`` (``app.update``): hands the presser's own token to the Update flow
  and answers ``202`` with the new release, ``200`` with "already up to date", or the error that
  the flow's refusal maps to;
* ``GET /apps/{app}/releases/{id}``: the release with its progress and, for a failed one, the
  reason and the build-log tail.

The Update flow itself (``reconciler/update.py``, lane R) is replaced here by ``StubUpdater``, which
creates its release through the ``write`` it is given, exactly as the real flow must. Nothing here
reads a workspace folder or opens a connection, so what is pinned is the route's side of the
``ReleaseUpdater`` seam, not the flow.

Starting data: see ``edge_harness`` (``sales`` owned by alice, bob ``CAN_USE``, carol
``CAN_MANAGE``; ``docs`` owned by frank; dana is the platform admin). Erin is a stranger to all.
"""

from __future__ import annotations

from typing import Final, Literal

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
    seeded_registry,
)
from pydantic import SecretStr
from stub_registry import StubRegistry

from appmux.admin.api import create_admin_app
from appmux.admin.apps import AppRoutes
from appmux.admin.authorizer import make_authorizer
from appmux.admin.releases import ReleaseRoutes
from appmux.admin.schemas import AppDetail, ReleaseCreated, ReleaseDetail, ReleaseUnchanged
from appmux.identity.actor import Actor
from appmux.reconciler.ports import ChangeWriter, RefusalKind, UpdateRefused, UpdateResult
from appmux.registry.models import App, AppConfig
from appmux.registry.protocol import Change
from appmux.registry.writers import Identity, WriteKind

pytestmark = pytest.mark.anyio

GRACE: Final = "grace@example.com"
ERIN: Final = "erin@example.com"
DIGEST: Final = "sha256:" + "ab" * 32
# Text that must never come back to the client from a refused body.
SUBMITTED_MARKER: Final = "marker-value-from-the-client"
LEAK_MARKERS: Final = (VISITOR_TOKEN, "Traceback", "SELECT")
UNREADABLE_FOLDER: Final = (
    "the folder is empty, or you cannot read it with your own access; ask its owner for read "
    "access, then press Update again"
)
FAILURE_REASON: Final = "health check did not pass within 60 s (last error: connection refused)"

REGISTER_BODY: Final[dict[str, object]] = {
    "slug": "reports",
    "display_name": "Weekly reports",
    "source_path": "/Workspace/Shared/apps/reports",
    "memory_budget_mb": 256,
    "owner": GRACE,
}

UpdaterMode = Literal[
    "create",
    "up_to_date",
    "refuse",
    "registry_down",
    "forget_to_write",
    "write_then_up_to_date",
    "write_twice",
    "wrong_number",
    "malformed_digest",
]


class StubUpdater:
    """The Update flow as the route sees it: a ``ReleaseUpdater`` that records what it was given.

    ``create`` writes a pending release and points ``app_config`` at it through ``write``, like the
    real flow. ``registry_down`` stops the registry just before that write. The other modes are the
    answers the route must map, and defects of a flow that the route must not turn into a success:
    it reports a release it never wrote (``forget_to_write``), writes one and reports "up to date"
    (``write_then_up_to_date``), writes twice (``write_twice``), passes a ``release_no`` that is not
    its row's (``wrong_number``) or a digest that is not one (``malformed_digest``).
    """

    def __init__(self, registry: StubRegistry) -> None:
        self.registry = registry
        self.mode: UpdaterMode = "create"
        self.refusal = UpdateRefused("refused", UNREADABLE_FOLDER)
        self.live_release_no = 1
        self.calls: list[tuple[str, str, str]] = []  # (presser, app, the token as given)

    async def update(
        self, *, actor: Actor, app_slug: str, user_token: SecretStr, write: ChangeWriter
    ) -> UpdateResult:
        self.calls.append((actor.email, app_slug, user_token.get_secret_value()))
        if self.mode == "refuse":
            raise self.refusal
        if self.mode == "up_to_date":
            return UpdateResult(release_no=self.live_release_no, is_up_to_date=True)
        existing = self.registry.snapshot.releases_for(app_slug)
        release_no = max((release.release_no for release in existing), default=0) + 1
        if self.mode == "forget_to_write":
            return UpdateResult(release_no=release_no, is_up_to_date=False)
        config = self.registry.snapshot.app_config(app_slug)
        assert config is not None
        if self.mode == "registry_down":
            self.registry.go_down()
        changes = [
            Change(
                WriteKind.RELEASE_INSERT,
                values={
                    "app_slug": app_slug,
                    "release_no": release_no,
                    "snapshot_digest": "sha256:not-hex"
                    if self.mode == "malformed_digest"
                    else DIGEST,
                    "detected": {"runtime": "python"},
                    "created_by": actor.email,
                },
            ),
            Change(
                WriteKind.APP_CONFIG_UPDATE,
                key={"app_slug": app_slug},
                values={"target_release_no": release_no, "updated_by": actor.email},
                expected_version=config.version,
            ),
        ]
        reported_no = release_no + 1 if self.mode == "wrong_number" else release_no
        await write(changes, release_no=reported_no)
        if self.mode == "write_twice":
            await write(changes, release_no=release_no)
        return UpdateResult(
            release_no=release_no, is_up_to_date=self.mode == "write_then_up_to_date"
        )


class Harness:
    """The admin application with the app and release routes, on a registry and a stub updater."""

    def __init__(self, registry: StubRegistry | None = None) -> None:
        self.registry = registry if registry is not None else seeded_registry()
        self.updater = StubUpdater(self.registry)
        self.woken: list[str] = []
        self.app = create_admin_app(
            registry=self.registry,
            authorizer=make_authorizer(self.registry, FakeGroups()),
            route_providers=[
                AppRoutes(),
                ReleaseRoutes(self.updater, on_release_created=self.woken.append),
            ],
        )

    async def request(
        self, method: str, path: str, email: str | None, json_body: object = None
    ) -> Reply:
        return await call(self.app, method, path, email=email, json_body=json_body)

    async def register(self, email: str | None, body: object = None) -> Reply:
        return await self.request("POST", "/apps", email, REGISTER_BODY if body is None else body)

    async def update(self, email: str | None, slug: str = "sales") -> Reply:
        return await self.request("POST", f"/apps/{slug}/releases", email)

    def is_untouched(self) -> bool:
        """Nothing was written, recorded or signalled."""
        return self.registry.writes == () and self.registry.audit_rows == () and self.woken == []


def seed_release(
    registry: StubRegistry, release_no: int, status: str = "pending", **columns: object
) -> None:
    registry.seed(
        "releases",
        {
            "app_slug": "sales",
            "release_no": release_no,
            "snapshot_digest": DIGEST,
            "detected": {
                "runtime": "python",
                "framework": "fastapi",
                "command": ["uvicorn", "app:app", "--port", "8000"],
                "env": [{"name": "LOG_LEVEL", "value": "info"}],
                "warnings": ["app.yaml: unknown key 'resources' ignored"],
            },
            "config": {"env": [{"name": "WAREHOUSE", "value_from": "sql-warehouse"}]},
            "wheels": [
                {"sha256": "c" * 64, "filename": "tool-1.0-py3-none-any.whl", "source": "/w"}
            ],
            "status": status,
            "status_at": NOW,
            "created_by": CAROL,
            "created_at": NOW,
            **columns,
        },
    )


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


def app_of(registry: StubRegistry, slug: str) -> App:
    app = registry.snapshot.app(slug)
    assert app is not None
    return app


def config_of(registry: StubRegistry, slug: str) -> AppConfig:
    config = registry.snapshot.app_config(slug)
    assert config is not None
    return config


def error_of(reply: Reply) -> tuple[str, str]:
    error = reply.json()["error"]
    return error["code"], error["message"]


def assert_no_leak(reply: Reply) -> None:
    for marker in (*LEAK_MARKERS, SUBMITTED_MARKER):
        assert marker not in reply.text


# --- POST /apps ---------------------------------------------------------------------------------


async def test_a_platform_admin_registers_an_app_and_gets_its_page_back() -> None:
    harness = Harness()

    reply = await harness.register(DANA)

    assert reply.status == 200
    detail = AppDetail.model_validate_json(reply.body)
    assert (detail.slug, detail.display_name, detail.owner) == ("reports", "Weekly reports", GRACE)
    assert (detail.source_path, detail.memory_budget_mb) == ("/Workspace/Shared/apps/reports", 256)
    assert (detail.created_by, detail.updated_by, detail.version) == (DANA, DANA, 1)
    assert detail.state == "stopped"
    assert detail.releases == []
    assert detail.config.desired_state == "stopped"
    assert detail.config.target_release_no is None
    assert (detail.delegation.obo_delegated, detail.delegation.shared_sp_delegated) == (
        False,
        False,
    )
    assert (detail.egress, detail.grants, detail.resources) == ([], [], [])


async def test_the_app_is_stored_stopped_with_no_release_and_no_delegation() -> None:
    harness = Harness()

    await harness.register(DANA)

    app = app_of(harness.registry, "reports")
    assert (app.owner, app.source_path, app.memory_budget_mb) == (
        GRACE,
        "/Workspace/Shared/apps/reports",
        256,
    )
    assert (app.obo_delegated, app.shared_sp_delegated, list(app.egress)) == (False, False, [])
    config = config_of(harness.registry, "reports")
    assert (config.desired_state, config.target_release_no) == ("stopped", None)
    assert harness.registry.snapshot.releases_for("reports") == ()


async def test_the_platform_fields_are_written_under_the_admins_own_token_in_one_write() -> None:
    harness = Harness()

    await harness.register(DANA)

    (write,) = harness.registry.writes
    assert write.kinds == (WriteKind.APP_INSERT, WriteKind.APP_CONFIG_INSERT)
    assert write.identity is Identity.PLATFORM_ADMIN
    assert write.admin_token is not None
    assert write.admin_token.get_secret_value() == VISITOR_TOKEN


async def test_one_app_register_audit_row_names_the_admin_the_owner_and_the_budget() -> None:
    harness = Harness()

    await harness.register(DANA)

    (row,) = harness.registry.audit_rows
    assert (row.action, row.actor, row.app_slug) == ("app.register", DANA, "reports")
    assert row.detail == {"owner": GRACE, "budget_mb": 256}
    assert row.reason is None
    assert harness.woken == []  # registering starts nothing


async def test_the_new_owner_opens_the_page_of_the_app_but_a_stranger_does_not() -> None:
    harness = Harness()
    await harness.register(DANA)

    owner = await harness.request("GET", "/apps/reports", GRACE)
    stranger = await harness.request("GET", "/apps/reports", ERIN)

    assert owner.status == 200
    assert stranger.status == 404


@pytest.mark.parametrize("person", [ALICE, BOB, CAROL, ERIN])
async def test_nobody_but_a_platform_admin_registers_an_app(person: str) -> None:
    harness = Harness()

    reply = await harness.register(person)

    assert reply.status == 403
    code, message = error_of(reply)
    assert (code, "app.register" in message) == ("forbidden", True)
    assert harness.is_untouched()
    assert harness.registry.snapshot.app("reports") is None


async def test_a_taken_slug_is_409_slug_taken_and_the_existing_app_is_untouched() -> None:
    harness = Harness()
    before = app_of(harness.registry, "sales")

    reply = await harness.register(DANA, {**REGISTER_BODY, "slug": "sales", "owner": GRACE})

    assert reply.status == 409
    assert error_of(reply) == ("slug_taken", "slug 'sales' is already taken")
    assert harness.is_untouched()
    assert app_of(harness.registry, "sales") == before


async def test_registering_the_same_slug_twice_stores_one_app_and_one_audit_row() -> None:
    harness = Harness()

    first = await harness.register(DANA)
    second = await harness.register(DANA, {**REGISTER_BODY, "owner": ERIN})

    assert (first.status, second.status) == (200, 409)
    assert app_of(harness.registry, "reports").owner == GRACE
    assert len(harness.registry.audit_rows) == 1


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"slug": "Reports"}, "slug is not valid"),
        ({"slug": "_appmux"}, "slug is not valid"),
        ({"slug": "-reports"}, "slug is not valid"),
        ({"slug": "a" * 32}, "slug is not valid"),
        ({"display_name": "   "}, "display_name is not valid"),
        ({"source_path": "/Users/dana/reports"}, "source_path is not valid"),
        ({"memory_budget_mb": 63}, "memory_budget_mb is not valid"),
        ({"memory_budget_mb": 16385}, "memory_budget_mb is not valid"),
        ({"memory_budget_mb": "256"}, "memory_budget_mb is not valid"),
        ({"owner": "Grace@Example.com"}, "owner is not valid"),
        ({"owner": "not-an-email"}, "owner is not valid"),
        ({"obo_delegated": True}, "obo_delegated is not allowed"),
        ({"version": 1}, "version is not allowed"),
    ],
)
async def test_a_body_that_fails_its_schema_is_400_and_nothing_is_written(
    change: dict[str, object], problem: str
) -> None:
    harness = Harness()

    reply = await harness.register(DANA, {**REGISTER_BODY, **change})

    assert reply.status == 400
    code, message = error_of(reply)
    assert (code, problem in message) == ("bad_request", True)
    assert harness.is_untouched()


@pytest.mark.parametrize("field", list(REGISTER_BODY))
async def test_a_missing_field_is_400_naming_the_field(field: str) -> None:
    harness = Harness()
    body = {name: value for name, value in REGISTER_BODY.items() if name != field}

    reply = await harness.register(DANA, body)

    assert reply.status == 400
    assert f"{field} is required" in error_of(reply)[1]
    assert harness.is_untouched()


async def test_a_refused_body_never_comes_back_to_the_client() -> None:
    harness = Harness()

    reply = await harness.register(DANA, {**REGISTER_BODY, "owner": SUBMITTED_MARKER})

    assert reply.status == 400
    assert_no_leak(reply)


async def test_an_admin_whose_token_did_not_arrive_is_told_why_and_nothing_is_written() -> None:
    harness = Harness()

    reply = await call(
        harness.app,
        "POST",
        "/apps",
        headers=[("X-Forwarded-Email", DANA)],
        json_body=REGISTER_BODY,
        same_origin=True,
    )

    assert reply.status == 403
    code, message = error_of(reply)
    assert code == "user_token_missing"
    assert "access token" in message and "user authorization" in message
    assert harness.is_untouched()


async def test_an_admin_without_a_platform_database_role_gets_403_and_nothing_changes() -> None:
    registry = StubRegistry(clock=lambda: NOW, platform_writer_tokens=["a-token-nobody-sends"])
    registry.seed(
        "platform_settings",
        {
            "key": "platform_admins",
            "value": [{"type": "user", "name": DANA}],
            "updated_by": DANA,
            "updated_at": NOW,
        },
    )
    harness = Harness(registry)

    reply = await harness.register(DANA)

    assert reply.status == 403
    assert error_of(reply)[0] == "registry_role_missing"
    assert harness.is_untouched()
    assert registry.snapshot.app("reports") is None


async def test_registering_while_the_registry_is_down_is_503_and_stores_nothing() -> None:
    harness = Harness()
    harness.registry.go_down()

    reply = await harness.register(DANA)

    assert reply.status == 503
    assert error_of(reply)[0] == "registry_unavailable"
    harness.registry.come_back()
    assert harness.is_untouched()
    assert harness.registry.snapshot.app("reports") is None


# --- GET /apps/{app} ----------------------------------------------------------------------------


@pytest.mark.parametrize("person", [DANA, ALICE, CAROL])
async def test_a_platform_admin_the_owner_and_a_can_manage_holder_read_the_app_page(
    person: str,
) -> None:
    harness = Harness()

    reply = await harness.request("GET", "/apps/sales", person)

    assert reply.status == 200
    detail = AppDetail.model_validate_json(reply.body)
    assert (detail.slug, detail.owner, detail.version) == ("sales", ALICE, 1)
    assert [(g.name, g.permission) for g in detail.grants] == [
        (BOB, "CAN_USE"),
        (CAROL, "CAN_MANAGE"),
    ]
    assert harness.is_untouched()


async def test_a_person_who_can_only_use_the_app_gets_403_on_its_page() -> None:
    harness = Harness()

    reply = await harness.request("GET", "/apps/sales", BOB)

    assert reply.status == 403
    assert error_of(reply)[0] == "forbidden"


async def test_an_app_the_caller_cannot_open_reads_exactly_like_an_app_that_is_missing() -> None:
    harness = Harness()

    hidden = await harness.request("GET", "/apps/sales", ERIN)
    missing = await harness.request("GET", "/apps/nothing", ERIN)
    missing_for_admin = await harness.request("GET", "/apps/nothing", DANA)

    assert (hidden.status, missing.status, missing_for_admin.status) == (404, 404, 404)
    assert hidden.json() == missing.json() == missing_for_admin.json()


async def test_an_app_whose_settings_row_is_missing_is_503_not_a_broken_page() -> None:
    registry = seeded_registry()
    registry.seed(
        "apps",
        {
            "slug": "orphan",
            "display_name": "Orphan",
            "owner": FRANK,
            "source_path": "/Workspace/Shared/orphan",
            "memory_budget_mb": 128,
            "created_by": DANA,
            "created_at": NOW,
            "updated_by": DANA,
            "updated_at": NOW,
        },
    )
    harness = Harness(registry)

    reply = await harness.request("GET", "/apps/orphan", FRANK)

    assert reply.status == 503
    assert error_of(reply)[0] == "app_config_missing"


# --- POST /apps/{app}/releases ------------------------------------------------------------------


async def test_pressing_update_creates_a_pending_release_and_answers_202() -> None:
    harness = Harness()

    reply = await harness.update(CAROL)

    assert reply.status == 202
    created = ReleaseCreated.model_validate_json(reply.body)
    assert (created.release_no, created.status) == (1, "pending")
    release = harness.registry.snapshot.release("sales", 1)
    assert release is not None
    assert (release.status, release.created_by, release.snapshot_digest) == (
        "pending",
        CAROL,
        DIGEST,
    )
    assert config_of(harness.registry, "sales").target_release_no == 1


async def test_the_flow_gets_the_pressers_own_token_and_identity() -> None:
    harness = Harness()

    await harness.update(CAROL)

    assert harness.updater.calls == [(CAROL, "sales", VISITOR_TOKEN)]


async def test_the_release_is_written_as_the_service_principal_with_one_release_create_row() -> (
    None
):
    harness = Harness()

    await harness.update(CAROL)

    (write,) = harness.registry.writes
    assert write.kinds == (WriteKind.RELEASE_INSERT, WriteKind.APP_CONFIG_UPDATE)
    assert write.identity is Identity.APPMUX_SP
    assert write.admin_token is None  # the presser's token reads the folder; it never writes
    (row,) = harness.registry.audit_rows
    assert (row.action, row.actor, row.app_slug) == ("release.create", CAROL, "sales")
    assert row.detail == {"release": 1, "digest": DIGEST}


async def test_the_reconciler_is_woken_once_for_the_app_after_the_release_exists() -> None:
    harness = Harness()

    await harness.update(CAROL)

    assert harness.woken == ["sales"]


async def test_each_update_that_finds_a_change_makes_the_next_release_number() -> None:
    harness = Harness()

    first = await harness.update(ALICE)
    second = await harness.update(DANA)

    assert [ReleaseCreated.model_validate_json(r.body).release_no for r in (first, second)] == [
        1,
        2,
    ]
    assert config_of(harness.registry, "sales").target_release_no == 2
    assert [row.actor for row in harness.registry.audit_rows] == [ALICE, DANA]
    assert harness.woken == ["sales", "sales"]


async def test_an_update_whose_folder_equals_a_live_release_is_200_and_creates_nothing() -> None:
    harness = Harness()
    seed_release(harness.registry, 3, "active")
    set_target(harness.registry, 3)
    harness.updater.mode = "up_to_date"
    harness.updater.live_release_no = 3

    reply = await harness.update(CAROL)

    assert reply.status == 200
    unchanged = ReleaseUnchanged.model_validate_json(reply.body)
    assert (unchanged.status, unchanged.release_no) == ("up_to_date", 3)
    assert harness.is_untouched()  # no write, no audit row, no wake-up
    assert [r.release_no for r in harness.registry.snapshot.releases_for("sales")] == [3]
    assert config_of(harness.registry, "sales").target_release_no == 3


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("not_found", 404, "not_found"),
        ("refused", 400, "bad_request"),
        ("conflict", 409, "conflict"),
        ("unavailable", 503, "unavailable"),
    ],
)
async def test_a_refusal_of_the_flow_becomes_its_error_with_the_flows_message(
    kind: RefusalKind, status: int, code: str
) -> None:
    harness = Harness()
    harness.updater.mode = "refuse"
    harness.updater.refusal = UpdateRefused(kind, "the flow says what failed and what to do")

    reply = await harness.update(CAROL)

    assert reply.status == status
    assert error_of(reply) == (code, "the flow says what failed and what to do")
    assert harness.is_untouched()


async def test_a_read_with_no_access_fails_with_a_message_that_says_why_and_nothing_changes() -> (
    None
):
    harness = Harness()
    harness.updater.mode = "refuse"
    harness.updater.refusal = UpdateRefused("refused", UNREADABLE_FOLDER)
    before = config_of(harness.registry, "sales")

    reply = await harness.update(CAROL)

    assert reply.status == 400
    assert error_of(reply) == ("bad_request", UNREADABLE_FOLDER)
    assert harness.is_untouched()
    assert harness.registry.snapshot.releases_for("sales") == ()
    assert config_of(harness.registry, "sales") == before
    assert_no_leak(reply)


@pytest.mark.parametrize(("person", "status"), [(ALICE, 202), (CAROL, 202), (DANA, 202)])
async def test_the_owner_a_can_manage_holder_and_a_platform_admin_may_press_update(
    person: str, status: int
) -> None:
    harness = Harness()

    reply = await harness.update(person)

    assert reply.status == status
    assert harness.updater.calls == [(person, "sales", VISITOR_TOKEN)]


async def test_a_person_who_can_only_use_the_app_gets_403_and_the_flow_never_runs() -> None:
    harness = Harness()

    reply = await harness.update(BOB)

    assert reply.status == 403
    assert "app.update" in error_of(reply)[1]
    assert harness.updater.calls == []
    assert harness.is_untouched()


async def test_a_stranger_and_a_missing_app_get_the_same_404_and_the_flow_never_runs() -> None:
    harness = Harness()

    stranger = await harness.update(ERIN)
    missing = await harness.update(ERIN, "nothing")
    missing_for_admin = await harness.update(DANA, "nothing")

    assert (stranger.status, missing.status, missing_for_admin.status) == (404, 404, 404)
    assert stranger.json() == missing.json() == missing_for_admin.json()
    assert harness.updater.calls == []
    assert harness.is_untouched()


async def test_a_person_whose_token_did_not_arrive_is_told_why_and_the_flow_never_runs() -> None:
    harness = Harness()

    reply = await call(
        harness.app,
        "POST",
        "/apps/sales/releases",
        headers=[("X-Forwarded-Email", CAROL)],
        same_origin=True,
    )

    assert reply.status == 403
    code, message = error_of(reply)
    assert code == "user_token_missing"
    assert "access token" in message and "user authorization" in message
    assert harness.updater.calls == []
    assert harness.is_untouched()


async def test_an_update_from_another_site_is_refused_by_the_guard_before_the_flow() -> None:
    harness = Harness()

    reply = await call(harness.app, "POST", "/apps/sales/releases", email=CAROL, same_origin=False)

    assert reply.status == 403
    assert error_of(reply)[0] == "cross_origin"
    assert harness.updater.calls == []


async def test_a_failed_registry_write_inside_the_flow_is_503_with_no_release_and_no_wake_up() -> (
    None
):
    harness = Harness()
    harness.updater.mode = "registry_down"

    reply = await harness.update(CAROL)

    assert reply.status == 503
    assert error_of(reply)[0] == "registry_unavailable"
    harness.registry.come_back()
    assert harness.is_untouched()
    assert harness.registry.snapshot.releases_for("sales") == ()


async def test_a_flow_that_reports_a_release_it_never_wrote_is_a_500_and_wakes_nothing() -> None:
    harness = Harness()
    harness.updater.mode = "forget_to_write"

    reply = await harness.update(CAROL)

    assert reply.status == 500
    assert reply.json()["error"]["code"] == "internal_error"
    assert harness.woken == []
    assert harness.registry.snapshot.releases_for("sales") == ()


async def test_a_flow_that_wrote_a_release_but_says_up_to_date_is_a_500_not_a_success() -> None:
    harness = Harness()
    harness.updater.mode = "write_then_up_to_date"

    reply = await harness.update(CAROL)

    assert reply.status == 500
    assert reply.json()["error"]["code"] == "internal_error"
    assert harness.woken == []


@pytest.mark.parametrize("mode", ["wrong_number", "malformed_digest"])
async def test_a_release_row_the_audit_row_cannot_describe_is_a_500_and_stores_nothing(
    mode: UpdaterMode,
) -> None:
    harness = Harness()
    harness.updater.mode = mode

    reply = await harness.update(CAROL)

    assert reply.status == 500
    assert reply.json()["error"]["code"] == "internal_error"
    assert harness.is_untouched()
    assert harness.registry.snapshot.releases_for("sales") == ()


async def test_a_flow_that_writes_a_second_release_is_stopped_after_the_first() -> None:
    harness = Harness()
    harness.updater.mode = "write_twice"

    reply = await harness.update(CAROL)

    assert reply.status == 500
    assert [r.release_no for r in harness.registry.snapshot.releases_for("sales")] == [1]
    assert len(harness.registry.audit_rows) == 1
    assert harness.woken == []


# --- GET /apps/{app}/releases/{id} --------------------------------------------------------------


async def test_a_release_shows_its_status_content_and_whether_it_is_the_target() -> None:
    harness = Harness()
    seed_release(harness.registry, 2, "building", label="candidate", tags=["rc"])
    set_target(harness.registry, 2)

    reply = await harness.request("GET", "/apps/sales/releases/2", CAROL)

    assert reply.status == 200
    detail = ReleaseDetail.model_validate_json(reply.body)
    assert (detail.release_no, detail.status, detail.is_target) == (2, "building", True)
    assert (detail.name, detail.tags, detail.status_reason) == ("candidate", ["rc"], None)
    assert (detail.created_by, detail.snapshot_digest) == (CAROL, DIGEST)
    assert (detail.detected.runtime, detail.detected.framework) == ("python", "fastapi")
    assert detail.detected.command == ["uvicorn", "app:app", "--port", "8000"]
    assert [(e.name, e.value) for e in detail.detected.env] == [("LOG_LEVEL", "info")]
    assert detail.detected.warnings == ["app.yaml: unknown key 'resources' ignored"]
    assert [(e.name, e.value_from) for e in detail.config.env] == [("WAREHOUSE", "sql-warehouse")]
    assert [w.filename for w in detail.wheels] == ["tool-1.0-py3-none-any.whl"]
    assert harness.is_untouched()


async def test_a_release_that_is_not_the_target_says_so() -> None:
    harness = Harness()
    seed_release(harness.registry, 1, "superseded")
    seed_release(harness.registry, 2, "active")
    set_target(harness.registry, 2)

    old = await harness.request("GET", "/apps/sales/releases/1", ALICE)

    assert ReleaseDetail.model_validate_json(old.body).is_target is False


async def test_a_failed_release_shows_the_reason_and_the_tail_of_the_build_log() -> None:
    harness = Harness()
    tail = "\n".join(f"build line {number}" for number in range(1, 41))
    seed_release(harness.registry, 1, "failed", status_reason=FAILURE_REASON, build_log_tail=tail)
    set_target(harness.registry, 1)

    reply = await harness.request("GET", "/apps/sales/releases/1", CAROL)

    detail = ReleaseDetail.model_validate_json(reply.body)
    assert (detail.status, detail.status_reason) == ("failed", FAILURE_REASON)
    assert detail.build_log_tail == tail
    assert detail.build_log_tail.splitlines()[-1] == "build line 40"


async def test_progress_is_read_from_the_release_as_the_reconciler_moves_it_on() -> None:
    harness = Harness()
    created = await harness.update(CAROL)
    release_no = ReleaseCreated.model_validate_json(created.body).release_no
    seen: list[str] = []

    for status in ("pending", "building", "starting", "healthy", "active"):
        if status != "pending":
            await harness.registry.write(
                [
                    Change(
                        WriteKind.RELEASE_UPDATE,
                        key={"app_slug": "sales", "release_no": release_no},
                        values={"status": status},
                    )
                ]
            )
        reply = await harness.request("GET", f"/apps/sales/releases/{release_no}", CAROL)
        seen.append(ReleaseDetail.model_validate_json(reply.body).status)

    assert seen == ["pending", "building", "starting", "healthy", "active"]


async def test_the_new_release_is_listed_on_the_app_page_as_the_target() -> None:
    harness = Harness()
    await harness.update(CAROL)

    reply = await harness.request("GET", "/apps/sales", CAROL)

    detail = AppDetail.model_validate_json(reply.body)
    assert [(r.release_no, r.status, r.is_target) for r in detail.releases] == [
        (1, "pending", True)
    ]
    assert detail.config.target_release_no == 1


async def test_a_release_without_a_detected_runtime_still_shows_its_status_and_reason() -> None:
    # The registry allows an unset runtime (T087) while the contract says python or node; the
    # read must not hide the reason of a failure behind that gap.
    harness = Harness()
    seed_release(
        harness.registry,
        1,
        "failed",
        detected={},
        status_reason="no requirements.txt and the command does not start python",
    )

    reply = await harness.request("GET", "/apps/sales/releases/1", CAROL)

    assert reply.status == 200
    detail = ReleaseDetail.model_validate_json(reply.body)
    assert detail.status_reason == "no requirements.txt and the command does not start python"
    assert detail.detected.command == []


@pytest.mark.parametrize("person", [DANA, ALICE, CAROL])
async def test_the_release_read_needs_can_manage(person: str) -> None:
    harness = Harness()
    seed_release(harness.registry, 1, "active")

    reply = await harness.request("GET", "/apps/sales/releases/1", person)

    assert reply.status == 200


async def test_a_person_who_can_only_use_the_app_gets_403_on_a_release() -> None:
    harness = Harness()
    seed_release(harness.registry, 1, "active")

    reply = await harness.request("GET", "/apps/sales/releases/1", BOB)

    assert reply.status == 403


async def test_a_release_of_an_app_the_caller_cannot_open_reads_like_one_of_a_missing_app() -> None:
    harness = Harness()
    seed_release(harness.registry, 1, "active")

    hidden = await harness.request("GET", "/apps/sales/releases/1", ERIN)
    missing = await harness.request("GET", "/apps/nothing/releases/1", ERIN)

    assert (hidden.status, missing.status) == (404, 404)
    assert hidden.json() == missing.json()


@pytest.mark.parametrize("release_id", ["9", "0", "99999999999999999999", "abc", "-1", "1.5"])
async def test_a_release_that_does_not_exist_is_404(release_id: str) -> None:
    harness = Harness()
    seed_release(harness.registry, 1, "active")

    reply = await harness.request("GET", f"/apps/sales/releases/{release_id}", CAROL)

    assert reply.status == 404


async def test_a_deleted_release_is_404_because_its_snapshot_is_gone() -> None:
    harness = Harness()
    seed_release(
        harness.registry,
        1,
        "superseded",
        snapshot_digest=None,
        deleted_by="appmux",
        deleted_at=NOW,
    )

    reply = await harness.request("GET", "/apps/sales/releases/1", CAROL)

    assert reply.status == 404
    assert error_of(reply)[1] == "no such release"
