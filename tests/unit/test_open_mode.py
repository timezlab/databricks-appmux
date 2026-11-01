"""Open as a user: the cookie, the token decision and what the child receives (T150).

FR-028, rule 17. A platform admin holds ``CAN_MANAGE`` on every app without a grant. On an
OBO-delegated app where they are neither the owner nor named by a grant, the edge removes
``X-Forwarded-Access-Token`` (the app still sees who is there from ``X-Forwarded-Email``), unless
they chose **Open as a user** for that app. The choice is kept in the ``__Host-appmux_open``
cookie, which the edge alone sets and removes before forwarding.

What is pinned here, with no network and no registry (the route that writes ``app.view_as_user``
and the cookie header is in ``tests/contract/test_platform_admin_support.py``):

* the cookie: a ``__Host-`` cookie by the browser's rules, per person, and unforgeable. Every child
  runs on the same origin, so its page script can write any cookie the browser lets a script write;
  an unsigned or foreign value therefore means "no choice", never "as a user";
* the decision (``OpenModeTokenPolicy``), as a table: who gets the token on which app;
* the two together through ``rewrite_request_headers``: the headers a child receives.

All emails and tokens are placeholders.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

import pytest
from pydantic import SecretStr

from appmux.admin.authorizer import Access
from appmux.admin.schemas import OpenMode
from appmux.authz.policy import AdminEntry, AppAccess, AuthzData, Grant, Permission
from appmux.edge.open_mode import (
    MAX_CHOICES,
    OpenModeCookies,
    OpenModeTokenPolicy,
)
from appmux.identity.actor import Actor
from appmux.registry.snapshot import SNAPSHOT_TABLES, RegistrySnapshot, build_snapshot
from appmux.rewrite.request import rewrite_request_headers
from appmux.rewrite.response import OPEN_MODE_COOKIE_NAME

pytestmark = pytest.mark.anyio

ALICE: Final = "alice@example.com"
BOB: Final = "bob@example.com"
CAROL: Final = "carol@example.com"
DANA: Final = "dana@example.com"
ERIN: Final = "erin@example.com"
FRANK: Final = "frank@example.com"
HEIDI: Final = "heidi@example.com"
TOKEN: Final = "tok-test-open-mode"  # noqa: S105 - a fake value for a test
KEY: Final = b"k" * 32
NOW: Final = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


def actor_of(email: str, *, token: str | None = TOKEN) -> Actor:
    return Actor(email=email, visitor_token=SecretStr(token) if token is not None else None)


def empty_snapshot() -> RegistrySnapshot:
    return build_snapshot({table: [] for table in SNAPSHOT_TABLES}, loaded_at=NOW)


# sales: OBO, alice owns it, bob has CAN_USE. hr: OBO, dana (a platform admin) is named by a grant.
# docs: not OBO. team: OBO, shared with a group only. dana, heidi and the group data-platform are
# platform admins; erin is nobody.
AUTHZ_DATA: Final = AuthzData(
    apps={
        "sales": AppAccess(
            "sales", ALICE, grants=(Grant("user", BOB, Permission.CAN_USE),), obo_delegated=True
        ),
        "hr": AppAccess(
            "hr", ALICE, grants=(Grant("user", DANA, Permission.CAN_USE),), obo_delegated=True
        ),
        "docs": AppAccess("docs", FRANK, obo_delegated=False),
        "team": AppAccess(
            "team",
            ALICE,
            grants=(Grant("group", "sales-team", Permission.CAN_USE),),
            obo_delegated=True,
        ),
    },
    platform_admins=(
        AdminEntry("user", DANA),
        AdminEntry("user", HEIDI),
        AdminEntry("group", "data-platform"),
    ),
    user_authorization=True,
)


class FakeAccess:
    """The ``AccessSource`` of the policy: fixed data, the groups of a person, or a failure."""

    def __init__(self, groups: dict[str, frozenset[str] | None] | None = None) -> None:
        self.groups = groups or {}
        self.is_broken = False

    async def access_for(self, actor: Actor) -> Access:
        if self.is_broken:
            raise RuntimeError("the registry has not been loaded")
        return Access(
            snapshot=empty_snapshot(),
            data=AUTHZ_DATA,
            groups=self.groups.get(actor.email, frozenset()),
        )


@pytest.fixture
def cookies() -> OpenModeCookies:
    return OpenModeCookies(key=KEY)


@pytest.fixture
def access() -> FakeAccess:
    return FakeAccess()


@pytest.fixture
def policy(cookies: OpenModeCookies, access: FakeAccess) -> OpenModeTokenPolicy:
    return OpenModeTokenPolicy(access, cookies)


def cookie_pair(set_cookie: str) -> str:
    """The ``name=value`` a browser sends back for a ``Set-Cookie`` line."""
    return set_cookie.split(";")[0]


def switched(
    cookies: OpenModeCookies,
    actor: Actor,
    slug: str,
    mode: OpenMode,
    *,
    sent: Sequence[str] = (),
) -> str:
    """The ``Set-Cookie`` the edge answers to a switch, for a browser that already sent ``sent``."""
    return cookies.switched([("Cookie", pair) for pair in sent], actor, slug, mode)


def choice_of(cookies: OpenModeCookies, actor: Actor, *pairs: str) -> frozenset[str]:
    return cookies.chosen_as_user([("Cookie", pair) for pair in pairs], actor)


# --- the cookie -----------------------------------------------------------------------------


def test_the_cookie_follows_the_host_prefix_rules_and_is_not_readable_by_a_script(
    cookies: OpenModeCookies,
) -> None:
    line = switched(cookies, actor_of(DANA), "sales", "as_user")

    name_and_value, *attributes = [part.strip() for part in line.split(";")]
    assert name_and_value.startswith(f"{OPEN_MODE_COOKIE_NAME}=")
    assert sorted(attributes) == ["HttpOnly", "Path=/", "SameSite=Lax", "Secure"]
    # No Domain (a __Host- cookie must not have one) and no lifetime: it ends with the session.
    assert "domain" not in line.lower() and "max-age" not in line.lower()
    assert "expires" not in line.lower()


def test_a_choice_comes_back_for_the_person_it_was_set_for(cookies: OpenModeCookies) -> None:
    dana = actor_of(DANA)
    line = switched(cookies, dana, "sales", "as_user")

    assert choice_of(cookies, dana, cookie_pair(line)) == {"sales"}


def test_the_email_is_compared_without_case_or_padding(cookies: OpenModeCookies) -> None:
    line = switched(cookies, actor_of(DANA), "sales", "as_user")

    assert choice_of(cookies, actor_of(f"  {DANA.upper()} "), cookie_pair(line)) == {"sales"}


def test_a_cookie_is_no_choice_for_anyone_but_the_person_it_was_set_for(
    cookies: OpenModeCookies,
) -> None:
    line = switched(cookies, actor_of(DANA), "sales", "as_user")

    assert choice_of(cookies, actor_of(HEIDI), cookie_pair(line)) == frozenset()


def test_a_cookie_made_with_another_key_is_no_choice(cookies: OpenModeCookies) -> None:
    other = OpenModeCookies(key=b"z" * 32)
    line = switched(other, actor_of(DANA), "sales", "as_user")

    assert choice_of(cookies, actor_of(DANA), cookie_pair(line)) == frozenset()


def test_a_new_process_forgets_every_choice() -> None:
    first, second = OpenModeCookies(), OpenModeCookies()
    dana = actor_of(DANA)

    line = switched(first, dana, "sales", "as_user")

    assert choice_of(first, dana, cookie_pair(line)) == {"sales"}
    assert choice_of(second, dana, cookie_pair(line)) == frozenset()


def test_a_key_that_is_too_short_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        OpenModeCookies(key=b"short")


@pytest.mark.parametrize(
    "value",
    [
        "sales",  # what a page script writes: no signature
        "sales~",
        "sales~AAAA",
        "~",
        "",
        "sales~" + "A" * 43,  # the right length, the wrong signature
        "sales.docs~" + "A" * 43,
        "Sales~" + "A" * 43,
        "not a slug~" + "A" * 43,
        "x" * 5000,
    ],
)
def test_a_value_a_script_could_write_is_no_choice(cookies: OpenModeCookies, value: str) -> None:
    assert choice_of(cookies, actor_of(DANA), f"{OPEN_MODE_COOKIE_NAME}={value}") == frozenset()


def test_a_signed_value_changed_afterwards_is_no_choice(cookies: OpenModeCookies) -> None:
    dana = actor_of(DANA)
    pair = cookie_pair(switched(cookies, dana, "sales", "as_user"))
    name, _, value = pair.partition("=")
    slugs, _, signature = value.partition("~")

    assert choice_of(cookies, dana, pair) == {"sales"}
    assert choice_of(cookies, dana, f"{name}=docs~{signature}") == frozenset()  # another app
    assert choice_of(cookies, dana, f"{name}={slugs}.docs~{signature}") == frozenset()  # one more
    flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    assert choice_of(cookies, dana, f"{name}={slugs}~{flipped}") == frozenset()


def test_a_cookie_sent_twice_is_no_choice(cookies: OpenModeCookies) -> None:
    dana = actor_of(DANA)
    pair = cookie_pair(switched(cookies, dana, "sales", "as_user"))

    assert choice_of(cookies, dana, f"{pair}; {pair}") == frozenset()
    assert choice_of(cookies, dana, pair, pair) == frozenset()


def test_other_cookies_and_lookalike_names_do_not_matter(cookies: OpenModeCookies) -> None:
    dana = actor_of(DANA)
    pair = cookie_pair(switched(cookies, dana, "sales", "as_user"))
    value = pair.partition("=")[2]

    assert choice_of(cookies, dana, f"a=1; {pair}; b=2") == {"sales"}
    assert choice_of(cookies, dana, "a=1", pair, "b=2") == {"sales"}  # HTTP/2 splits the line
    assert choice_of(cookies, dana, f"appmux_open={value}") == frozenset()
    assert choice_of(cookies, dana, f"x{OPEN_MODE_COOKIE_NAME}={value}") == frozenset()
    assert choice_of(cookies, dana, f"{OPEN_MODE_COOKIE_NAME.lower()}={value}") == frozenset()
    assert choice_of(cookies, dana, f"a={pair}") == frozenset()  # inside another cookie's value


def test_no_cookie_header_is_no_choice(cookies: OpenModeCookies) -> None:
    assert cookies.chosen_as_user([("Host", "apps.example.test")], actor_of(DANA)) == frozenset()
    assert cookies.chosen_as_user([], actor_of(DANA)) == frozenset()


def test_choices_for_several_apps_are_kept_and_switching_back_removes_only_that_app(
    cookies: OpenModeCookies,
) -> None:
    dana = actor_of(DANA)
    first = cookie_pair(switched(cookies, dana, "sales", "as_user"))
    second = cookie_pair(switched(cookies, dana, "team", "as_user", sent=[first]))
    assert choice_of(cookies, dana, second) == {"sales", "team"}

    back = cookie_pair(switched(cookies, dana, "sales", "without_token", sent=[second]))

    assert choice_of(cookies, dana, back) == {"team"}


def test_choosing_the_same_app_twice_keeps_one_entry(cookies: OpenModeCookies) -> None:
    dana = actor_of(DANA)
    first = cookie_pair(switched(cookies, dana, "sales", "as_user"))
    again = cookie_pair(switched(cookies, dana, "sales", "as_user", sent=[first]))

    assert choice_of(cookies, dana, again) == {"sales"}


def test_switching_back_the_last_app_deletes_the_cookie(cookies: OpenModeCookies) -> None:
    dana = actor_of(DANA)
    first = cookie_pair(switched(cookies, dana, "sales", "as_user"))

    line = switched(cookies, dana, "sales", "without_token", sent=[first])

    name_and_value, *attributes = [part.strip() for part in line.split(";")]
    assert name_and_value == f"{OPEN_MODE_COOKIE_NAME}="
    assert "Max-Age=0" in attributes
    assert {"Path=/", "Secure", "HttpOnly", "SameSite=Lax"} <= set(attributes)


def test_switching_back_when_nothing_was_chosen_still_answers_with_a_deletion(
    cookies: OpenModeCookies,
) -> None:
    line = switched(cookies, actor_of(DANA), "sales", "without_token")

    assert line.startswith(f"{OPEN_MODE_COOKIE_NAME}=;") and "Max-Age=0" in line


def test_a_cookie_that_is_not_valid_is_replaced_not_extended(cookies: OpenModeCookies) -> None:
    dana = actor_of(DANA)

    line = switched(cookies, dana, "sales", "as_user", sent=[f"{OPEN_MODE_COOKIE_NAME}=docs"])

    assert choice_of(cookies, dana, cookie_pair(line)) == {"sales"}


def test_at_most_sixteen_apps_are_kept_and_the_newest_stay(cookies: OpenModeCookies) -> None:
    dana = actor_of(DANA)
    sent: list[str] = []
    slugs = [f"app-{number}" for number in range(MAX_CHOICES + 1)]
    for slug in slugs:
        sent = [cookie_pair(switched(cookies, dana, slug, "as_user", sent=sent))]

    chosen = choice_of(cookies, dana, *sent)
    assert MAX_CHOICES == 16
    assert chosen == set(slugs[1:])


# --- the decision ---------------------------------------------------------------------------


def as_user_cookie(cookies: OpenModeCookies, email: str, *slugs: str) -> str:
    actor = actor_of(email)
    sent: list[str] = []
    for slug in slugs:
        sent = [cookie_pair(switched(cookies, actor, slug, "as_user", sent=sent))]
    return sent[0]


async def decide(
    policy: OpenModeTokenPolicy, email: str, slug: str, *cookie_pairs: str, token: bool = True
) -> bool:
    headers = [("Host", "apps.example.test"), *(("Cookie", pair) for pair in cookie_pairs)]
    return await policy(actor_of(email, token=TOKEN if token else None), slug, headers)


async def test_an_admin_without_a_grant_gets_no_token_by_default(
    policy: OpenModeTokenPolicy,
) -> None:
    assert await decide(policy, DANA, "sales") is False


async def test_an_admin_who_chose_open_as_a_user_gets_the_token_for_that_app_only(
    policy: OpenModeTokenPolicy, cookies: OpenModeCookies
) -> None:
    pair = as_user_cookie(cookies, DANA, "sales")

    assert await decide(policy, DANA, "sales", pair) is True
    assert await decide(policy, DANA, "team", pair) is False  # OBO too, but not chosen


async def test_switching_back_stops_the_token(
    policy: OpenModeTokenPolicy, cookies: OpenModeCookies
) -> None:
    dana = actor_of(DANA)
    chosen = as_user_cookie(cookies, DANA, "sales")
    back = switched(cookies, dana, "sales", "without_token", sent=[chosen])

    assert await decide(policy, DANA, "sales", chosen) is True
    assert await decide(policy, DANA, "sales", cookie_pair(back)) is False


async def test_a_cookie_another_person_holds_gives_no_token(
    policy: OpenModeTokenPolicy, cookies: OpenModeCookies
) -> None:
    danas_cookie = as_user_cookie(cookies, DANA, "sales")

    assert await decide(policy, HEIDI, "sales", danas_cookie) is False


async def test_a_value_a_child_page_script_wrote_gives_no_token(
    policy: OpenModeTokenPolicy,
) -> None:
    forged = f"{OPEN_MODE_COOKIE_NAME}=sales"

    assert await decide(policy, DANA, "sales", forged) is False


async def test_an_app_that_is_not_obo_delegated_never_gets_a_token(
    policy: OpenModeTokenPolicy, cookies: OpenModeCookies
) -> None:
    pair = as_user_cookie(cookies, DANA, "docs")

    assert await decide(policy, DANA, "docs", pair) is False
    assert await decide(policy, FRANK, "docs") is False  # not even its owner
    assert await decide(policy, ERIN, "docs") is False


@pytest.mark.parametrize(
    ("email", "slug"),
    [
        (BOB, "sales"),  # CAN_USE by a user grant
        (ALICE, "sales"),  # the owner
        (DANA, "hr"),  # a platform admin who is named by a grant already is a visitor
        (ALICE, "team"),  # the owner of a group-shared app
    ],
)
async def test_a_visitor_of_an_obo_app_gets_the_token_with_no_cookie(
    policy: OpenModeTokenPolicy, email: str, slug: str
) -> None:
    assert await decide(policy, email, slug) is True


async def test_a_group_member_of_an_obo_app_gets_the_token(
    policy: OpenModeTokenPolicy, access: FakeAccess
) -> None:
    access.groups[CAROL] = frozenset({"sales-team"})

    assert await decide(policy, CAROL, "team") is True


async def test_a_group_admin_is_an_admin_without_a_grant(
    policy: OpenModeTokenPolicy, access: FakeAccess, cookies: OpenModeCookies
) -> None:
    access.groups[CAROL] = frozenset({"data-platform"})
    pair = as_user_cookie(cookies, CAROL, "sales")

    assert await decide(policy, CAROL, "sales") is False
    assert await decide(policy, CAROL, "sales", pair) is True


async def test_a_person_who_cannot_open_the_app_gets_no_token(
    policy: OpenModeTokenPolicy, cookies: OpenModeCookies
) -> None:
    pair = as_user_cookie(cookies, ERIN, "sales")

    assert await decide(policy, ERIN, "sales") is False
    assert await decide(policy, ERIN, "sales", pair) is False


async def test_an_unknown_app_gets_no_token(policy: OpenModeTokenPolicy) -> None:
    assert await decide(policy, DANA, "nothing") is False


async def test_unknown_groups_leave_the_admin_without_the_token_even_after_the_choice(
    policy: OpenModeTokenPolicy, access: FakeAccess, cookies: OpenModeCookies
) -> None:
    """A group grant may hide behind the unknown, so "named by no grant" cannot be shown."""
    access.groups[DANA] = None
    pair = as_user_cookie(cookies, DANA, "sales")

    assert await decide(policy, DANA, "sales", pair) is False


async def test_a_visitor_with_a_user_grant_keeps_the_token_while_groups_are_unknown(
    policy: OpenModeTokenPolicy, access: FakeAccess
) -> None:
    access.groups[BOB] = None

    assert await decide(policy, BOB, "sales") is True


async def test_an_error_while_deciding_means_no_token_and_leaks_nothing(
    policy: OpenModeTokenPolicy,
    access: FakeAccess,
    cookies: OpenModeCookies,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pair = as_user_cookie(cookies, DANA, "sales")
    access.is_broken = True

    with caplog.at_level(logging.DEBUG):
        assert await decide(policy, DANA, "sales", pair) is False
        assert await decide(policy, BOB, "sales") is False

    assert caplog.records, "the failure is logged once per decision"
    logged = caplog.text
    assert TOKEN not in logged
    assert pair.partition("=")[2] not in logged
    assert DANA not in logged and BOB not in logged


# --- what the child receives ----------------------------------------------------------------


async def child_headers(
    policy: OpenModeTokenPolicy,
    email: str,
    slug: str,
    cookie_lines: Sequence[str] = (),
    *,
    token: str | None = TOKEN,
) -> dict[str, list[str]]:
    """The headers of the request after the edge's decision and rewrite, grouped by lower name."""
    actor = actor_of(email, token=token)
    incoming = [("Host", "apps.example.test"), ("X-Forwarded-Email", email)]
    if token is not None:
        incoming.append(("X-Forwarded-Access-Token", token))
    incoming += [("Cookie", line) for line in cookie_lines]
    allowed = await policy(actor, slug, incoming)
    grouped: dict[str, list[str]] = {}
    for name, value in rewrite_request_headers(
        incoming, actor=actor, slug=slug, request_id="req-1", forward_access_token=allowed
    ):
        grouped.setdefault(name.lower(), []).append(value)
    return grouped


async def test_the_default_for_an_admin_removes_the_token_and_the_app_still_sees_the_email(
    policy: OpenModeTokenPolicy,
) -> None:
    received = await child_headers(policy, DANA, "sales")

    assert "x-forwarded-access-token" not in received
    assert received["x-forwarded-email"] == [DANA]


async def test_after_open_as_a_user_the_child_receives_the_token_until_switched_back(
    policy: OpenModeTokenPolicy, cookies: OpenModeCookies
) -> None:
    dana = actor_of(DANA)
    chosen = as_user_cookie(cookies, DANA, "sales")
    back = cookie_pair(switched(cookies, dana, "sales", "without_token", sent=[chosen]))

    with_token = await child_headers(policy, DANA, "sales", [chosen])
    without = await child_headers(policy, DANA, "sales", [back])

    assert with_token["x-forwarded-access-token"] == [TOKEN]
    assert with_token["x-forwarded-email"] == [DANA]
    assert "x-forwarded-access-token" not in without


async def test_a_non_obo_app_has_one_open_and_never_receives_a_token(
    policy: OpenModeTokenPolicy, cookies: OpenModeCookies
) -> None:
    chosen = as_user_cookie(cookies, DANA, "docs")

    assert "x-forwarded-access-token" not in await child_headers(policy, DANA, "docs", [chosen])
    assert "x-forwarded-access-token" not in await child_headers(policy, FRANK, "docs")


async def test_a_visitor_of_an_obo_app_receives_the_token_as_before(
    policy: OpenModeTokenPolicy,
) -> None:
    received = await child_headers(policy, BOB, "sales")

    assert received["x-forwarded-access-token"] == [TOKEN]


async def test_the_open_mode_cookie_is_removed_before_forwarding_and_other_cookies_stay(
    policy: OpenModeTokenPolicy, cookies: OpenModeCookies
) -> None:
    chosen = as_user_cookie(cookies, DANA, "sales")

    received = await child_headers(policy, DANA, "sales", [f"theme=dark; {chosen}; lang=en"])

    assert received["cookie"] == ["theme=dark; lang=en"]
    assert OPEN_MODE_COOKIE_NAME not in str(received)
    assert chosen.partition("=")[2] not in str(received)


async def test_a_line_holding_only_the_open_mode_cookie_is_dropped(
    policy: OpenModeTokenPolicy, cookies: OpenModeCookies
) -> None:
    chosen = as_user_cookie(cookies, DANA, "sales")

    received = await child_headers(policy, DANA, "sales", [chosen, "theme=dark"])

    assert received["cookie"] == ["theme=dark"]


async def test_the_cookie_is_removed_in_every_case_and_when_the_policy_denies(
    policy: OpenModeTokenPolicy,
) -> None:
    forged = f"{OPEN_MODE_COOKIE_NAME.lower()}=sales; {OPEN_MODE_COOKIE_NAME}=x"

    received = await child_headers(policy, ERIN, "docs", [forged])

    assert "cookie" not in received


async def test_cookie_lines_without_the_open_mode_cookie_are_forwarded_unchanged(
    policy: OpenModeTokenPolicy,
) -> None:
    lines = ["a=1;b=2", f"x{OPEN_MODE_COOKIE_NAME}=1"]

    received = await child_headers(policy, BOB, "sales", lines)

    assert received["cookie"] == lines
