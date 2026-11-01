"""The request inbox, end to end, against a running appmux in a Databricks container (T148, US8).

Four scenes of user story 8, each through the admin API only:

1. **A new app.** Grace asks for an app; a platform admin approves it. The app exists, is owned by
   Grace, is stopped and has no release (FR-040, FR-042). Before the approval nothing exists: a
   request grants nothing (FR-045).
2. **An OBO delegation.** Carol owns an app and asks for ``obo``. The flag stays off until the
   platform admin approves, then it is on (FR-041, FR-045).
3. **A taken slug.** Grace asks for a slug that is free; before the admin presses Approve somebody
   registers it. Approving closes the request by itself as ``denied``, under the admin who pressed
   the button, with the note ``slug '<slug>' is already taken (automatic)`` (FR-043, design 9.4).
4. **A rename of a running app.** Grace's app is running; she asks for a new slug and the admin
   approves. The app restarts under the new prefix: the new URL answers, the old one is ``404``
   (FR-044, design 9.4 step 4).

The test talks to the edge on the container's own port and plays the Databricks proxy by sending the
identity headers itself, for the platform admin and for the placeholder people (Grace and Carol),
which only works from inside the container. The admin's own token is sent on the calls that need it
(approve and deny write under the admin's role; Update reads the workspace) and nowhere else, and no
answer may repeat it. It does not run in the default gate: it is marked ``integration`` and skipped
unless ``APPMUX_TEST_CONTAINER=1``; ``scripts/sync-deploy.sh`` puts the code on a container. Nothing
here has run against one, so no claim that it passes exists, and a scene whose route is not mounted
yet fails with the status it got.

Everything the run registers is deleted afterwards; a failure to delete is a warning that names the
app to remove. Slugs carry a random suffix so two runs, and the leftovers of a failed one, do not
meet.

Set-up the run needs (it fails loudly when one is missing, instead of passing on nothing):

* ``APPMUX_TEST_BASE_URL``: where the edge listens, for example ``http://127.0.0.1:8000``;
* ``APPMUX_TEST_ADMIN_EMAIL``: a platform admin;
* ``APPMUX_TEST_USER_TOKEN``: that person's Databricks token;
* ``APPMUX_TEST_FIXTURES_FOLDER``: a workspace folder starting with ``/Workspace/`` that holds a
  copy of ``tests/fixtures/`` (the ``light_fastapi`` fixture is the app).
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

import httpx
import pytest
from ops_session import (
    API,
    REQUEST_TIMEOUT_S,
    AdminSession,
    identity_headers,
    load_target,
    register_body,
)

from appmux.admin.schemas import CreatePermissionRequest, DecidePermissionRequest

pytestmark = pytest.mark.integration

GRACE: Final = "grace.requests@example.com"
CAROL: Final = "carol.requests@example.com"
SLUG_SUFFIX_BYTES: Final = 3
BUDGET_MB: Final = 128
NEW_PREFIX_WAIT_S: Final = 180.0
POLL_INTERVAL_S: Final = 1.0
TAKEN_NOTE_FRAGMENT: Final = "is already taken (automatic)"


def create_body(kind: str, app: str, payload: dict[str, Any], reason: str) -> dict[str, Any]:
    body = CreatePermissionRequest.model_validate(
        {"kind": kind, "app": app, "payload": payload, "reason": reason}
    )
    return body.model_dump(mode="json")


def register_request_body(*, slug: str, folder: str) -> dict[str, Any]:
    payload = {
        "slug": slug,
        "display_name": f"Requests {slug}",
        "source_path": folder,
        "memory_budget_mb": BUDGET_MB,
    }
    return create_body("register", slug, payload, "the walkthrough asks for a new app")


def decision_body(*, reason: str | None = None, note: str | None = None) -> dict[str, Any]:
    return DecidePermissionRequest(reason=reason, note=note).model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class Slugs:
    """The apps of one run."""

    asked: str
    delegated: str
    taken: str
    running: str
    renamed: str

    @classmethod
    def fresh(cls) -> Slugs:
        suffix = secrets.token_hex(SLUG_SUFFIX_BYTES)
        return cls(
            asked=f"req-asked-{suffix}",
            delegated=f"req-obo-{suffix}",
            taken=f"req-taken-{suffix}",
            running=f"req-run-{suffix}",
            renamed=f"req-moved-{suffix}",
        )


class Inbox:
    """The calls of the scenes: the admin through ``AdminSession``, the others by their address."""

    def __init__(self, client: httpx.Client, admin: AdminSession) -> None:
        self._client = client
        self.admin = admin
        self.folder = f"{admin.target.fixtures_folder}/light_fastapi"

    def as_person(
        self,
        person: str,
        method: str,
        path: str,
        *,
        json: object | None = None,
        expect: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        headers = identity_headers(self.admin.target, person=person, mutating=method != "GET")
        response = self._client.request(method, f"{API}{path}", json=json, headers=headers)
        assert response.status_code in expect, (
            f"{method} {path} as {person}: expected {list(expect)}, got {response.status_code}"
        )
        return response

    def ask(self, person: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.as_person(person, "POST", "/requests", json=body, expect=(200, 201))
        row: dict[str, Any] = response.json()
        assert row["status"] == "pending"
        return row

    def answer(
        self, row: dict[str, Any], *, verb: str = "approve", reason: str | None = None
    ) -> dict[str, Any]:
        response = self.admin.call(
            "POST",
            f"/requests/{row['request_id']}/{verb}",
            json=decision_body(reason=reason, note="the walkthrough answers"),
            with_token=True,
        )
        answered: dict[str, Any] = response.json()
        return answered

    def register_for(self, slug: str, owner: str) -> dict[str, Any]:
        body = register_body(slug=slug, name=f"Requests {slug}", folder=self.folder, owner=owner)
        response = self.admin.call("POST", "/apps", json=body, expect=(200, 201))
        self.admin.created_apps.append(slug)
        detail: dict[str, Any] = response.json()
        return detail

    def app_of(self, person: str, slug: str) -> dict[str, Any]:
        detail: dict[str, Any] = self.as_person(person, "GET", f"/apps/{slug}").json()
        return detail

    def open_as(self, person: str, slug: str) -> httpx.Response:
        return self._client.get(
            f"/{slug}/", headers=identity_headers(self.admin.target, person=person)
        )

    def wait_until_open(self, person: str, slug: str) -> None:
        deadline = time.monotonic() + NEW_PREFIX_WAIT_S
        while True:
            status = self.open_as(person, slug).status_code
            if status == 200:
                return
            assert time.monotonic() < deadline, (
                f"/{slug}/ still answers {status} after {NEW_PREFIX_WAIT_S:g} s, expected 200"
            )
            time.sleep(POLL_INTERVAL_S)

    def audit_actions_after(self, last_id: int) -> list[str]:
        rows = self.admin.read("/audit?limit=1000")["items"]
        return [row["action"] for row in reversed(rows) if row["id"] > last_id]

    def last_audit_id(self) -> int:
        rows = self.admin.read("/audit?limit=1")["items"]
        return int(rows[0]["id"]) if rows else 0


@pytest.fixture(scope="module")
def inbox() -> Iterator[Inbox]:
    target = load_target()
    with httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as client:
        session = AdminSession(client, target)
        try:
            yield Inbox(client, session)
        finally:
            session.delete_created_apps()


@pytest.fixture(scope="module")
def slugs() -> Slugs:
    return Slugs.fresh()


def test_an_approved_new_app_exists_owned_by_the_asker_stopped_and_without_a_release(
    inbox: Inbox, slugs: Slugs
) -> None:
    row = inbox.ask(GRACE, register_request_body(slug=slugs.asked, folder=inbox.folder))

    # A request grants nothing: no app yet, and the asker sees the pending row.
    inbox.as_person(GRACE, "GET", f"/apps/{slugs.asked}", expect=(404,))
    listed = inbox.as_person(GRACE, "GET", "/requests").json()["items"]
    assert [item["status"] for item in listed if item["request_id"] == row["request_id"]] == [
        "pending"
    ]

    approved = inbox.answer(row, reason="the walkthrough approves the new app")

    inbox.admin.created_apps.append(slugs.asked)
    assert approved["status"] == "approved" and approved["app_version"] is not None
    detail = inbox.app_of(GRACE, slugs.asked)
    assert (detail["owner"], detail["state"], detail["releases"]) == (GRACE, "stopped", [])


def test_carols_obo_request_changes_nothing_until_the_admin_approves_it(
    inbox: Inbox, slugs: Slugs
) -> None:
    inbox.register_for(slugs.delegated, CAROL)
    assert inbox.app_of(CAROL, slugs.delegated)["delegation"]["obo_delegated"] is False

    row = inbox.ask(
        CAROL,
        create_body("delegation", slugs.delegated, {"obo": True}, "needs the visitor's data"),
    )
    assert inbox.app_of(CAROL, slugs.delegated)["delegation"]["obo_delegated"] is False

    answered = inbox.answer(row, reason="the walkthrough allows OBO")

    assert answered["status"] == "approved"
    assert inbox.app_of(CAROL, slugs.delegated)["delegation"]["obo_delegated"] is True


def test_a_slug_taken_before_the_approval_closes_the_request_as_denied_by_itself(
    inbox: Inbox, slugs: Slugs
) -> None:
    last_audit_id = inbox.last_audit_id()
    row = inbox.ask(GRACE, register_request_body(slug=slugs.taken, folder=inbox.folder))
    inbox.register_for(slugs.taken, inbox.admin.admin)

    closed = inbox.answer(row)

    assert closed["status"] == "denied"
    assert closed["closed_by"] == inbox.admin.admin
    assert TAKEN_NOTE_FRAGMENT in (closed["closed_note"] or "")
    assert f"'{slugs.taken}'" in (closed["closed_note"] or "")
    assert inbox.app_of(inbox.admin.admin, slugs.taken)["owner"] == inbox.admin.admin
    assert "request.deny" in inbox.audit_actions_after(last_audit_id)


def test_a_running_app_renamed_by_request_restarts_under_the_new_prefix(
    inbox: Inbox, slugs: Slugs
) -> None:
    admin = inbox.admin
    inbox.register_for(slugs.running, GRACE)
    first = admin.press_update(slugs.running)
    admin.wait_for_release(slugs.running, first, "active")
    admin.wait_for_state(slugs.running, "running")
    assert inbox.open_as(GRACE, slugs.running).status_code == 200
    last_audit_id = inbox.last_audit_id()

    row = inbox.ask(
        GRACE,
        create_body("settings", slugs.running, {"slug": slugs.renamed}, "a better address"),
    )
    answered = inbox.answer(row, reason="the walkthrough approves the rename")
    admin.created_apps.remove(slugs.running)
    admin.created_apps.append(slugs.renamed)

    assert answered["status"] == "approved"
    inbox.as_person(GRACE, "GET", f"/apps/{slugs.running}", expect=(404,))
    inbox.wait_until_open(GRACE, slugs.renamed)
    assert inbox.open_as(GRACE, slugs.running).status_code == 404
    assert inbox.app_of(GRACE, slugs.renamed)["slug"] == slugs.renamed
    actions = inbox.audit_actions_after(last_audit_id)
    assert "request.approve" in actions and "app.rename" in actions
