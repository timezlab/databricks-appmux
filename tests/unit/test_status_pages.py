"""The status pages the request pipeline answers with (contracts/edge-http.md, T085).

A status page is built from fixed text and from a slug that matched a registered app, never from an
exception, a path or anything a child produced. These tests pin that: the pages name the app and
its state in words this build owns, escape what they print, and the headers keep the page inert
(no script, no framing, no caching).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from appmux.edge.status_pages import (
    APP_STARTING_RETRY_AFTER_S,
    BAD_GATEWAY_STATUS,
    GATEWAY_TIMEOUT_STATUS,
    app_unavailable_page,
    app_unavailable_retry_after_s,
    bad_gateway_page,
    gateway_timeout_page,
    plain_text_headers,
    status_page_headers,
)


def header_map(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {name.decode(): value.decode() for name, value in headers}


# --- the page of an app that has no route ------------------------------------------------------


@pytest.mark.parametrize("state", ["stopped", "starting", "crashed", "no_release"])
def test_the_page_of_an_unavailable_app_names_the_app_and_its_state(state: str) -> None:
    page = app_unavailable_page("sales", state).decode()

    assert "sales" in page
    assert f"<code>state</code>: {state}" in page
    # Inert under `default-src 'none'`: no script, no style, no reference to anything else.
    assert "<script" not in page and "<style" not in page and "http" not in page


def test_each_state_gets_its_own_advice() -> None:
    states = ("stopped", "starting", "crashed", "no_release")
    advice = {state: app_unavailable_page("sales", state) for state in states}

    assert len(set(advice.values())) == len(states)


@pytest.mark.parametrize("state", ["running", "", "booting <b>", "STOPPED"])
def test_a_state_this_build_does_not_know_is_shown_as_starting(state: str) -> None:
    # An app with no route is not serving. A reader that says "running" for it is behind the route
    # table, which is the same as an app that is still coming up.
    assert app_unavailable_page("sales", state) == app_unavailable_page("sales", "starting")


def test_a_slug_is_escaped_on_the_page() -> None:
    page = app_unavailable_page("<img src=x>", "stopped").decode()

    assert "<img" not in page
    assert "&lt;img" in page


@pytest.mark.parametrize(
    ("state", "retry_after_s"),
    [
        ("starting", APP_STARTING_RETRY_AFTER_S),
        ("stopped", None),
        ("crashed", None),
        ("no_release", None),
    ],
)
def test_only_an_app_that_is_coming_up_asks_the_client_to_come_back(
    state: str, retry_after_s: int | None
) -> None:
    assert app_unavailable_retry_after_s(state) == retry_after_s


# --- 502 and 504 ---------------------------------------------------------------------------------


def test_the_gateway_statuses_are_502_and_504() -> None:
    assert (BAD_GATEWAY_STATUS, GATEWAY_TIMEOUT_STATUS) == (502, 504)


@pytest.mark.parametrize("make_page", [bad_gateway_page, gateway_timeout_page])
def test_a_gateway_page_names_the_app_and_carries_nothing_from_the_failure(
    make_page: Callable[[str], bytes],
) -> None:
    page = make_page("sales").decode()

    assert "sales" in page
    for leak in ("Traceback", "Errno", "/home", ".sock", "Exception", "refused"):
        assert leak not in page


def test_the_two_gateway_pages_say_different_things() -> None:
    assert bad_gateway_page("sales") != gateway_timeout_page("sales")


# --- headers -------------------------------------------------------------------------------------


def test_a_page_without_retry_after_has_no_retry_after_header() -> None:
    headers = header_map(status_page_headers(b"<p>x</p>", retry_after_s=None))

    assert "retry-after" not in headers
    assert headers["content-length"] == "8"
    assert headers["cache-control"] == "no-store"
    assert headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"


def test_a_page_with_retry_after_states_it_in_seconds() -> None:
    headers = header_map(status_page_headers(b"x", retry_after_s=5))

    assert headers["retry-after"] == "5"


def test_plain_text_answers_are_not_cacheable_and_not_sniffable() -> None:
    headers = header_map(plain_text_headers(b"not found"))

    assert headers["content-type"] == "text/plain; charset=utf-8"
    assert headers["content-length"] == "9"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert "retry-after" not in headers


def test_a_plain_text_answer_can_ask_the_client_to_retry() -> None:
    headers = header_map(plain_text_headers(b"busy", retry_after_s=1))

    assert headers["retry-after"] == "1"
