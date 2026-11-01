"""Host a child safely, against a running appmux in a Databricks container (T096, US1, FR-002).

The five acceptance scenarios of user story 1 and the two routing fixtures, driven through the
admin API and the edge the way a platform admin and a visitor do:

1. an app registered with no release answers ``503`` and no child receives the request;
2. Update on ``light_fastapi`` ends ``active`` and only then does ``/<slug>/`` answer, with the
   child's own body; a request sent before the status read never gets a ``200`` while the release
   is not ``active`` (the route opens after the health check, constitution II);
3. ``startup_crash``, ``always_500`` and ``slow_start`` past its deadline end ``failed`` with the
   reason and a log tail stored, and their route never opens;
4. a folder the presser cannot read is refused with a message and creates no release;
5. a second Update of an unchanged folder reports "already up to date" and creates no release;
   ``slow_start`` within its deadline ends ``active``.

``absolute_links`` and ``base_path_aware`` are asserted on what the child saw: the stray
``/static/x.css`` request never reached ``absolute_links`` (its ``hits`` counter), and
``base_path_aware`` was told its prefix (``base_path`` and ``source``).

It does not run in the default gate: every test here is marked ``integration`` and skipped unless
``APPMUX_TEST_CONTAINER=1``. The identity of the caller comes from ``ops_session.py``, which has two
modes: against the deployed App (``scripts/run-remote-tests.sh``, ``APPMUX_TEST_REMOTE=1``) the real
proxy is in front of appmux and the test sends the owner's bearer token; inside the container it
plays the proxy and sends the identity headers itself. It passed 10 of 10 on 2026-10-31 against
the Free Edition dev App, run from the dev machine.

Set-up the run needs (it fails loudly when one is missing, instead of passing on nothing):

* ``APPMUX_TEST_BASE_URL``: the App's ``https`` address (remote), or where the edge listens, for
  example ``http://127.0.0.1:8000`` (inside the container);
* ``APPMUX_TEST_ADMIN_EMAIL`` (inside the container only): a platform admin; the test registers
  every app with this person as its owner and sends every request as them. A remote run reads the
  admin from ``GET /me`` instead;
* ``APPMUX_TEST_USER_TOKEN``: that person's own Databricks token, which Update reads the folder
  with (it is sent only on ``POST .../releases`` inside the container, and is never printed);
* ``APPMUX_TEST_FIXTURES_FOLDER``: a workspace folder, starting with ``/Workspace/``, holding a
  copy of ``tests/fixtures/`` (one sub-folder per fixture) that the person above can read.

Every app the test registers is deleted afterwards; a deletion that fails is reported as a warning
naming the slug, so an operator can remove it.
"""

from __future__ import annotations

import secrets
import time
import warnings
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
from ops_session import Target, identity_headers, load_target

pytestmark = pytest.mark.integration

API: Final = "/_appmux/api/v1"
REQUEST_TIMEOUT_S: Final = 30.0
MEMORY_BUDGET_MB: Final = 128
POLL_INTERVAL_S: Final = 1.0
# A release spends its time in the build, the sandbox start and the health deadline (60 s default).
RELEASE_TIMEOUT_S: Final = 240.0
# The route opens a moment after the registry says ``active`` (registry first, route second).
ROUTE_OPENS_WITHIN_S: Final = 10.0
SHORT_DEADLINE_S: Final = 5
TERMINAL_STATUSES: Final = frozenset({"active", "failed"})
SLUG_SUFFIX_BYTES: Final = 3


@pytest.fixture(scope="module")
def target() -> Target:
    return load_target()


class Appmux:
    """The calls of this test: register, update, read a release, ask the child, clean up."""

    def __init__(self, client: httpx.Client, target: Target) -> None:
        self._client = client
        self._target = target
        self._registered: list[str] = []

    # --- the admin API ---------------------------------------------------------------------

    def _identity(self) -> dict[str, str]:
        return identity_headers(self._target)

    def _write_headers(self) -> dict[str, str]:
        return identity_headers(self._target, mutating=True)

    def register(self, fixture: str, *, source_path: str | None = None) -> str:
        slug = f"it-{fixture.replace('_', '-')[:14]}-{secrets.token_hex(SLUG_SUFFIX_BYTES)}"
        response = self._client.post(
            f"{API}/apps",
            json={
                "slug": slug,
                "display_name": f"Integration {fixture}",
                "source_path": source_path or f"{self._target.fixtures_folder}/{fixture}",
                "memory_budget_mb": MEMORY_BUDGET_MB,
                "owner": self._target.admin_email,
            },
            headers=self._write_headers(),
        )
        assert response.status_code in {200, 201}, f"registering {fixture}: {response.text}"
        self._registered.append(slug)
        return slug

    def app(self, slug: str) -> dict[str, Any]:
        response = self._client.get(f"{API}/apps/{slug}", headers=self._identity())
        assert response.status_code == 200, f"reading {slug}: {response.status_code}"
        body: dict[str, Any] = response.json()
        return body

    def release_count(self, slug: str) -> int:
        return len(self.app(slug)["releases"])

    def set_health_deadline(self, slug: str, deadline_s: int) -> None:
        """Change only the health deadline, which belongs to the app and creates no release."""
        config = self.app(slug)["config"]
        response = self._client.put(
            f"{API}/apps/{slug}/config",
            json={
                "version": config["version"],
                "env": config["env"],
                "wheel_sources": config["wheel_sources"],
                "health_path": config["health_path"],
                "health_deadline_s": deadline_s,
                "own_sp_client_id_key": config["own_sp_client_id_key"],
                "own_sp_client_secret_key": config["own_sp_client_secret_key"],
            },
            headers=self._write_headers(),
        )
        assert response.status_code == 200, f"setting the deadline of {slug}: {response.text}"

    def update(self, slug: str) -> httpx.Response:
        """Press Update with the person's own token (the one place the token is sent)."""
        headers = identity_headers(self._target, with_token=True, mutating=True)
        return self._client.post(f"{API}/apps/{slug}/releases", headers=headers)

    def release(self, slug: str, release_no: int) -> dict[str, Any]:
        response = self._client.get(
            f"{API}/apps/{slug}/releases/{release_no}", headers=self._identity()
        )
        assert response.status_code == 200, f"reading release {release_no} of {slug}"
        body: dict[str, Any] = response.json()
        return body

    # --- the edge --------------------------------------------------------------------------

    def child(self, slug: str, path: str = "/") -> httpx.Response:
        return self._client.get(f"/{slug}{path}", headers=self._identity())

    def origin(self, path: str) -> httpx.Response:
        return self._client.get(path, headers=self._identity())

    # --- waiting ---------------------------------------------------------------------------

    def release_after_update(self, slug: str) -> int:
        response = self.update(slug)
        assert response.status_code == 202, (
            f"Update of {slug}: {response.status_code} {response.text}"
        )
        body = response.json()
        assert body["status"] == "pending"
        return int(body["release_no"])

    def watch_release(self, slug: str, release_no: int) -> dict[str, Any]:
        """Poll until the release is ``active`` or ``failed`` and check the route on the way.

        The request goes first and the status is read second: a ``200`` means the route was open
        when it was sent, and the route opens only after the registry says ``active``, so the
        status read right after must already be ``active``.
        """
        deadline = time.monotonic() + RELEASE_TIMEOUT_S
        while True:
            answered = self.child(slug).status_code
            detail = self.release(slug, release_no)
            assert not (answered == 200 and detail["status"] != "active"), (
                f"/{slug}/ answered 200 while release {release_no} was {detail['status']}: "
                "the route opened before the health check passed"
            )
            if detail["status"] in TERMINAL_STATUSES:
                return detail
            assert time.monotonic() < deadline, (
                f"release {release_no} of {slug} is still {detail['status']} "
                f"after {RELEASE_TIMEOUT_S:.0f} s"
            )
            time.sleep(POLL_INTERVAL_S)

    def serves_within(self, slug: str, path: str = "/") -> httpx.Response:
        """The child's answer once the route is open, which follows ``active`` by a moment."""
        deadline = time.monotonic() + ROUTE_OPENS_WITHIN_S
        while True:
            response = self.child(slug, path)
            if response.status_code != 503 or time.monotonic() >= deadline:
                return response
            time.sleep(0.2)

    # --- cleanup ---------------------------------------------------------------------------

    def delete_registered(self) -> None:
        for slug in self._registered:
            response = self._client.delete(f"{API}/apps/{slug}", headers=self._write_headers())
            if response.status_code != 204:
                warnings.warn(
                    f"could not delete the test app {slug} (status {response.status_code}); "
                    "remove it by hand",
                    stacklevel=2,
                )


@pytest.fixture
def appmux(target: Target) -> Iterator[Appmux]:
    with httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as client:
        session = Appmux(client, target)
        try:
            yield session
        finally:
            session.delete_registered()


def _assert_failed_and_never_routed(appmux: Appmux, slug: str, detail: dict[str, Any]) -> None:
    assert detail["status"] == "failed", f"the release ended {detail['status']}"
    assert detail["status_reason"], "a failed release states why"
    assert appmux.child(slug).status_code == 503, "the route of a failed release stayed closed"


# --- scenarios 1 and 2: no route before a release, a route only after health ------------------


def test_an_app_with_no_release_receives_no_request(appmux: Appmux) -> None:
    slug = appmux.register("light_fastapi")

    answer = appmux.child(slug)

    assert answer.status_code == 503
    assert "light_fastapi" not in answer.text, "no child answered"
    assert appmux.release_count(slug) == 0


def test_update_opens_the_route_only_after_the_release_is_active(appmux: Appmux) -> None:
    slug = appmux.register("light_fastapi")
    release_no = appmux.release_after_update(slug)

    detail = appmux.watch_release(slug, release_no)

    assert detail["status"] == "active", detail["status_reason"]
    answer = appmux.serves_within(slug)
    assert answer.status_code == 200
    assert answer.json()["fixture"] == "light_fastapi"


# --- scenarios 3: a release that never turns healthy never gets a route -----------------------


def test_a_child_that_exits_at_start_fails_the_release_and_keeps_the_route_closed(
    appmux: Appmux,
) -> None:
    slug = appmux.register("startup_crash")
    release_no = appmux.release_after_update(slug)

    detail = appmux.watch_release(slug, release_no)

    _assert_failed_and_never_routed(appmux, slug, detail)
    assert "status 3" in detail["status_reason"]
    assert "startup_crash: exiting at start with status 3" in (detail["build_log_tail"] or "")


def test_a_child_that_answers_500_fails_the_release_with_the_last_error(appmux: Appmux) -> None:
    slug = appmux.register("always_500")
    appmux.set_health_deadline(slug, SHORT_DEADLINE_S)
    release_no = appmux.release_after_update(slug)

    detail = appmux.watch_release(slug, release_no)

    _assert_failed_and_never_routed(appmux, slug, detail)
    assert detail["status_reason"] == (
        f"health check did not pass within {SHORT_DEADLINE_S} s (last error: status 500)"
    )


def test_a_child_that_binds_its_port_after_the_deadline_fails_the_release(appmux: Appmux) -> None:
    # slow_start waits 10 s by default; the deadline is 5 s.
    slug = appmux.register("slow_start")
    appmux.set_health_deadline(slug, SHORT_DEADLINE_S)
    release_no = appmux.release_after_update(slug)

    detail = appmux.watch_release(slug, release_no)

    _assert_failed_and_never_routed(appmux, slug, detail)
    assert detail["status_reason"] == (
        f"health check did not pass within {SHORT_DEADLINE_S} s (last error: connection refused)"
    )


def test_a_child_that_binds_its_port_within_the_deadline_becomes_active(appmux: Appmux) -> None:
    # slow_start waits 10 s by default; the default deadline is 60 s.
    slug = appmux.register("slow_start")
    release_no = appmux.release_after_update(slug)

    detail = appmux.watch_release(slug, release_no)

    assert detail["status"] == "active", detail["status_reason"]
    report = appmux.serves_within(slug, "/report")
    assert report.status_code == 200
    assert report.json()["start_delay_s"] == pytest.approx(10.0)


# --- scenarios 4 and 5: an Update that changes nothing, or cannot read ------------------------


def test_a_folder_the_person_cannot_read_is_refused_and_creates_no_release(
    appmux: Appmux, target: Target
) -> None:
    missing = f"{target.fixtures_folder}/no-such-folder-{secrets.token_hex(SLUG_SUFFIX_BYTES)}"
    slug = appmux.register("light_fastapi", source_path=missing)

    response = appmux.update(slug)

    assert 400 <= response.status_code < 500, response.text
    assert response.json()["error"]["message"], "the refusal says why"
    assert appmux.release_count(slug) == 0


def test_updating_an_unchanged_folder_reports_up_to_date_and_creates_no_release(
    appmux: Appmux,
) -> None:
    slug = appmux.register("light_fastapi")
    release_no = appmux.release_after_update(slug)
    assert appmux.watch_release(slug, release_no)["status"] == "active"

    again = appmux.update(slug)

    assert again.status_code == 200
    assert again.json() == {"status": "up_to_date", "release_no": release_no}
    assert appmux.release_count(slug) == 1


# --- the routing fixtures: assert what the child saw ------------------------------------------


def test_a_stray_absolute_link_is_refused_by_the_edge_and_never_reaches_the_child(
    appmux: Appmux,
) -> None:
    slug = appmux.register("absolute_links")
    release_no = appmux.release_after_update(slug)
    assert appmux.watch_release(slug, release_no)["status"] == "active"
    assert appmux.serves_within(slug).status_code == 200

    stray = appmux.origin("/static/x.css")
    report = appmux.child(slug, "/report")

    assert stray.status_code == 404
    assert report.status_code == 200
    assert report.json()["hits"].get("/static/x.css", 0) == 0


def test_a_base_path_aware_child_is_told_the_prefix_it_is_served_under(appmux: Appmux) -> None:
    slug = appmux.register("base_path_aware")
    release_no = appmux.release_after_update(slug)
    assert appmux.watch_release(slug, release_no)["status"] == "active"

    report = appmux.serves_within(slug, "/report")

    assert report.status_code == 200
    body = report.json()
    assert body["base_path"] == f"/{slug}"
    assert body["source"] == "x-forwarded-prefix"
