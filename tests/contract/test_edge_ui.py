"""The admin UI behind the edge application (T170, design 7.3, contracts/edge-http.md).

The unit under test is ``create_edge_app`` given a UI application: ``/`` and ``/_appmux/...`` come
from the static export, ``/_appmux/api/v1`` stays the admin API, and the self-check gate treats the
UI like the rest of ``/_appmux/`` (it stays reachable while the result is failed, so a platform
admin can read it). The export is a tiny one written to ``tmp_path``; the server itself is covered
by ``tests/unit/test_ui_static.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

import pytest
from edge_harness import ALICE, MOUNT, FakeGroups, call, canary_text, seeded_registry

from appmux import __main__ as entry
from appmux.edge.app import create_edge_app
from appmux.edge.gate import ASGIApplication
from appmux.edge.ui_static import create_ui_app, load_ui_export, unavailable_ui_app
from appmux.sandbox.self_check import SelfCheckState

pytestmark = pytest.mark.anyio

LAUNCHER_MARKER: Final = "launcher-page-marker"
APPS_MARKER: Final = "apps-page-marker"
NOT_FOUND_MARKER: Final = "not-found-page-marker"
INLINE_SCRIPT: Final = "self.__next_f=[]"


def page(marker: str) -> str:
    return (
        f"<!doctype html><html><body><p>{marker}</p><script>{INLINE_SCRIPT}</script></body></html>"
    )


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    (tmp_path / "apps").mkdir()
    (tmp_path / "index.html").write_text(page(LAUNCHER_MARKER))
    (tmp_path / "apps" / "index.html").write_text(page(APPS_MARKER))
    (tmp_path / "404.html").write_text(page(NOT_FOUND_MARKER))
    return tmp_path


def edge_with_ui(export_dir: Path, state: SelfCheckState | None = None) -> ASGIApplication:
    return create_edge_app(
        registry=seeded_registry(),
        group_source=FakeGroups(),
        self_check=state,
        ui_app=create_ui_app(load_ui_export(export_dir)),
    )


async def test_the_launcher_is_served_at_the_root(export_dir: Path) -> None:
    reply = await call(edge_with_ui(export_dir), "GET", "/")

    assert reply.status == 200
    assert LAUNCHER_MARKER in reply.text
    assert "script-src 'self' 'sha256-" in reply.headers["content-security-policy"]


async def test_a_page_of_the_ui_is_served_below_the_prefix(export_dir: Path) -> None:
    reply = await call(edge_with_ui(export_dir), "GET", "/_appmux/apps/")

    assert reply.status == 200
    assert APPS_MARKER in reply.text


async def test_the_prefix_without_its_slash_is_the_ui_and_not_a_404_of_the_edge(
    export_dir: Path,
) -> None:
    reply = await call(edge_with_ui(export_dir), "GET", "/_appmux")

    assert reply.status == 308
    assert reply.headers["location"] == "/_appmux/"


async def test_an_unknown_ui_path_gets_the_export_404_page(export_dir: Path) -> None:
    reply = await call(edge_with_ui(export_dir), "GET", "/_appmux/nothing-here/")

    assert reply.status == 404
    assert NOT_FOUND_MARKER in reply.text


async def test_the_admin_api_keeps_its_own_mount(export_dir: Path) -> None:
    reply = await call(edge_with_ui(export_dir), "GET", f"{MOUNT}/me", email=ALICE)

    assert reply.status == 200
    assert reply.json()["email"] == ALICE
    assert LAUNCHER_MARKER not in reply.text


async def test_a_path_below_the_api_mount_that_the_api_does_not_know_is_not_the_ui(
    export_dir: Path,
) -> None:
    reply = await call(edge_with_ui(export_dir), "GET", f"{MOUNT}/no-such-route", email=ALICE)

    assert reply.status == 404
    assert NOT_FOUND_MARKER not in reply.text


async def test_only_get_and_head_reach_the_ui(export_dir: Path) -> None:
    reply = await call(edge_with_ui(export_dir), "POST", "/_appmux/apps/")

    assert reply.status == 405


async def test_a_path_that_is_neither_the_ui_nor_the_api_is_still_a_404_of_the_edge(
    export_dir: Path,
) -> None:
    reply = await call(edge_with_ui(export_dir), "GET", "/sales/")

    assert reply.status == 404
    assert NOT_FOUND_MARKER not in reply.text
    assert LAUNCHER_MARKER not in reply.text


async def test_without_a_ui_application_the_root_and_the_prefix_stay_404() -> None:
    app = create_edge_app(registry=seeded_registry(), group_source=FakeGroups())

    assert (await call(app, "GET", "/")).status == 404
    assert (await call(app, "GET", "/_appmux/")).status == 404


async def test_a_failed_self_check_keeps_the_ui_reachable_and_closes_the_launcher_path(
    export_dir: Path,
) -> None:
    state = SelfCheckState(_failing_canary)
    await state.run()
    app = edge_with_ui(export_dir, state)

    assert (await call(app, "GET", "/_appmux/apps/")).status == 200
    assert (await call(app, "GET", "/")).status == 503


async def test_the_unavailable_ui_answers_503_and_names_no_path() -> None:
    app = create_edge_app(
        registry=seeded_registry(), group_source=FakeGroups(), ui_app=unavailable_ui_app()
    )

    for path in ("/", "/_appmux/", "/_appmux/apps/"):
        reply = await call(app, "GET", path)
        assert reply.status == 503
        assert reply.headers["cache-control"] == "no-store"
        assert "web/out" not in reply.text
    # The admin API is not part of the UI and keeps working.
    assert (await call(app, "GET", f"{MOUNT}/me", email=ALICE)).status == 200


async def _failing_canary() -> str:
    return canary_text(failing={"pid_isolation": "saw a process"})


async def test_start_up_serves_the_export_it_finds(export_dir: Path) -> None:
    app = create_edge_app(
        registry=seeded_registry(),
        group_source=FakeGroups(),
        ui_app=entry._ui_application(export_dir),
    )

    assert LAUNCHER_MARKER in (await call(app, "GET", "/")).text


async def test_start_up_with_no_export_serves_the_stand_in_and_logs_the_cause(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = tmp_path / "no-such-export"

    with caplog.at_level(logging.ERROR, logger="appmux.__main__"):
        ui_app = entry._ui_application(missing)

    app = create_edge_app(registry=seeded_registry(), group_source=FakeGroups(), ui_app=ui_app)
    reply = await call(app, "GET", "/_appmux/")
    assert reply.status == 503
    assert str(missing) not in reply.text
    assert str(missing) in caplog.text


async def test_start_up_with_an_export_the_policy_cannot_serve_serves_the_stand_in(
    export_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (export_dir / "apps" / "index.html").write_text('<html><body style="color:red"></body></html>')

    with caplog.at_level(logging.ERROR, logger="appmux.__main__"):
        ui_app = entry._ui_application(export_dir)

    app = create_edge_app(registry=seeded_registry(), group_source=FakeGroups(), ui_app=ui_app)
    assert (await call(app, "GET", "/")).status == 503
    assert "apps/index.html" in caplog.text
