"""What ``create_edge_app`` mounts when it is given the parts of the running service (T095).

The route providers of the release flow, the resources, the source viewer and Open as a user were
built and tested one by one against the admin API alone. These tests pin that the edge application
mounts each one when the start-up gives it its part, and mounts nothing (the plain ``404`` of an
unknown path) when it is not given:

* ``release_updater`` and ``on_release_created`` -> ``POST /apps/{app}/releases`` and the release
  page;
* ``attach_checker`` -> the requirements and the attach and detach routes;
* ``source_store`` -> the read-only source viewer;
* ``workspace_client`` -> the workspace folder browser;
* ``open_mode_cookies`` -> ``POST /apps/{app}/open-mode`` writes the cookie *and* the pipeline reads
  that same cookie to decide whether a child receives an admin's token (one key, one instance);
* ``bootstrap_admins`` is read once and shared: a one-shot iterable still reaches the authorizer
  and ``PUT /platform/admins``.

Nothing opens a TCP port; the child of the open-mode test is a unix-socket server in ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import httpx
import pytest
from edge_harness import (
    ALICE,
    DANA,
    MOUNT,
    NOW,
    VISITOR_TOKEN,
    App,
    FakeGroups,
    Reply,
    app_row,
    call,
    seeded_registry,
)
from pydantic import SecretStr
from stub_registry import StubRegistry

from appmux.edge.app import create_edge_app
from appmux.edge.open_mode import OpenModeCookies
from appmux.identity.actor import Actor
from appmux.reconciler.ports import ChangeWriter, UpdateResult
from appmux.reconciler.route_table import Route, RouteTable
from appmux.registry.protocol import Change
from appmux.registry.resources import AttachProbe, CheckVerdict
from appmux.registry.writers import WriteKind
from appmux.upstream.client import UpstreamClient

pytestmark = pytest.mark.anyio

DIGEST: Final = "sha256:" + "ab" * 32
COOKIE_KEY: Final = b"k" * 32


class FakeUpdater:
    """An Update flow that either finds the folder unchanged or writes one new release."""

    def __init__(self, registry: StubRegistry, *, is_up_to_date: bool) -> None:
        self.registry = registry
        self.is_up_to_date = is_up_to_date

    async def update(
        self, *, actor: Actor, app_slug: str, user_token: SecretStr, write: ChangeWriter
    ) -> UpdateResult:
        if self.is_up_to_date:
            return UpdateResult(release_no=1, is_up_to_date=True)
        existing = self.registry.snapshot.releases_for(app_slug)
        release_no = max((r.release_no for r in existing), default=0) + 1
        row = Change(
            WriteKind.RELEASE_INSERT,
            values={
                "app_slug": app_slug,
                "release_no": release_no,
                "snapshot_digest": DIGEST,
                "detected": {"runtime": "python"},
                "created_by": actor.email,
            },
        )
        await write([row], release_no=release_no)
        return UpdateResult(release_no=release_no, is_up_to_date=False)


class NeverCalledChecker:
    """An attach checker for tests that only look at which routes exist."""

    async def check(self, probe: AttachProbe) -> CheckVerdict:
        raise AssertionError("the attach check must not run in this test")


async def get(app: App, path: str, email: str) -> Reply:
    return await call(app, "GET", f"{MOUNT}{path}", email=email)


# --- the release routes ------------------------------------------------------------------------


async def test_the_release_route_is_served_when_an_updater_is_given() -> None:
    registry = seeded_registry()
    app = create_edge_app(
        registry=registry,
        group_source=FakeGroups(),
        release_updater=FakeUpdater(registry, is_up_to_date=True),
    )

    reply = await call(app, "POST", f"{MOUNT}/apps/sales/releases", email=ALICE)

    assert reply.status == 200
    assert reply.json() == {"status": "up_to_date", "release_no": 1}


async def test_a_new_release_tells_the_reconciler_which_app_to_look_at() -> None:
    registry = seeded_registry()
    woken: list[str] = []
    app = create_edge_app(
        registry=registry,
        group_source=FakeGroups(),
        release_updater=FakeUpdater(registry, is_up_to_date=False),
        on_release_created=woken.append,
    )

    reply = await call(app, "POST", f"{MOUNT}/apps/sales/releases", email=ALICE)

    assert reply.status == 202
    assert woken == ["sales"]


async def test_without_an_updater_the_edge_has_no_release_routes() -> None:
    app = create_edge_app(registry=seeded_registry(), group_source=FakeGroups())

    update = await call(app, "POST", f"{MOUNT}/apps/sales/releases", email=ALICE)
    page = await get(app, "/apps/sales/releases/1", ALICE)

    assert (update.status, page.status) == (404, 404)


# --- the resource routes -----------------------------------------------------------------------


async def test_the_requirements_route_is_served_when_an_attach_checker_is_given() -> None:
    app = create_edge_app(
        registry=seeded_registry(),
        group_source=FakeGroups(),
        attach_checker=NeverCalledChecker(),
    )

    reply = await get(app, "/apps/sales/requirements", ALICE)

    assert reply.status == 200


async def test_without_an_attach_checker_the_edge_has_no_resource_routes() -> None:
    app = create_edge_app(registry=seeded_registry(), group_source=FakeGroups())

    reply = await get(app, "/apps/sales/requirements", ALICE)

    assert reply.status == 404


# --- the source viewer -------------------------------------------------------------------------


class ArchiveStore:
    def __init__(self, archive: bytes) -> None:
        self.archive = archive

    async def get(self, digest: str, destination: Path) -> None:
        await asyncio.to_thread(destination.write_bytes, self.archive)


def archive_of(name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def registry_with_release_seven() -> StubRegistry:
    registry = seeded_registry()
    registry.seed(
        "releases",
        {
            "app_slug": "sales",
            "release_no": 7,
            "snapshot_digest": DIGEST,
            "detected": {},
            "status": "active",
            "status_at": NOW,
            "created_by": ALICE,
            "created_at": NOW,
        },
    )
    return registry


async def test_the_source_viewer_is_served_when_a_snapshot_store_is_given() -> None:
    app = create_edge_app(
        registry=registry_with_release_seven(),
        group_source=FakeGroups(),
        source_store=ArchiveStore(archive_of("app.py", b"print('hi')\n")),
    )

    reply = await get(app, "/apps/sales/releases/7/source", DANA)

    assert reply.status == 200
    assert [item["path"] for item in reply.json()["items"]] == ["app.py"]


async def test_without_a_snapshot_store_the_edge_has_no_source_viewer() -> None:
    app = create_edge_app(registry=registry_with_release_seven(), group_source=FakeGroups())

    reply = await get(app, "/apps/sales/releases/7/source", DANA)

    assert reply.status == 404


# --- the workspace folder browser --------------------------------------------------------------


def workspace_client_listing_one_file() -> httpx.AsyncClient:
    def answer(request: httpx.Request) -> httpx.Response:
        listed = {"objects": [{"path": "/Workspace/Shared/app.yaml", "object_type": "FILE"}]}
        return httpx.Response(200, json=listed)

    return httpx.AsyncClient(
        base_url="https://workspace.example.invalid", transport=httpx.MockTransport(answer)
    )


async def browse_shared(app: App) -> httpx.Response:
    """``GET /workspace/browse?path=/Workspace/Shared`` as Alice, query string included.

    The harness's ``call`` has no query string, so this goes through httpx's ASGI transport.
    """
    headers = {"X-Forwarded-Email": ALICE, "X-Forwarded-Access-Token": VISITOR_TOKEN}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get(
            f"{MOUNT}/workspace/browse", params={"path": "/Workspace/Shared"}, headers=headers
        )


async def test_the_folder_browser_is_served_when_a_workspace_client_is_given() -> None:
    app = create_edge_app(
        registry=seeded_registry(),
        group_source=FakeGroups(),
        workspace_client=workspace_client_listing_one_file(),
    )

    reply = await browse_shared(app)

    assert reply.status_code == 200, reply.text
    assert [entry["name"] for entry in reply.json()["entries"]] == ["app.yaml"]


async def test_without_a_workspace_client_the_edge_has_no_folder_browser() -> None:
    app = create_edge_app(registry=seeded_registry(), group_source=FakeGroups())

    reply = await browse_shared(app)

    assert reply.status_code == 404


# --- Open as a user: one cookie key for the route and the pipeline ----------------------------


class Child:
    """A child that answers every request and keeps the head of each one it received."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.heads: list[dict[str, str]] = []
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")[1:]
                pairs = (line.partition(":") for line in lines if line)
                self.heads.append({name.strip().lower(): value.strip() for name, _, value in pairs})
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def child(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Child]:
    # AF_UNIX paths are limited to about 100 characters, so the directory name stays short.
    instance = Child(tmp_path_factory.mktemp("w") / "sales.sock")
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


def registry_with_an_obo_app() -> StubRegistry:
    registry = seeded_registry()
    registry.seed("apps", {**app_row("sales", owner=ALICE), "obo_delegated": True})
    return registry


def cookie_pair(reply: Reply) -> str:
    return reply.headers["set-cookie"].partition(";")[0]


async def test_the_open_mode_route_writes_the_cookie_with_the_given_key() -> None:
    cookies = OpenModeCookies(key=COOKIE_KEY)
    app = create_edge_app(
        registry=registry_with_an_obo_app(),
        group_source=FakeGroups(),
        open_mode_cookies=cookies,
    )

    reply = await call(
        app, "POST", f"{MOUNT}/apps/sales/open-mode", email=DANA, json_body={"mode": "as_user"}
    )

    assert reply.status == 204
    admin = Actor(email=DANA, visitor_token=None)
    chosen = cookies.chosen_as_user([("cookie", cookie_pair(reply))], admin)
    assert chosen == {"sales"}


async def test_an_admin_gets_no_token_at_the_child_until_the_cookie_they_were_given_comes_back(
    child: Child,
) -> None:
    routes = RouteTable()
    routes.open_route("sales", Route(release_no=1, socket_path=child.path))
    upstream = UpstreamClient()
    app = create_edge_app(
        registry=registry_with_an_obo_app(),
        group_source=FakeGroups(),
        routes=routes,
        upstream_client=upstream,
        open_mode_cookies=OpenModeCookies(key=COOKIE_KEY),
    )
    try:
        before = await call(app, "GET", "/sales/", email=DANA)
        chosen = await call(
            app, "POST", f"{MOUNT}/apps/sales/open-mode", email=DANA, json_body={"mode": "as_user"}
        )
        after = await call(
            app, "GET", "/sales/", email=DANA, headers=[("Cookie", cookie_pair(chosen))]
        )
        owner = await call(app, "GET", "/sales/", email=ALICE)
    finally:
        await upstream.aclose()

    assert [before.status, chosen.status, after.status, owner.status] == [200, 204, 200, 200]
    tokens = [head.get("x-forwarded-access-token") for head in child.heads]
    # The admin without the cookie: none. With the cookie it set: theirs. The owner: always theirs.
    assert tokens == [None, VISITOR_TOKEN, VISITOR_TOKEN]
    assert "cookie" not in child.heads[1], "the open-mode cookie must not reach the child"


# --- the bootstrap admins ----------------------------------------------------------------------


def registry_without_platform_admins() -> StubRegistry:
    registry = StubRegistry(clock=lambda: NOW)
    registry.seed("apps", app_row("sales", owner=ALICE))
    registry.seed(
        "app_config",
        {"app_slug": "sales", "desired_state": "running", "updated_by": DANA, "updated_at": NOW},
    )
    return registry


async def test_a_one_shot_list_of_bootstrap_admins_reaches_every_part_that_needs_it() -> None:
    registry = registry_without_platform_admins()
    one_shot = (email for email in [DANA])
    app = create_edge_app(registry=registry, group_source=FakeGroups(), bootstrap_admins=one_shot)

    # Dana is an admin only through the bootstrap list, and sending that same list is no change.
    reply = await call(
        app,
        "PUT",
        f"{MOUNT}/platform/admins",
        email=DANA,
        json_body={"admins": [{"type": "user", "name": DANA}], "reason": "keep the list"},
    )

    assert reply.status == 200
    assert reply.json() == {"items": [{"type": "user", "name": DANA}]}
    assert registry.writes == ()
