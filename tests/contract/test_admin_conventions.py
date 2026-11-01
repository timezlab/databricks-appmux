"""The conventions every admin route shares (contracts/admin-api.md, Conventions; T023).

The routes of this file are test-only providers: they stand for the route modules of later tasks
and use only what ``AdminContext`` offers. They run on the in-memory registry (``stub_registry``)
and are mounted at ``/_appmux/api/v1`` as the edge will mount them. What is pinned here:

* one error body for every error, with no stack trace, SQL text, path, class name or secret;
* identity first (``401``), then the same-origin check on mutating methods (``403``), then the
  route;
* ``409`` on a stale ``version``, ``503`` when the registry is down and the route needs it;
* a mutating route that did not record an audit entry fails closed;
* ``Cache-Control: no-store`` and ``X-Content-Type-Options: nosniff`` on every response.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal

import httpx
import pytest
from pydantic import AwareDatetime, BaseModel, ConfigDict, SecretStr, field_validator
from starlette.applications import Starlette
from starlette.endpoints import HTTPEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from stub_registry import StubRegistry

from appmux.admin.api import (
    MAX_BODY_BYTES,
    AdminContext,
    RegistryView,
    _AdminGuard,
    create_admin_app,
    parse_body,
)
from appmux.admin.errors import (
    AdminError,
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    Unauthorized,
    Unavailable,
    error_response,
)
from appmux.authz.actions import Action
from appmux.identity.actor import Actor
from appmux.registry.audit import AuditEntry
from appmux.registry.errors import (
    ChangedByOthers,
    InvalidRow,
    RegistryError,
    RegistryRoleMissing,
    RegistryUnavailable,
    RowAlreadyExists,
    RowMissing,
)
from appmux.registry.models import AuditLogRow, MetricRow
from appmux.registry.protocol import Change
from appmux.registry.writers import WriteKind

MOUNT: Final = "/_appmux/api/v1"
VIEWER: Final = "viewer@example.com"
OWNER: Final = "owner@example.com"
ADMIN: Final = "admin@example.com"
ADMIN_TOKEN: Final = SecretStr("admin-token-value-for-tests")
VISITOR_TOKEN: Final = "visitor-token-value-for-tests"  # noqa: S105 - a fake value for a test
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
IDENTITY: Final = {"X-Forwarded-Email": VIEWER}
MUTATING_METHODS: Final = ("POST", "PUT", "PATCH", "DELETE")
# What must never reach a body: each marker sits in an exception message or a submitted value below.
LEAKS: Final = (
    "Traceback",
    'File "',
    "SELECT",
    "password_hash",
    "/srv/appmux",
    "OperationalError",
    "hunter2-submitted-value",
    VISITOR_TOKEN,
)


class Reason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    reason: str | None = None


class Typed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
    name: str


class Echoing(BaseModel):
    """A body whose own validator repeats the submitted value in its message, as Pydantic can."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["a", "b"]
    name: str

    @field_validator("name")
    @classmethod
    def refuse_every_name(cls, value: str) -> str:
        raise ValueError(f"bad name {value}")


class OperationalError(Exception):
    """Stands for a database driver's error class: its name must not reach a body."""


class RecordingAuthorizer:
    """Allows what ``denied`` does not list and records every question."""

    def __init__(self) -> None:
        self.denied: set[tuple[str, str | None]] = set()
        self.unopenable: set[str] = set()
        self.answer: object = True
        self.fails = False
        self.calls: list[tuple[str, str, str | None]] = []

    async def __call__(self, actor: Actor, action: str, app_slug: str | None) -> bool:
        self.calls.append((actor.email, action, app_slug))
        if self.fails:
            raise RuntimeError("authorizer broke: SELECT password_hash FROM people")
        if action == Action.APP_OPEN and app_slug in self.unopenable:
            return False
        if (action, app_slug) in self.denied:
            return False
        return self.answer


def app_values(slug: str) -> dict[str, object]:
    return {
        "slug": slug,
        "display_name": slug.title(),
        "owner": OWNER,
        "source_path": f"/Workspace/Users/{OWNER}/{slug}",
        "memory_budget_mb": 256,
        "created_by": ADMIN,
        "updated_by": ADMIN,
    }


def register(registry: StubRegistry, slug: str) -> None:
    entry = AuditEntry.model_validate({"actor": ADMIN, "action": "app.register", "app_slug": slug})
    asyncio.run(
        registry.write(
            [
                Change(WriteKind.APP_INSERT, values=app_values(slug)),
                Change(WriteKind.APP_CONFIG_INSERT, values={"app_slug": slug, "updated_by": ADMIN}),
            ],
            [entry],
            admin_token=ADMIN_TOKEN,
        )
    )


class ProbeRoutes:
    """Every convention, reachable through a route of its own."""

    def __init__(self) -> None:
        self.grants_written = 0

    def routes(self, context: AdminContext) -> Sequence[Route]:
        def grant_change(slug: str) -> Change:
            self.grants_written += 1
            return Change(
                WriteKind.APP_GRANT_INSERT,
                values={
                    "app_slug": slug,
                    "principal_type": "user",
                    "principal": f"reader{self.grants_written}@example.com",
                    "permission": "CAN_USE",
                    "granted_by": OWNER,
                },
            )

        async def whoami(request: Request) -> Response:
            actor = context.actor(request)
            return JSONResponse(
                {"email": actor.email, "has_token": actor.visitor_token is not None}
            )

        async def raise_error(request: Request) -> Response:
            name = request.path_params["name"]
            errors: dict[str, Exception] = {
                "bad_request": BadRequest("field 'slug' must be lower case"),
                "unauthorized": Unauthorized(),
                "forbidden": Forbidden(),
                "not_found": NotFound(),
                "conflict": Conflict(),
                "unavailable": Unavailable(),
                "slug_taken": Conflict("slug 'reports' is already taken", code="slug_taken"),
                "changed_by_others": ChangedByOthers(),
                "registry_unavailable": RegistryUnavailable(),
                "registry_role_missing": RegistryRoleMissing(),
                "row_exists": RowAlreadyExists(),
                "row_missing": RowMissing(),
                "invalid_row": InvalidRow("the values do not fit apps: hunter2-submitted-value"),
                "registry_error": RegistryError(),
            }
            raise errors[name]

        async def boom(request: Request) -> Response:
            raise OperationalError(
                "SELECT password_hash FROM people WHERE token = 'hunter2-submitted-value' "
                "failed in /srv/appmux/registry/client.py"
            )

        async def parse(request: Request) -> Response:
            body = await parse_body(request, Typed)
            context.skip_audit(request, "the probe route changes nothing")
            return JSONResponse({"count": body.count})

        async def mutate(request: Request) -> Response:
            slug = request.path_params["slug"]
            entry = context.audit_entry(request, "grant.app", app_slug=slug)
            await context.write(request, [grant_change(slug)], [entry])
            return JSONResponse({"ok": True})

        async def silent(request: Request) -> Response:
            return JSONResponse({"ok": True})

        async def refused(request: Request) -> Response:
            raise Conflict("a state forbids that", code="state_forbids")

        async def no_audit_entries(request: Request) -> Response:
            await context.write(request, [grant_change(request.path_params["slug"])], [])
            return JSONResponse({"ok": True})

        async def someone_elses_entry(request: Request) -> Response:
            entry = AuditEntry.model_validate(
                {"actor": OWNER, "action": "grant.app", "app_slug": request.path_params["slug"]}
            )
            await context.write(request, [grant_change(request.path_params["slug"])], [entry])
            return JSONResponse({"ok": True})

        async def exempt(request: Request) -> Response:
            context.skip_audit(request, "a self-check changes no registry row")
            return JSONResponse({"ok": True})

        async def exempt_without_reason(request: Request) -> Response:
            context.skip_audit(request, "  ")
            return JSONResponse({"ok": True})

        async def swallowed(request: Request) -> Response:
            # A route that catches the failure of its write and answers 200 anyway.
            try:
                entry = context.audit_entry(request, "grant.app", app_slug="nowhere")
                await context.write(request, [grant_change("nowhere")], [entry])
            except RowMissing:
                pass
            return JSONResponse({"ok": True})

        async def with_reason(request: Request) -> Response:
            body = await parse_body(request, Reason)
            slug = request.path_params["slug"]
            entry = context.audit_entry(request, "grant.app", app_slug=slug, reason=body.reason)
            await context.write(request, [grant_change(slug)], [entry])
            return JSONResponse({"ok": True})

        async def config_early(request: Request) -> Response:
            body = await parse_body(request, Reason)
            slug = request.path_params["slug"]
            config = context.registry.snapshot.app_config(slug)
            assert config is not None
            context.require_version(config.version, body.version)
            return await write_config(request, slug, config.version)

        async def config_late(request: Request) -> Response:
            body = await parse_body(request, Reason)
            return await write_config(request, request.path_params["slug"], body.version)

        async def write_config(request: Request, slug: str, expected: int) -> Response:
            entry = context.audit_entry(request, "config.edit", app_slug=slug)
            change = Change(
                WriteKind.APP_CONFIG_UPDATE,
                key={"app_slug": slug},
                values={"health_path": "/healthz", "updated_by": VIEWER},
                expected_version=expected,
            )
            (row,) = await context.write(request, [change], [entry])
            return JSONResponse({"version": getattr(row, "version", None)})

        async def echoing(request: Request) -> Response:
            await parse_body(request, Echoing)
            return JSONResponse({})

        async def loud_headers(request: Request) -> Response:
            return JSONResponse(
                {},
                headers={"Cache-Control": "public, max-age=60", "X-Content-Type-Options": "foo"},
            )

        async def audit_only(request: Request) -> Response:
            entry = context.audit_entry(request, "release.view_source", app_slug="sales")
            rows = await context.write(request, [], [entry])
            return JSONResponse({"rows": len(rows)})

        async def nothing_at_all(request: Request) -> Response:
            await context.write(request, [], [])
            return JSONResponse({})

        async def two_entries(request: Request) -> Response:
            slug = request.path_params["slug"]
            first = context.audit_entry(request, "grant.app", app_slug=slug)
            second = context.audit_entry(request, "grant.app", app_slug=slug)
            await context.write(request, [grant_change(slug)], [first, second])
            return JSONResponse({})

        async def second_entry_of_another_actor(request: Request) -> Response:
            slug = request.path_params["slug"]
            first = context.audit_entry(request, "grant.app", app_slug=slug)
            second = AuditEntry.model_validate(
                {"actor": OWNER, "action": "grant.app", "app_slug": slug}
            )
            await context.write(request, [grant_change(slug)], [first, second])
            return JSONResponse({})

        async def first_entry_of_another_actor(request: Request) -> Response:
            slug = request.path_params["slug"]
            first = AuditEntry.model_validate(
                {"actor": OWNER, "action": "grant.app", "app_slug": slug}
            )
            second = context.audit_entry(request, "grant.app", app_slug=slug)
            await context.write(request, [grant_change(slug)], [first, second])
            return JSONResponse({})

        async def need(request: Request) -> Response:
            slug = request.path_params["slug"]
            await context.require(
                request,
                Action(request.path_params["action"]),
                None if slug == "-" else slug,
            )
            return JSONResponse({"allowed": True})

        async def snapshot_read(request: Request) -> Response:
            return JSONResponse({"apps": len(context.registry.snapshot.apps)})

        async def strict_read(request: Request) -> Response:
            context.require_registry()
            return JSONResponse({"apps": len(context.registry.snapshot.apps)})

        async def audit_read(request: Request) -> Response:
            rows = await context.registry.read_audit(
                app_slug=None,
                start=_EPOCH,
                end=_FAR_FUTURE,
                limit=50,
            )
            return JSONResponse({"items": [row.action for row in rows]})

        every_method = list(MUTATING_METHODS)
        return [
            Route("/whoami", whoami),
            Route("/raise/{name}", raise_error),
            Route("/boom", boom),
            Route("/parse", parse, methods=["POST"]),
            Route("/mutate/{slug}", mutate, methods=every_method),
            Route("/silent", silent, methods=["GET", *every_method]),
            Route("/refused", refused, methods=every_method),
            Route("/no-audit-entries/{slug}", no_audit_entries, methods=["POST"]),
            Route("/someone-elses-entry/{slug}", someone_elses_entry, methods=["POST"]),
            Route("/exempt", exempt, methods=["POST"]),
            Route("/exempt-without-reason", exempt_without_reason, methods=["POST"]),
            Route("/swallowed", swallowed, methods=["POST"]),
            Route("/with-reason/{slug}", with_reason, methods=["POST"]),
            Route("/config-early/{slug}", config_early, methods=["PUT"]),
            Route("/config-late/{slug}", config_late, methods=["PUT"]),
            Route("/echoing", echoing, methods=["POST"]),
            Route("/loud-headers", loud_headers),
            Route("/audit-only", audit_only, methods=["POST"]),
            Route("/nothing-at-all", nothing_at_all, methods=["POST"]),
            Route("/two-entries/{slug}", two_entries, methods=["POST"]),
            Route(
                "/second-entry-of-another-actor/{slug}",
                second_entry_of_another_actor,
                methods=["POST"],
            ),
            Route(
                "/first-entry-of-another-actor/{slug}",
                first_entry_of_another_actor,
                methods=["POST"],
            ),
            Route("/class-based", ClassBased),
            Route("/need/{action}/{slug}", need),
            Route("/snapshot-read", snapshot_read),
            Route("/strict-read", strict_read),
            Route("/audit-read", audit_read),
        ]


class ClassBased(HTTPEndpoint):
    """A class endpoint has no ``methods`` list, so the router passes any verb to its handlers."""

    async def get(self, request: Request) -> Response:
        return JSONResponse({"changed": False})

    async def post(self, request: Request) -> Response:
        return JSONResponse({"changed": True})

    async def delete(self, request: Request) -> Response:
        return JSONResponse({"changed": True})


class SecondProvider:
    """A second route module: the seam lets it add routes without touching the first."""

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def second(request: Request) -> Response:
            return JSONResponse({"from": "second"})

        return [Route("/second", second)]


_EPOCH: Final = datetime(2000, 1, 1, tzinfo=UTC)
_FAR_FUTURE: Final = datetime(2100, 1, 1, tzinfo=UTC)


class Harness:
    def __init__(self) -> None:
        self.registry = StubRegistry()
        self.authorizer = RecordingAuthorizer()
        self.probe = ProbeRoutes()
        register(self.registry, "sales")
        admin = create_admin_app(
            registry=self.registry,
            authorizer=self.authorizer,
            route_providers=[self.probe, SecondProvider()],
        )
        parent = Starlette(routes=[Mount(MOUNT, app=admin)])
        # raise_server_exceptions stays True: an exception that escapes the app fails the test.
        self.client = TestClient(parent)

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | Sequence[tuple[str, str]] | None = None,
        identity: bool = True,
        same_origin: bool = True,
        **kwargs: object,
    ):
        sent: list[tuple[str, str]] = []
        if identity:
            sent.extend(IDENTITY.items())
        if same_origin:
            sent.extend(SAME_ORIGIN.items())
        if headers is not None:
            sent.extend(headers.items() if isinstance(headers, dict) else headers)
        return self.client.request(method, f"{MOUNT}{path}", headers=sent, **kwargs)

    def get(self, path: str, **kwargs: object):
        return self.request("GET", path, **kwargs)


@pytest.fixture
def harness() -> Harness:
    return Harness()


def error_of(response: httpx.Response) -> dict[str, str]:
    """The body of an error response, checked to be exactly the contract's shape."""
    body = response.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) == {"code", "message"}, body
    assert isinstance(body["error"]["code"], str)
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    return body["error"]


def assert_no_leaks(response: httpx.Response) -> None:
    for leak in LEAKS:
        assert leak not in response.text, f"{leak!r} reached the response body"


# --- error bodies --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "status", "code", "message"),
    [
        ("bad_request", 400, "bad_request", "field 'slug' must be lower case"),
        ("unauthorized", 401, "unauthorized", None),
        ("forbidden", 403, "forbidden", None),
        ("not_found", 404, "not_found", None),
        ("conflict", 409, "conflict", None),
        ("unavailable", 503, "unavailable", None),
        ("slug_taken", 409, "slug_taken", "slug 'reports' is already taken"),
        (
            "changed_by_others",
            409,
            "changed_by_others",
            "changed by someone else, reload and retry",
        ),
        ("registry_unavailable", 503, "registry_unavailable", None),
        (
            "registry_role_missing",
            403,
            "registry_role_missing",
            "your database role cannot write platform data; ask the Lakebase project owner",
        ),
        ("row_exists", 409, "row_exists", None),
        ("row_missing", 404, "row_missing", None),
        ("invalid_row", 400, "invalid_row", None),
    ],
)
def test_every_error_type_answers_the_contract_body(
    harness: Harness, name: str, status: int, code: str, message: str | None
) -> None:
    response = harness.get(f"/raise/{name}")

    assert response.status_code == status
    error = error_of(response)
    assert error["code"] == code
    if message is not None:
        assert error["message"] == message
    assert_no_leaks(response)


def test_a_registry_error_message_is_the_fixed_one_not_the_raised_one(harness: Harness) -> None:
    # InvalidRow's own message names fields; the body still carries the fixed text only.
    response = harness.get("/raise/invalid_row")

    assert error_of(response)["message"] == InvalidRow.default_message
    assert_no_leaks(response)


def test_an_unknown_registry_error_is_a_generic_500(harness: Harness) -> None:
    response = harness.get("/raise/registry_error")

    assert response.status_code == 500
    assert error_of(response)["code"] == "internal_error"


def test_an_unexpected_exception_is_a_generic_500_logged_once_with_its_traceback(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = harness.get("/boom")

    assert response.status_code == 500
    assert error_of(response)["code"] == "internal_error"
    assert_no_leaks(response)
    assert "OperationalError" not in response.text
    failures = [record for record in caplog.records if record.exc_info is not None]
    assert len(failures) == 1
    assert failures[0].levelno >= logging.ERROR
    assert failures[0].exc_info is not None
    assert failures[0].exc_info[0] is OperationalError


def test_error_response_builds_the_body_without_an_app() -> None:
    response = error_response(NotFound("no such app 'x'"))

    assert response.status_code == 404
    assert json.loads(response.body) == {
        "error": {"code": "not_found", "message": "no such app 'x'"}
    }


def test_an_admin_error_code_must_be_snake_case() -> None:
    with pytest.raises(ValueError, match="code"):
        Conflict("x", code="Not A Code")


def test_admin_error_types_share_one_base() -> None:
    for error_type in (BadRequest, Unauthorized, Forbidden, NotFound, Conflict, Unavailable):
        assert issubclass(error_type, AdminError)


# --- request validation --------------------------------------------------------------------------


def test_a_valid_body_is_parsed(harness: Harness) -> None:
    response = harness.request("POST", "/parse", json={"count": 3, "name": "a"})

    assert response.status_code == 200
    assert response.json() == {"count": 3}


def test_a_body_that_fails_its_schema_is_400_naming_fields_and_never_the_value(
    harness: Harness,
) -> None:
    response = harness.request(
        "POST", "/parse", json={"count": "hunter2-submitted-value", "extra_field": 1}
    )

    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "bad_request"
    assert "count" in error["message"]
    assert "extra_field" in error["message"]
    assert "name" in error["message"]
    assert_no_leaks(response)


def test_a_missing_field_is_named(harness: Harness) -> None:
    response = harness.request("POST", "/parse", json={"count": 1})

    assert response.status_code == 400
    assert "name" in error_of(response)["message"]


def test_a_body_that_is_not_json_is_400_without_echoing_it(harness: Harness) -> None:
    response = harness.request(
        "POST",
        "/parse",
        content=b'{"count": hunter2-submitted-value',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert error_of(response)["code"] == "bad_request"
    assert_no_leaks(response)


def test_a_body_that_is_not_declared_as_json_is_400(harness: Harness) -> None:
    response = harness.request(
        "POST",
        "/parse",
        content=b'{"count": 1, "name": "a"}',
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 400
    assert error_of(response)["code"] == "bad_request"


def test_a_body_over_the_cap_is_400(harness: Harness) -> None:
    padding = "x" * (MAX_BODY_BYTES + 1)
    response = harness.request("POST", "/parse", json={"count": 1, "name": padding})

    assert response.status_code == 400
    assert error_of(response)["code"] == "bad_request"


def test_a_hostile_field_name_is_shortened_and_printable(harness: Harness) -> None:
    hostile = "evil\x1b[31m" + "y" * 500
    response = harness.request("POST", "/parse", json={"count": 1, "name": "a", hostile: 1})

    assert response.status_code == 400
    message = error_of(response)["message"]
    assert "\x1b" not in message
    assert len(message) < 400


# --- identity ------------------------------------------------------------------------------------


def test_no_identity_is_401(harness: Harness) -> None:
    response = harness.get("/whoami", identity=False)

    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthorized"


def test_a_repeated_identity_header_is_401(harness: Harness) -> None:
    response = harness.get(
        "/whoami",
        identity=False,
        headers=[("X-Forwarded-Email", VIEWER), ("X-Forwarded-Email", "other@example.com")],
    )

    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthorized"


def test_a_repeated_token_header_is_401_and_never_echoed(harness: Harness) -> None:
    response = harness.get(
        "/whoami",
        headers=[
            ("X-Forwarded-Access-Token", VISITOR_TOKEN),
            ("X-Forwarded-Access-Token", VISITOR_TOKEN),
        ],
    )

    assert response.status_code == 401
    assert VISITOR_TOKEN not in response.text


def test_identity_is_checked_before_the_same_origin_rule(harness: Harness) -> None:
    response = harness.request("POST", "/silent", identity=False, same_origin=False)

    assert response.status_code == 401


def test_identity_is_checked_before_the_route_is_looked_up(harness: Harness) -> None:
    response = harness.get("/no-such-route", identity=False)

    assert response.status_code == 401


def test_the_actor_reaches_the_route_and_its_token_is_not_returned(harness: Harness) -> None:
    response = harness.get("/whoami", headers={"X-Forwarded-Access-Token": VISITOR_TOKEN})

    assert response.json() == {"email": VIEWER, "has_token": True}
    assert VISITOR_TOKEN not in response.text


# --- same-origin ---------------------------------------------------------------------------------


@pytest.mark.parametrize("method", MUTATING_METHODS)
def test_a_mutating_method_without_sec_fetch_site_is_403(harness: Harness, method: str) -> None:
    response = harness.request(method, "/silent", same_origin=False)

    assert response.status_code == 403
    assert error_of(response)["code"] == "cross_origin"


@pytest.mark.parametrize(
    "value", ["cross-site", "same-site", "none", "", "Same-Origin ", "same-origin, none"]
)
def test_any_sec_fetch_site_but_same_origin_is_403(harness: Harness, value: str) -> None:
    response = harness.request(
        "POST", "/silent", same_origin=False, headers={"Sec-Fetch-Site": value}
    )

    assert response.status_code == 403


def test_a_repeated_sec_fetch_site_is_403(harness: Harness) -> None:
    response = harness.request(
        "POST",
        "/silent",
        same_origin=False,
        headers=[("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin")],
    )

    assert response.status_code == 403


def test_a_get_needs_no_sec_fetch_site(harness: Harness) -> None:
    response = harness.get("/silent", same_origin=False)

    assert response.status_code == 200


@pytest.mark.parametrize("method", MUTATING_METHODS)
def test_a_same_origin_mutating_call_reaches_the_route(harness: Harness, method: str) -> None:
    response = harness.request(method, "/mutate/sales")

    assert response.status_code == 200, response.text


# --- version -------------------------------------------------------------------------------------


def test_a_current_version_is_accepted_and_the_new_one_returned(harness: Harness) -> None:
    for route in ("/config-early/sales", "/config-late/sales"):
        current = harness.registry.snapshot.app_config("sales")
        assert current is not None

        response = harness.request("PUT", route, json={"version": current.version})

        assert response.status_code == 200, response.text
        assert response.json() == {"version": current.version + 1}


@pytest.mark.parametrize("route", ["/config-early/sales", "/config-late/sales"])
def test_a_stale_version_is_409_with_the_fixed_message(harness: Harness, route: str) -> None:
    response = harness.request("PUT", route, json={"version": 0})

    assert response.status_code == 409
    error = error_of(response)
    assert error["code"] == "changed_by_others"
    assert error["message"] == "changed by someone else, reload and retry"
    assert harness.registry.audit_rows[-1].action == "app.register"  # nothing new was written


# --- registry down -------------------------------------------------------------------------------


def test_a_write_while_the_registry_is_down_is_503(harness: Harness) -> None:
    harness.registry.go_down()

    response = harness.request("POST", "/mutate/sales")

    assert response.status_code == 503
    assert error_of(response)["code"] == "registry_unavailable"


def test_the_registry_view_forwards_every_argument_of_a_read() -> None:
    class RecordingRegistry(StubRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.reads: list[tuple[str, str | None, datetime, datetime, int]] = []

        async def read_audit(
            self, *, app_slug: str | None, start: AwareDatetime, end: AwareDatetime, limit: int
        ) -> tuple[AuditLogRow, ...]:
            self.reads.append(("audit", app_slug, start, end, limit))
            return await super().read_audit(app_slug=app_slug, start=start, end=end, limit=limit)

        async def read_metrics(
            self, *, app_slug: str | None, start: AwareDatetime, end: AwareDatetime, limit: int
        ) -> tuple[MetricRow, ...]:
            self.reads.append(("metrics", app_slug, start, end, limit))
            return await super().read_metrics(app_slug=app_slug, start=start, end=end, limit=limit)

    registry = RecordingRegistry()
    view = RegistryView(registry)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 10, 2, tzinfo=UTC)

    async def read_both() -> None:
        await view.read_audit(app_slug="sales", start=start, end=end, limit=7)
        await view.read_metrics(app_slug="docs", start=start, end=end, limit=9)

    asyncio.run(read_both())

    assert registry.reads == [
        ("audit", "sales", start, end, 7),
        ("metrics", "docs", start, end, 9),
    ]


def test_a_database_read_while_the_registry_is_down_is_503(harness: Harness) -> None:
    harness.registry.go_down()

    response = harness.get("/audit-read")

    assert response.status_code == 503
    assert error_of(response)["code"] == "registry_unavailable"


def test_a_route_that_needs_the_registry_is_503_while_last_error_is_set(harness: Harness) -> None:
    harness.registry.go_down()

    assert harness.get("/strict-read").status_code == 503

    harness.registry.come_back()
    assert harness.get("/strict-read").status_code == 200


def test_a_route_that_reads_the_snapshot_still_answers_while_the_registry_is_down(
    harness: Harness,
) -> None:
    harness.registry.go_down()

    response = harness.get("/snapshot-read")

    assert response.status_code == 200
    assert response.json() == {"apps": 1}


def test_a_failed_write_leaves_no_audit_row(harness: Harness) -> None:
    before = len(harness.registry.audit_rows)
    harness.registry.go_down()

    harness.request("POST", "/mutate/sales")

    harness.registry.come_back()
    assert len(harness.registry.audit_rows) == before


# --- audit ---------------------------------------------------------------------------------------


def test_a_mutating_route_writes_its_audit_row_with_the_actor(harness: Harness) -> None:
    response = harness.request("POST", "/mutate/sales")

    assert response.status_code == 200
    row = harness.registry.audit_rows[-1]
    assert (row.actor, row.action, row.app_slug) == (VIEWER, "grant.app", "sales")


@pytest.mark.parametrize("method", MUTATING_METHODS)
def test_a_mutating_route_that_records_no_audit_entry_fails_closed(
    harness: Harness, method: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        response = harness.request(method, "/silent")

    assert response.status_code == 500
    assert error_of(response)["code"] == "internal_error"
    assert "ok" not in response.text
    assert any("audit" in record.getMessage().lower() for record in caplog.records)


def test_a_read_route_needs_no_audit_entry(harness: Harness) -> None:
    assert harness.get("/silent").status_code == 200


def test_a_refused_mutation_needs_no_audit_entry(harness: Harness) -> None:
    response = harness.request("POST", "/refused")

    assert response.status_code == 409
    assert error_of(response)["code"] == "state_forbids"


def test_a_route_that_swallows_the_failure_of_its_write_still_fails_closed(
    harness: Harness,
) -> None:
    response = harness.request("POST", "/swallowed")

    assert response.status_code == 500
    assert error_of(response)["code"] == "internal_error"


def test_a_write_with_no_audit_entries_is_refused_and_writes_nothing(harness: Harness) -> None:
    before = harness.registry.snapshot.grants_for("sales")

    response = harness.request("POST", "/no-audit-entries/sales")

    assert response.status_code == 500
    assert harness.registry.snapshot.grants_for("sales") == before


def test_an_audit_entry_for_another_actor_is_refused_and_writes_nothing(harness: Harness) -> None:
    before = harness.registry.snapshot.grants_for("sales")

    response = harness.request("POST", "/someone-elses-entry/sales")

    assert response.status_code == 500
    assert harness.registry.snapshot.grants_for("sales") == before


def test_a_route_may_declare_that_it_changes_nothing(harness: Harness) -> None:
    assert harness.request("POST", "/exempt").status_code == 200


def test_declaring_no_audit_needs_a_reason(harness: Harness) -> None:
    assert harness.request("POST", "/exempt-without-reason").status_code == 500


def test_an_audit_reason_that_looks_like_a_credential_is_400_and_not_echoed(
    harness: Harness,
) -> None:
    response = harness.request(
        "POST",
        "/with-reason/sales",
        json={"version": 1, "reason": "password=hunter2-submitted-value"},
    )

    assert response.status_code == 400
    assert error_of(response)["code"] == "bad_request"
    assert_no_leaks(response)
    assert harness.registry.snapshot.grants_for("sales") == ()


def test_an_audit_reason_is_stored(harness: Harness) -> None:
    response = harness.request(
        "POST", "/with-reason/sales", json={"version": 1, "reason": "the owner asked"}
    )

    assert response.status_code == 200
    assert harness.registry.audit_rows[-1].reason == "the owner asked"


# --- authorization -------------------------------------------------------------------------------


def test_an_allowed_action_passes_and_the_authorizer_is_asked_with_the_actor(
    harness: Harness,
) -> None:
    response = harness.get("/need/config.edit/sales")

    assert response.status_code == 200
    assert (VIEWER, "config.edit", "sales") in harness.authorizer.calls


def test_a_denied_action_on_an_app_the_actor_can_open_is_403(harness: Harness) -> None:
    harness.authorizer.denied.add(("config.edit", "sales"))

    response = harness.get("/need/config.edit/sales")

    assert response.status_code == 403
    assert error_of(response)["code"] == "forbidden"


def test_an_unknown_app_is_404_even_for_an_actor_the_authorizer_would_allow(
    harness: Harness,
) -> None:
    response = harness.get("/need/config.edit/nowhere")

    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_an_app_the_actor_cannot_open_is_404_like_an_unknown_one(harness: Harness) -> None:
    harness.authorizer.unopenable.add("sales")
    hidden = harness.get("/need/config.edit/sales")
    unknown = harness.get("/need/config.edit/nowhere")

    assert hidden.status_code == unknown.status_code == 404
    assert hidden.json() == unknown.json()


def test_a_platform_action_without_an_app_is_checked_with_no_app(harness: Harness) -> None:
    harness.authorizer.denied.add(("settings.edit", None))

    assert harness.get("/need/settings.edit/-").status_code == 403
    assert harness.get("/need/egress.edit/-").status_code == 200
    assert (VIEWER, "settings.edit", None) in harness.authorizer.calls


@pytest.mark.parametrize("answer", [None, 0, "yes", [True]])
def test_only_true_allows(harness: Harness, answer: object) -> None:
    harness.authorizer.answer = answer

    response = harness.get("/need/settings.edit/-")

    assert response.status_code == 403


def test_an_authorizer_that_fails_denies_with_a_generic_500(harness: Harness) -> None:
    harness.authorizer.fails = True

    response = harness.get("/need/settings.edit/-")

    assert response.status_code == 500
    assert error_of(response)["code"] == "internal_error"
    assert_no_leaks(response)


def test_an_app_lookup_while_nothing_is_loaded_is_503() -> None:
    class NeverLoaded(StubRegistry):
        @property
        def snapshot(self):
            raise RegistryUnavailable

    registry = NeverLoaded()
    admin = create_admin_app(
        registry=registry, authorizer=RecordingAuthorizer(), route_providers=[ProbeRoutes()]
    )

    response = TestClient(admin).get("/need/config.edit/sales", headers={**IDENTITY, **SAME_ORIGIN})

    assert response.status_code == 503


# --- headers, routing and the seam ---------------------------------------------------------------


@pytest.mark.parametrize(
    "request_args",
    [
        ("GET", "/whoami", {}),
        ("GET", "/raise/not_found", {}),
        ("GET", "/boom", {}),
        ("GET", "/whoami", {"identity": False}),
        ("POST", "/silent", {"same_origin": False}),
        ("GET", "/no-such-route", {}),
        ("PUT", "/whoami", {}),
        ("POST", "/silent", {}),
    ],
)
def test_every_response_is_not_cached_and_not_sniffed(
    harness: Harness, request_args: tuple[str, str, dict[str, object]]
) -> None:
    method, path, options = request_args

    response = harness.request(method, path, **options)

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_an_unknown_route_uses_the_error_body(harness: Harness) -> None:
    response = harness.get("/no-such-route")

    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_a_wrong_method_uses_the_error_body_and_keeps_the_allow_header(harness: Harness) -> None:
    response = harness.request("PUT", "/whoami")

    assert response.status_code == 405
    assert error_of(response)["code"] == "method_not_allowed"
    assert "GET" in response.headers["allow"]


def test_routes_of_every_provider_are_served(harness: Harness) -> None:
    assert harness.get("/second").json() == {"from": "second"}
    assert harness.get("/whoami").status_code == 200


def test_a_provider_may_only_return_routes() -> None:
    class Wrong:
        def routes(self, context: AdminContext) -> Sequence[Route]:
            return [Mount("/x", routes=[])]

    with pytest.raises(TypeError, match="Route"):
        create_admin_app(
            registry=StubRegistry(), authorizer=RecordingAuthorizer(), route_providers=[Wrong()]
        )


def test_the_app_works_without_a_mount_and_without_providers() -> None:
    admin = create_admin_app(registry=StubRegistry(), authorizer=RecordingAuthorizer())

    response = TestClient(admin).get("/anything", headers=IDENTITY)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_an_audit_entry_is_built_for_the_requesting_actor(harness: Harness) -> None:
    captured: list[AuditEntry] = []

    class Capture:
        def routes(self, context: AdminContext) -> Sequence[Route]:
            async def build(request: Request) -> Response:
                captured.append(context.audit_entry(request, "app.start", app_slug="sales"))
                return JSONResponse({})

            return [Route("/capture", build)]

    admin = create_admin_app(
        registry=harness.registry, authorizer=harness.authorizer, route_providers=[Capture()]
    )
    TestClient(admin).get("/capture", headers=IDENTITY)

    assert [(entry.actor, entry.action, entry.app_slug) for entry in captured] == [
        (VIEWER, "app.start", "sales")
    ]


# --- gaps found by review: values, versions, audit entries, headers ------------------------------


def test_validation_never_repeats_a_submitted_value_even_when_pydantic_would(
    harness: Harness,
) -> None:
    response = harness.request(
        "POST", "/echoing", json={"kind": "c-submitted-kind", "name": "hunter2-submitted-value"}
    )

    assert response.status_code == 400
    message = error_of(response)["message"]
    assert message == "the request is not valid: kind is not valid; name is not valid"
    assert "submitted" not in response.text
    assert_no_leaks(response)


def test_validation_names_at_most_ten_fields_and_counts_the_rest(harness: Harness) -> None:
    unknown = {f"extra{number:02d}": 1 for number in range(12)}
    response = harness.request("POST", "/parse", json={"count": 1, "name": "a", **unknown})

    assert response.status_code == 400
    message = error_of(response)["message"]
    assert message.count("is not allowed") == 10
    assert message.endswith("; and 2 more")


@pytest.mark.parametrize(
    "content_type",
    ["application/json; charset=utf-8", "Application/JSON", " application/json ;x=y"],
)
def test_the_json_media_type_may_carry_parameters_and_any_case(
    harness: Harness, content_type: str
) -> None:
    response = harness.request(
        "POST",
        "/parse",
        content=b'{"count": 2, "name": "a"}',
        headers={"Content-Type": content_type},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"count": 2}


def test_a_version_above_the_stored_one_is_409_as_well_as_one_below(harness: Harness) -> None:
    current = harness.registry.snapshot.app_config("sales")
    assert current is not None
    audit_before = len(harness.registry.audit_rows)

    for sent in (current.version + 1, current.version + 5, current.version - 1):
        response = harness.request("PUT", "/config-early/sales", json={"version": sent})

        assert response.status_code == 409, sent
        assert error_of(response)["code"] == "changed_by_others"
    assert len(harness.registry.audit_rows) == audit_before


def test_require_version_raises_for_any_difference_and_only_then() -> None:
    AdminContext.require_version(3, 3)
    for sent in (2, 4, 400):
        with pytest.raises(Conflict) as raised:
            AdminContext.require_version(3, sent)
        assert raised.value.error_code == "changed_by_others"
        assert raised.value.message == ChangedByOthers.default_message


def test_every_audit_entry_must_name_the_actor_not_only_the_first(harness: Harness) -> None:
    before = harness.registry.snapshot.grants_for("sales")
    audit_before = len(harness.registry.audit_rows)

    response = harness.request("POST", "/second-entry-of-another-actor/sales")

    assert response.status_code == 500
    assert harness.registry.snapshot.grants_for("sales") == before
    assert len(harness.registry.audit_rows) == audit_before


@pytest.mark.parametrize("route", ["first-entry-of-another-actor", "second-entry-of-another-actor"])
def test_an_entry_of_another_actor_is_refused_in_any_position(harness: Harness, route: str) -> None:
    # [own, other] and [other, own]: a check of only the first or only the last entry passes one.
    before = harness.registry.snapshot.grants_for("sales")
    audit_before = len(harness.registry.audit_rows)

    response = harness.request("POST", f"/{route}/sales")

    assert response.status_code == 500
    assert harness.registry.snapshot.grants_for("sales") == before
    assert len(harness.registry.audit_rows) == audit_before


def test_several_audit_entries_of_the_actor_are_all_written(harness: Harness) -> None:
    audit_before = len(harness.registry.audit_rows)

    response = harness.request("POST", "/two-entries/sales")

    assert response.status_code == 200, response.text
    new_rows = harness.registry.audit_rows[audit_before:]
    assert [(row.actor, row.action) for row in new_rows] == [(VIEWER, "grant.app")] * 2


def test_an_audit_only_write_counts_as_audited(harness: Harness) -> None:
    audit_before = len(harness.registry.audit_rows)
    grants_before = harness.registry.snapshot.grants_for("sales")

    response = harness.request("POST", "/audit-only")

    assert response.status_code == 200, response.text
    assert response.json() == {"rows": 0}
    (row,) = harness.registry.audit_rows[audit_before:]
    assert (row.actor, row.action, row.app_slug) == (VIEWER, "release.view_source", "sales")
    assert harness.registry.snapshot.grants_for("sales") == grants_before


def test_a_write_of_nothing_at_all_is_refused(harness: Harness) -> None:
    audit_before = len(harness.registry.audit_rows)

    response = harness.request("POST", "/nothing-at-all")

    assert response.status_code == 500
    assert len(harness.registry.audit_rows) == audit_before


def test_a_route_cannot_loosen_the_cache_and_sniffing_headers(harness: Harness) -> None:
    response = harness.get("/loud-headers")

    assert response.status_code == 200
    assert response.headers.get_list("cache-control") == ["no-store"]
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]


# --- authorization: unknown and forbidden apps do the same work ----------------------------------


def trace_of_open_check(slug: str, *, unopenable: bool, answer: object) -> list[tuple[str, ...]]:
    """What one ``require`` on ``slug`` asks of the authorizer and of the registry, in order."""
    events: list[tuple[str, ...]] = []

    class TracingRegistry(StubRegistry):
        @property
        def snapshot(self):
            events.append(("snapshot",))
            return super().snapshot

    class TracingAuthorizer(RecordingAuthorizer):
        async def __call__(self, actor: Actor, action: str, app_slug: str | None) -> bool:
            events.append(("authorize", action, "<app>" if app_slug == slug else str(app_slug)))
            return await super().__call__(actor, action, app_slug)

    registry = TracingRegistry()
    register(registry, "sales")
    authorizer = TracingAuthorizer()
    authorizer.answer = answer
    if unopenable:
        authorizer.unopenable.add(slug)
    admin = create_admin_app(
        registry=registry, authorizer=authorizer, route_providers=[ProbeRoutes()]
    )
    events.clear()

    response = TestClient(admin).get(
        f"/need/config.edit/{slug}", headers={**IDENTITY, **SAME_ORIGIN}
    )

    assert response.status_code == 404
    return events


@pytest.mark.parametrize("answer", [True, False])
def test_an_unknown_app_and_an_unopenable_one_cause_the_same_authorizer_and_lookup_calls(
    answer: object,
) -> None:
    unknown = trace_of_open_check("nowhere", unopenable=False, answer=answer)
    hidden = trace_of_open_check("sales", unopenable=True, answer=answer)

    assert unknown == hidden
    assert ("authorize", "app.open", "<app>") in unknown
    assert ("snapshot",) in unknown
    # Neither answer went on to ask about the action itself.
    assert all(event[1:2] != ("config.edit",) for event in unknown)


# --- the safe-method allow-list ------------------------------------------------------------------


async def asgi_answer(
    app: Starlette, method: str, path: str, headers: Sequence[tuple[str, str]]
) -> tuple[int, bytes]:
    """Call ``app`` as a server does, with the verb exactly as sent (an HTTP client upper-cases)."""
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 443),
    }
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent)
    return status, body


@pytest.fixture
def class_app(harness: Harness) -> Starlette:
    return create_admin_app(
        registry=harness.registry,
        authorizer=harness.authorizer,
        route_providers=[harness.probe],
    )


@pytest.mark.parametrize(
    "method", ["post", "Post", "pOST", "delete", "Delete", "PROPFIND", "patch"]
)
def test_any_verb_but_a_safe_one_is_mutating_in_any_case(class_app: Starlette, method: str) -> None:
    # Without Sec-Fetch-Site: the same-origin rule applies, whatever the case of the verb.
    status, body = asyncio.run(
        asgi_answer(class_app, method, "/class-based", list(IDENTITY.items()))
    )

    assert status == 403, body
    assert json.loads(body)["error"]["code"] == "cross_origin"


@pytest.mark.parametrize("method", ["post", "Post", "delete", "PROPFIND"])
def test_a_same_origin_verb_that_is_not_safe_still_needs_its_audit_entry(
    class_app: Starlette, method: str
) -> None:
    headers = [*IDENTITY.items(), *SAME_ORIGIN.items()]

    status, body = asyncio.run(asgi_answer(class_app, method, "/class-based", headers))

    assert status in (500, 405), body  # 405: no handler. 500: the handler wrote no audit entry.
    assert b"true" not in body


@pytest.mark.parametrize("method", ["GET", "get", "Get", "HEAD", "head", "OPTIONS", "options"])
def test_safe_verbs_in_any_case_need_no_sec_fetch_site(class_app: Starlette, method: str) -> None:
    status, _ = asyncio.run(asgi_answer(class_app, method, "/class-based", list(IDENTITY.items())))

    assert status != 403


def test_an_unknown_verb_through_the_client_is_403_without_same_origin(harness: Harness) -> None:
    response = harness.request("PROPFIND", "/silent", same_origin=False)

    assert response.status_code == 403
    assert error_of(response)["code"] == "cross_origin"


# --- no trailing-slash redirect ------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", *MUTATING_METHODS])
def test_a_trailing_slash_is_a_plain_404_with_the_error_body(
    harness: Harness, method: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        response = harness.request(method, "/mutate/sales/", follow_redirects=False)

    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"
    assert "location" not in response.headers
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_a_trailing_slash_does_not_echo_the_host_header(harness: Harness) -> None:
    response = harness.get("/whoami/", headers={"Host": "evil.example"}, follow_redirects=False)

    assert response.status_code == 404
    assert "evil.example" not in str(response.headers) + response.text


# --- websocket and lifespan ------------------------------------------------------------


def test_a_websocket_is_refused_before_it_reaches_any_route(harness: Harness) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        harness.client.websocket_connect(f"{MOUNT}/whoami", headers=IDENTITY),
    ):
        pass


def test_the_guard_closes_a_websocket_scope_without_calling_the_app() -> None:
    reached: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope["type"])

    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(
        _AdminGuard(inner)({"type": "websocket", "path": "/x", "headers": []}, receive, send)
    )

    assert reached == []
    assert [message["type"] for message in sent] == ["websocket.close"]


def test_the_lifespan_passes_through_the_guard() -> None:
    admin = create_admin_app(registry=StubRegistry(), authorizer=RecordingAuthorizer())

    # Entering the client runs startup and shutdown through the guard; a guard that answered the
    # lifespan scope like a websocket would fail here.
    with TestClient(admin) as client:
        assert client.get("/anything", headers=IDENTITY).status_code == 404
