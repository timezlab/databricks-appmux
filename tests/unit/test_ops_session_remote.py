"""How the container tests identify themselves, without a container (the remote run).

``tests/integration/ops_session.py`` is the session the container tests share. Run inside the
container, a test plays the Databricks proxy by sending ``X-Forwarded-Email`` itself. Run from a
developer machine against the deployed App (``APPMUX_TEST_REMOTE=1``), the proxy is the real one:
the test sends the owner's OAuth token as a bearer token and the proxy puts the identity headers
on the request. Forged headers would be dropped by it, and a test must never claim to be someone
else than the token's owner. What can be checked without a network is checked here: the headers of
both modes, how the target is loaded (the admin's address is read from ``GET /me``, so no e-mail is
written in a command line or an environment file), and that no message repeats the token. The file
is loaded by path because the test directories are not packages.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

SESSION = Path(__file__).resolve().parents[1] / "integration" / "ops_session.py"
MODULE_NAME = "ops_session_under_test"

TOKEN = "oauth-token-placeholder"  # noqa: S105 - an obviously fake placeholder
ADMIN = "admin@example.com"
BASE_URL = "https://appmux.example.invalid"
FOLDER = "/Workspace/Users/someone@example.com/fixtures"


def _load_session() -> ModuleType:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, SESSION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def session() -> ModuleType:
    return _load_session()


@pytest.fixture
def remote_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("APPMUX_TEST_ADMIN_EMAIL",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APPMUX_TEST_REMOTE", "1")
    monkeypatch.setenv("APPMUX_TEST_BASE_URL", BASE_URL)
    monkeypatch.setenv("APPMUX_TEST_FIXTURES_FOLDER", FOLDER)
    monkeypatch.setenv("APPMUX_TEST_USER_TOKEN", TOKEN)


def me_answering(
    *, status: int = 200, email: str = ADMIN, is_platform_admin: bool = True
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A transport that answers ``GET /me``, and the requests it was asked."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"code": "nope"}})
        return httpx.Response(
            200, json={"email": email, "groups": [], "is_platform_admin": is_platform_admin}
        )

    return httpx.MockTransport(handler), seen


def remote_target(session: ModuleType, *, is_remote: bool = True) -> object:
    return session.Target(
        base_url=BASE_URL,
        host="appmux.example.invalid",
        port=443,
        admin_email=ADMIN,
        fixtures_folder=FOLDER,
        user_token=TOKEN,
        is_remote=is_remote,
    )


# --- the headers of the two modes ----------------------------------------------------------------


def test_inside_the_container_a_test_plays_the_proxy_and_names_the_admin(
    session: ModuleType,
) -> None:
    headers = session.identity_headers(remote_target(session, is_remote=False))

    assert headers == {"X-Forwarded-Email": ADMIN}


def test_inside_the_container_a_test_may_act_as_another_person_and_send_the_token(
    session: ModuleType,
) -> None:
    headers = session.identity_headers(
        remote_target(session, is_remote=False),
        person="grace@example.com",
        with_token=True,
        mutating=True,
    )

    assert headers == {
        "X-Forwarded-Email": "grace@example.com",
        "X-Forwarded-Access-Token": TOKEN,
        "Sec-Fetch-Site": "same-origin",
    }


def test_against_the_deployed_app_the_test_sends_a_bearer_token_and_forges_nothing(
    session: ModuleType,
) -> None:
    headers = session.identity_headers(remote_target(session), with_token=True, mutating=True)

    assert headers == {"Authorization": f"Bearer {TOKEN}", "Sec-Fetch-Site": "same-origin"}


def test_against_the_deployed_app_the_owner_of_the_token_may_name_themselves(
    session: ModuleType,
) -> None:
    headers = session.identity_headers(remote_target(session), person=ADMIN.upper())

    assert headers == {"Authorization": f"Bearer {TOKEN}"}


def test_against_the_deployed_app_a_test_cannot_act_as_another_person(
    session: ModuleType,
) -> None:
    with pytest.raises(pytest.skip.Exception, match="only as the owner of the bearer token"):
        session.identity_headers(remote_target(session), person="grace@example.com")


def test_a_target_never_shows_the_token(session: ModuleType) -> None:
    assert TOKEN not in repr(remote_target(session))


# --- loading the target --------------------------------------------------------------------------


def test_a_remote_target_learns_the_admin_from_the_app_and_needs_no_email(
    session: ModuleType, remote_environment: None
) -> None:
    transport, seen = me_answering()

    target = session.load_target(transport=transport)

    assert target.is_remote
    assert target.admin_email == ADMIN
    assert target.base_url == BASE_URL
    (request,) = seen
    assert request.url.path == "/_appmux/api/v1/me"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert "X-Forwarded-Email" not in request.headers


def test_a_remote_target_refuses_a_url_that_is_not_https(
    session: ModuleType, remote_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPMUX_TEST_BASE_URL", "http://appmux.example.invalid")

    with pytest.raises(pytest.fail.Exception, match="https"):
        session.load_target(transport=me_answering()[0])


def test_a_token_that_is_not_a_platform_admin_stops_the_run_and_says_what_to_do(
    session: ModuleType, remote_environment: None
) -> None:
    with pytest.raises(pytest.fail.Exception, match="platform admin") as failure:
        session.load_target(transport=me_answering(is_platform_admin=False)[0])

    assert TOKEN not in str(failure.value)


def test_a_refused_token_stops_the_run_with_the_status_and_not_the_token(
    session: ModuleType, remote_environment: None
) -> None:
    with pytest.raises(pytest.fail.Exception, match="401") as failure:
        session.load_target(transport=me_answering(status=401)[0])

    assert TOKEN not in str(failure.value)


def test_a_token_is_required_in_a_remote_run(
    session: ModuleType, remote_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APPMUX_TEST_USER_TOKEN")

    with pytest.raises(pytest.fail.Exception, match="APPMUX_TEST_USER_TOKEN"):
        session.load_target(transport=me_answering()[0])


def test_inside_the_container_the_admin_email_is_still_required(
    session: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APPMUX_TEST_REMOTE", raising=False)
    monkeypatch.delenv("APPMUX_TEST_ADMIN_EMAIL", raising=False)
    monkeypatch.setenv("APPMUX_TEST_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("APPMUX_TEST_FIXTURES_FOLDER", FOLDER)
    monkeypatch.setenv("APPMUX_TEST_USER_TOKEN", TOKEN)

    with pytest.raises(pytest.fail.Exception, match="APPMUX_TEST_ADMIN_EMAIL"):
        session.load_target()


# --- the session ---------------------------------------------------------------------------------


def test_a_remote_session_sends_the_bearer_token_and_checks_the_answer_for_it(
    session: ModuleType,
) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"slug": "demo"})

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    admin = session.AdminSession(client, remote_target(session))

    admin.call("POST", "/apps/demo/releases", expect=(200,), with_token=True)

    (request,) = sent
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Sec-Fetch-Site"] == "same-origin"
    assert "X-Forwarded-Email" not in request.headers
    assert "X-Forwarded-Access-Token" not in request.headers


def test_inside_the_container_registering_an_app_sends_the_token_the_proxy_would_add(
    session: ModuleType,
) -> None:
    # POST /apps writes the platform fields under the person's own token and answers
    # 403 user_token_missing without it. The first container run of test_edge_capacity (2026-11-05)
    # stopped there: the session played the proxy but left the token out of the registration.
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201, json={"slug": "demo"})

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    admin = session.AdminSession(client, remote_target(session, is_remote=False))

    admin.register("demo", fixture="light_fastapi")

    (request,) = sent
    assert request.headers["X-Forwarded-Email"] == ADMIN
    assert request.headers["X-Forwarded-Access-Token"] == TOKEN


def test_inside_the_container_deleting_the_test_apps_sends_the_token_too(
    session: ModuleType,
) -> None:
    # DELETE /apps/{slug} is written under the admin's token as well; without it the cleanup of a
    # container run would only warn and leave the apps it registered behind.
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(204)

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    admin = session.AdminSession(client, remote_target(session, is_remote=False))
    admin.created_apps.append("demo")

    admin.delete_created_apps()

    (request,) = sent
    assert request.method == "DELETE"
    assert request.headers["X-Forwarded-Access-Token"] == TOKEN


def test_a_delete_that_times_out_does_not_stop_the_cleanup_of_the_other_apps(
    session: ModuleType,
) -> None:
    # The first container run of test_requests_walkthrough (2026-11-05): the DELETE of a running,
    # renamed app took longer than the client's timeout; the exception ended the loop and left the
    # three other apps of the run registered.
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path.rsplit("/", 1)[-1])
        if asked[-1] == "slow":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(204)

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    admin = session.AdminSession(client, remote_target(session, is_remote=False))
    admin.created_apps.extend(["first", "slow", "last"])

    with pytest.warns(UserWarning, match="slow") as raised:
        admin.delete_created_apps()

    assert asked == ["last", "slow", "first"]
    assert all("by hand" in str(warning.message) for warning in raised)


def test_a_session_fails_when_an_answer_repeats_the_token(session: ModuleType) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"leaked {TOKEN}")

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    admin = session.AdminSession(client, remote_target(session))

    with pytest.raises(AssertionError, match="repeated the access token"):
        admin.call("GET", "/apps")


# --- a token that outlives one hour ---------------------------------------------------------------

PROFILE = "some-profile"  # a CLI profile name; it must never reach a message
RENEWED_TOKEN = "renewed-token-placeholder"  # noqa: S105 - an obviously fake placeholder
NOW_S = 1_000_000.0
RENEW_MARGIN_S = 600.0


class FakeCli:
    """Stands in for ``databricks auth token``: answers a token that expires ``lifetime_s`` on."""

    def __init__(
        self, tokens: list[str], *, lifetime_s: float = 3600.0, exit_code: int = 0
    ) -> None:
        self.tokens = tokens
        self.lifetime_s = lifetime_s
        self.exit_code = exit_code
        self.asked: list[list[str]] = []
        self.now_s = NOW_S

    def run(self, command: list[str], **_: object) -> object:
        import json
        import subprocess
        from datetime import UTC, datetime

        self.asked.append(command)
        if self.exit_code:
            return subprocess.CompletedProcess(command, self.exit_code, "", f"{PROFILE} failed")
        expiry = datetime.fromtimestamp(self.now_s + self.lifetime_s, tz=UTC).isoformat()
        body = json.dumps({"access_token": self.tokens.pop(0), "expiry": expiry})
        return subprocess.CompletedProcess(command, 0, body, "")


def renewal_with(session: ModuleType, cli: FakeCli) -> object:
    return session.TokenRenewal(PROFILE, TOKEN, run=cli.run, clock=lambda: cli.now_s)


def test_a_token_far_from_its_expiry_is_asked_for_once_and_then_kept(session: ModuleType) -> None:
    cli = FakeCli([TOKEN])
    renewal = renewal_with(session, cli)

    first = renewal.current()
    cli.now_s += 600.0
    second = renewal.current()

    assert (first, second) == (TOKEN, TOKEN)
    assert len(cli.asked) == 1
    assert cli.asked[0][:3] == ["databricks", "auth", "token"]
    assert PROFILE in cli.asked[0]


def test_a_token_close_to_its_expiry_is_asked_for_again_and_the_new_one_is_used(
    session: ModuleType,
) -> None:
    cli = FakeCli([TOKEN, RENEWED_TOKEN])
    renewal = renewal_with(session, cli)
    renewal.current()

    cli.now_s += 3600.0 - RENEW_MARGIN_S + 1.0
    token = renewal.current()

    assert token == RENEWED_TOKEN
    assert len(cli.asked) == 2
    # The CLI hands back its cached token until it is closer to expiry than the margin, so the
    # renewal has to say that the cached one is not wanted.
    assert "--force-refresh" not in cli.asked[0]
    assert "--force-refresh" in cli.asked[1]


def test_a_first_token_that_is_already_near_its_expiry_is_renewed_at_once(
    session: ModuleType,
) -> None:
    cli = FakeCli([TOKEN, RENEWED_TOKEN], lifetime_s=RENEW_MARGIN_S - 1.0)
    renewal = renewal_with(session, cli)

    token = renewal.current()

    assert token == RENEWED_TOKEN
    assert [("--force-refresh" in command) for command in cli.asked] == [False, True]


def test_a_cli_that_fails_stops_the_run_without_the_token_or_the_profile(
    session: ModuleType,
) -> None:
    renewal = renewal_with(session, FakeCli([], exit_code=1))

    with pytest.raises(pytest.fail.Exception, match="log in again") as failure:
        renewal.current()

    assert TOKEN not in str(failure.value)
    assert PROFILE not in str(failure.value)


def test_a_remote_target_sends_the_renewed_token(session: ModuleType) -> None:
    cli = FakeCli([TOKEN, RENEWED_TOKEN])
    target = session.Target(
        base_url=BASE_URL,
        host="appmux.example.invalid",
        port=443,
        admin_email=ADMIN,
        fixtures_folder=FOLDER,
        user_token=TOKEN,
        is_remote=True,
        renewal=renewal_with(session, cli),
    )
    session.identity_headers(target)

    cli.now_s += 3600.0
    headers = session.identity_headers(target)

    assert headers == {"Authorization": f"Bearer {RENEWED_TOKEN}"}


def test_a_remote_target_renews_only_when_a_profile_is_named(
    session: ModuleType, remote_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    without = session.load_target(transport=me_answering()[0])
    monkeypatch.setenv("APPMUX_TEST_TOKEN_PROFILE", PROFILE)
    with_profile = session.load_target(transport=me_answering()[0])

    assert without.renewal is None
    assert with_profile.renewal is not None
    assert PROFILE not in repr(with_profile)
