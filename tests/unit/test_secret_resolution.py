"""Secret resolution at child start (T121; FR-038, data model rule 15, design 8.3).

A ``secret`` resource is a ``scope/key`` reference. The appmux service principal reads its value
when a child starts, and the value goes to one place: the caller that builds the child's
environment (``sandbox/environment.py``). It is in no log, no audit row, no release row and no
build log; a start that cannot read a secret fails and names the scope, never the value.

The reader is tested against an ``httpx.MockTransport``: what these tests prove is how an answer is
read, not that Databricks answers this way today (spike 11 measured it on the Free Edition
workspace on 2026-10-30; the container-side check is the integration test of T129).
Every value, token and host here is a fake placeholder.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import pytest
from pydantic import SecretStr

from appmux.registry.models import AppResource
from appmux.registry.snapshot import SNAPSHOT_TABLES, RegistrySnapshot, build_snapshot
from appmux.sandbox.environment import EnvEntry, build_child_environment
from appmux.secrets.resolver import (
    REDACTED,
    DatabricksSecretReader,
    MissingResourceKeys,
    ResolvedResources,
    ResourceValueResolver,
    SecretReadFailed,
    SecretUnreadable,
    scrub_secrets,
)

pytestmark = pytest.mark.anyio

NOW: Final = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
BASE_URL: Final = "https://workspace.example.invalid"
SP_TOKEN: Final = "tok-test-sp"  # noqa: S105 - a placeholder, not a credential
API_KEY_VALUE: Final = "sk-placeholder-value-1"
DB_PASSWORD_VALUE: Final = "placeholder-db-pass-2"  # noqa: S105 - a placeholder, not a credential


# --- builders ------------------------------------------------------------------------------------


def resource(
    key: str,
    type_name: str = "secret",
    target: str | None = None,
    *,
    app_slug: str = "sales",
) -> AppResource:
    default_target = {"secret": "sales-secrets/openai-api-key", "sql_warehouse": "a1b2c3d4e5f67890"}
    return AppResource(
        app_slug=app_slug,
        key=key,
        type=type_name,
        target=target if target is not None else default_target.get(type_name, "x"),
        permission="READ" if type_name == "secret" else "CAN_USE",
        access_as=None if type_name == "secret" else "obo",
        check_result="ok",
        attached_by="alice@example.com",
        attached_at=NOW,
    )


def snapshot_of(*resources: AppResource) -> RegistrySnapshot:
    rows: dict[str, list[Any]] = {table: [] for table in SNAPSHOT_TABLES}
    rows["app_resources"] = list(resources)
    return build_snapshot(rows, loaded_at=NOW)


class StubReader:
    """Answers from a table of ``scope/key`` to value, and remembers every read."""

    def __init__(self, values: Mapping[str, str], *, failures: Mapping[str, str] | None = None):
        self.values = dict(values)
        self.failures = dict(failures or {})  # scope -> reason
        self.reads: list[tuple[str, str]] = []

    async def read(self, scope: str, key: str) -> str:
        self.reads.append((scope, key))
        if scope in self.failures:
            raise SecretReadFailed(scope, self.failures[scope])  # type: ignore[arg-type]
        return self.values[f"{scope}/{key}"]


def resolver_for(
    reader: StubReader, *resources: AppResource
) -> tuple[ResourceValueResolver, list[RegistrySnapshot]]:
    """A resolver over a movable snapshot: the list's last item is the current one."""
    current = [snapshot_of(*resources)]
    return ResourceValueResolver(snapshot=lambda: current[-1], reader=reader), current


STANDARD_SECRETS: Final = {
    "sales-secrets/openai-api-key": API_KEY_VALUE,
    "sales-secrets/db-password": DB_PASSWORD_VALUE,
}


# --- the resolver --------------------------------------------------------------------------------


async def test_a_secret_reference_is_read_and_its_value_returned_under_the_resource_key() -> None:
    reader = StubReader(STANDARD_SECRETS)
    resolver, _ = resolver_for(reader, resource("openai-api-key"))

    values = await resolver.resolve("sales", ["openai-api-key"])

    assert dict(values) == {"openai-api-key": API_KEY_VALUE}
    assert reader.reads == [("sales-secrets", "openai-api-key")]


async def test_a_resource_that_is_not_a_secret_resolves_to_its_target_without_a_read() -> None:
    reader = StubReader({})
    resolver, _ = resolver_for(reader, resource("sql-warehouse", "sql_warehouse"))

    values = await resolver.resolve("sales", ["sql-warehouse"])

    assert dict(values) == {"sql-warehouse": "a1b2c3d4e5f67890"}
    assert reader.reads == []


async def test_only_the_asked_keys_are_read() -> None:
    reader = StubReader(STANDARD_SECRETS)
    resolver, _ = resolver_for(
        reader,
        resource("openai-api-key"),
        resource("db-password", target="sales-secrets/db-password"),
    )

    values = await resolver.resolve("sales", ["db-password"])

    assert dict(values) == {"db-password": DB_PASSWORD_VALUE}
    assert reader.reads == [("sales-secrets", "db-password")]


async def test_no_keys_means_no_reads_and_an_empty_result() -> None:
    reader = StubReader({})
    resolver, _ = resolver_for(reader, resource("openai-api-key"))

    assert dict(await resolver.resolve("sales", [])) == {}
    assert reader.reads == []


async def test_a_key_of_another_app_is_not_a_resource_of_this_one() -> None:
    reader = StubReader(STANDARD_SECRETS)
    resolver, _ = resolver_for(reader, resource("openai-api-key", app_slug="other"))

    with pytest.raises(MissingResourceKeys) as missing:
        await resolver.resolve("sales", ["openai-api-key"])

    assert missing.value.keys == ("openai-api-key",)
    assert reader.reads == []


async def test_every_missing_key_is_named_at_once_and_nothing_is_read_first() -> None:
    reader = StubReader(STANDARD_SECRETS)
    resolver, _ = resolver_for(reader, resource("openai-api-key"))

    with pytest.raises(MissingResourceKeys) as missing:
        await resolver.resolve("sales", ["openai-api-key", "zeta", "alpha", "mid"])

    assert missing.value.keys == ("alpha", "mid", "zeta")
    assert reader.reads == []


async def test_the_snapshot_is_read_when_resolving_so_a_detached_resource_is_missing() -> None:
    reader = StubReader(STANDARD_SECRETS)
    resolver, current = resolver_for(reader, resource("openai-api-key"))
    assert dict(await resolver.resolve("sales", ["openai-api-key"])) != {}

    current.append(snapshot_of())  # the resource was detached since

    with pytest.raises(MissingResourceKeys):
        await resolver.resolve("sales", ["openai-api-key"])


# --- a secret that cannot be read fails the start, naming the scope ------------------------------


async def test_an_unreadable_secret_fails_naming_the_scope_and_neither_key_nor_value() -> None:
    reader = StubReader(STANDARD_SECRETS, failures={"locked-scope": "denied"})
    resolver, _ = resolver_for(
        reader,
        resource("openai-api-key"),
        resource("db-password", target="locked-scope/the-hidden-key-name"),
    )

    with pytest.raises(SecretUnreadable) as unreadable:
        await resolver.resolve("sales", ["openai-api-key", "db-password"])

    text = f"{unreadable.value} {unreadable.value!r} {unreadable.value.message}"
    assert unreadable.value.scopes == ("locked-scope",)
    assert "'locked-scope'" in unreadable.value.message
    assert "the-hidden-key-name" not in text
    assert API_KEY_VALUE not in text  # a secret that was read is not in the failure either


async def test_every_unreadable_scope_is_named_at_once() -> None:
    reader = StubReader({}, failures={"scope-b": "not_found", "scope-a": "denied"})
    resolver, _ = resolver_for(
        reader,
        resource("one", target="scope-b/k1"),
        resource("two", target="scope-a/k2"),
        resource("three", target="scope-a/k3"),
    )

    with pytest.raises(SecretUnreadable) as unreadable:
        await resolver.resolve("sales", ["one", "two", "three"])

    assert unreadable.value.scopes == ("scope-a", "scope-b")
    assert "'scope-a'" in unreadable.value.message
    assert "'scope-b'" in unreadable.value.message


@pytest.mark.parametrize(
    ("reason", "advice"),
    [
        ("denied", "READ"),
        ("not_found", "does not exist"),
        ("unavailable", "did not answer"),
        ("malformed", "not usable text"),
    ],
)
async def test_the_message_says_why_and_what_to_do(reason: str, advice: str) -> None:
    reader = StubReader({}, failures={"sales-secrets": reason})
    resolver, _ = resolver_for(reader, resource("openai-api-key"))

    with pytest.raises(SecretUnreadable) as unreadable:
        await resolver.resolve("sales", ["openai-api-key"])

    assert advice in unreadable.value.message
    assert "start the release again" in unreadable.value.message


APPMUX_APPLICATION_ID: Final = "0d4c1b84-3f5a-4a52-9a52-6a9c1f3e7b10"


async def test_a_denied_read_names_the_service_principal_that_needs_the_grant() -> None:
    # After the App is deleted and created again its service principal is new, and the person who
    # reads this has to find it among the workspace's principals (ADR 0022).
    reader = StubReader({}, failures={"sales-secrets": "denied"})
    current = [snapshot_of(resource("openai-api-key"))]
    resolver = ResourceValueResolver(
        snapshot=lambda: current[-1], reader=reader, service_principal=APPMUX_APPLICATION_ID
    )

    with pytest.raises(SecretUnreadable) as unreadable:
        await resolver.resolve("sales", ["openai-api-key"])

    message = unreadable.value.message
    assert f"service principal {APPMUX_APPLICATION_ID} needs READ" in message
    assert "'sales-secrets'" in message


async def test_a_read_that_failed_for_another_reason_does_not_name_the_service_principal() -> None:
    reader = StubReader({}, failures={"sales-secrets": "not_found"})
    current = [snapshot_of(resource("openai-api-key"))]
    resolver = ResourceValueResolver(
        snapshot=lambda: current[-1], reader=reader, service_principal=APPMUX_APPLICATION_ID
    )

    with pytest.raises(SecretUnreadable) as unreadable:
        await resolver.resolve("sales", ["openai-api-key"])

    assert APPMUX_APPLICATION_ID not in unreadable.value.message


async def test_a_stored_reference_that_is_not_scope_slash_key_is_never_sent_anywhere() -> None:
    # A row written around the attach check (a shared-SP child can write app_resources) can hold
    # any text as a target. It is not a reference, so no read is made, and the text that reaches
    # the status reason is made printable and short.
    hostile = "x'; DROP\n" + "a" * 300 + "/key"
    reader = StubReader({})
    resolver, _ = resolver_for(reader, _unchecked_secret("odd", hostile))

    with pytest.raises(SecretUnreadable) as unreadable:
        await resolver.resolve("sales", ["odd"])

    message = unreadable.value.message
    assert reader.reads == []
    assert "\n" not in message
    assert "DROP" not in message
    assert len(message) < 400


def _unchecked_secret(key: str, target: str) -> AppResource:
    # ``AppResource`` insists on ``[^/]+/[^/]+`` for a secret; this target obeys that and breaks
    # the stricter form the attach check enforces.
    return resource(key, target=target)


async def test_a_value_the_reader_returns_is_not_validated_or_changed_by_the_resolver() -> None:
    reader = StubReader({"sales-secrets/openai-api-key": " spaced value\twith tab "})
    resolver, _ = resolver_for(reader, resource("openai-api-key"))

    values = await resolver.resolve("sales", ["openai-api-key"])

    assert values["openai-api-key"] == " spaced value\twith tab "


# --- the value goes only to the caller -----------------------------------------------------------


async def test_the_result_lists_names_only_when_it_is_printed() -> None:
    reader = StubReader(STANDARD_SECRETS)
    resolver, _ = resolver_for(reader, resource("openai-api-key"))

    values = await resolver.resolve("sales", ["openai-api-key"])

    assert isinstance(values, ResolvedResources)
    for shown in (repr(values), str(values), f"{values}", f"{values!r}"):
        assert API_KEY_VALUE not in shown
        assert "openai-api-key" in shown


async def test_the_result_tells_which_values_are_secrets_so_a_log_can_be_scrubbed() -> None:
    reader = StubReader(STANDARD_SECRETS)
    resolver, _ = resolver_for(
        reader,
        resource("openai-api-key"),
        resource("sql-warehouse", "sql_warehouse"),
    )

    values = await resolver.resolve("sales", ["openai-api-key", "sql-warehouse"])

    assert isinstance(values, ResolvedResources)
    assert values.secret_values == (API_KEY_VALUE,)  # a warehouse id is not a secret


async def test_the_resolver_and_the_reader_print_no_value() -> None:
    reader = StubReader(STANDARD_SECRETS)
    resolver, _ = resolver_for(reader, resource("openai-api-key"))
    await resolver.resolve("sales", ["openai-api-key"])

    assert API_KEY_VALUE not in repr(resolver)
    http = httpx.AsyncClient(base_url=BASE_URL)
    assert SP_TOKEN not in repr(DatabricksSecretReader(http, token_provider=_token(SP_TOKEN)))


async def test_the_value_reaches_the_child_environment_and_nothing_else_prints_it() -> None:
    reader = StubReader(STANDARD_SECRETS)
    resolver, _ = resolver_for(reader, resource("openai-api-key"))
    values = await resolver.resolve("sales", ["openai-api-key"])

    environment = build_child_environment(
        app_yaml_env=[EnvEntry(name="OPENAI_API_KEY", value_from="openai-api-key")],
        release_env=[],
        appmux_variables={"APPMUX_SLUG": "sales"},
        resource_values=values,
    )

    assert environment["OPENAI_API_KEY"] == API_KEY_VALUE
    assert API_KEY_VALUE not in repr(environment)


async def test_no_log_line_holds_a_value_on_success_or_on_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    good_reader = StubReader(STANDARD_SECRETS)
    good, _ = resolver_for(good_reader, resource("openai-api-key"))
    bad_reader = StubReader(STANDARD_SECRETS, failures={"sales-secrets": "denied"})
    bad, _ = resolver_for(bad_reader, resource("openai-api-key"))

    await good.resolve("sales", ["openai-api-key"])
    with pytest.raises(SecretUnreadable):
        await bad.resolve("sales", ["openai-api-key"])

    assert API_KEY_VALUE not in caplog.text


# --- scrubbing: a build or start log that echoes a value -----------------------------------------


def test_a_value_echoed_in_a_log_is_replaced() -> None:
    log = f"building...\nusing key {API_KEY_VALUE} to fetch\nPASSWORD={DB_PASSWORD_VALUE}\ndone"

    scrubbed = scrub_secrets(log, [API_KEY_VALUE, DB_PASSWORD_VALUE])

    assert API_KEY_VALUE not in scrubbed
    assert DB_PASSWORD_VALUE not in scrubbed
    assert scrubbed == f"building...\nusing key {REDACTED} to fetch\nPASSWORD={REDACTED}\ndone"


def test_every_occurrence_is_replaced() -> None:
    assert scrub_secrets("abcd-abcd-abcd", ["abcd"]) == f"{REDACTED}-{REDACTED}-{REDACTED}"


def test_a_value_that_contains_another_is_replaced_whole() -> None:
    # The shorter value first would leave "-LONGER" behind.
    scrubbed = scrub_secrets("token=PREFIX-LONGER end", ["PREFIX", "PREFIX-LONGER"])

    assert scrubbed == f"token={REDACTED} end"


def test_an_empty_value_replaces_nothing() -> None:
    assert scrub_secrets("text", ["", ""]) == "text"


def test_a_log_without_a_value_is_unchanged() -> None:
    assert scrub_secrets("nothing here", [API_KEY_VALUE]) == "nothing here"


def test_scrubbing_accepts_any_iterable_of_values_once() -> None:
    values = (value for value in [API_KEY_VALUE])

    assert API_KEY_VALUE not in scrub_secrets(f"x {API_KEY_VALUE} y", values)


def test_the_replacement_text_is_not_a_secret_shape_and_holds_no_value() -> None:
    assert REDACTED.isprintable()
    assert API_KEY_VALUE not in REDACTED


# --- the reader: Databricks secrets/get with the service principal's token -----------------------

Reply = Callable[[httpx.Request], httpx.Response]


def _token(value: str) -> Callable[[], Awaitable[SecretStr]]:
    async def provide() -> SecretStr:
        return SecretStr(value)

    return provide


def reader_for(
    reply: Reply,
    seen: list[httpx.Request] | None = None,
    *,
    token_provider: Callable[[], Awaitable[SecretStr]] | None = None,
    **client: Any,
) -> DatabricksSecretReader:
    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return reply(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url=BASE_URL, **client)
    return DatabricksSecretReader(http, token_provider=token_provider or _token(SP_TOKEN))


def value_reply(value: str | bytes) -> Reply:
    raw = value.encode() if isinstance(value, str) else value
    body = {"key": "openai-api-key", "value": base64.b64encode(raw).decode()}
    return lambda _request: httpx.Response(200, json=body)


def status_reply(status: int, body: Any = None) -> Reply:
    return lambda _request: httpx.Response(status, json=body if body is not None else {})


async def failure_of(reader: DatabricksSecretReader) -> SecretReadFailed:
    with pytest.raises(SecretReadFailed) as failed:
        await reader.read("sales-secrets", "openai-api-key")
    return failed.value


async def test_the_reader_gets_the_value_with_the_service_principals_bearer_token() -> None:
    seen: list[httpx.Request] = []
    reader = reader_for(value_reply(API_KEY_VALUE), seen)

    value = await reader.read("sales-secrets", "openai-api-key")

    assert value == API_KEY_VALUE
    (request,) = seen
    assert request.method == "GET"
    assert request.url.path == "/api/2.0/secrets/get"
    assert dict(request.url.params) == {"scope": "sales-secrets", "key": "openai-api-key"}
    assert request.headers["Authorization"] == f"Bearer {SP_TOKEN}"
    assert request.url.host == "workspace.example.invalid"


async def test_a_value_with_non_ascii_text_round_trips() -> None:
    reader = reader_for(value_reply("pässwörd-✓"))

    assert await reader.read("sales-secrets", "openai-api-key") == "pässwörd-✓"


async def test_the_token_is_asked_for_on_every_read_so_the_sdk_can_refresh_it() -> None:
    tokens = iter(["tok-one", "tok-two"])

    async def provide() -> SecretStr:
        return SecretStr(next(tokens))

    seen: list[httpx.Request] = []
    reader = reader_for(value_reply("v"), seen, token_provider=provide)

    await reader.read("sales-secrets", "k1")
    await reader.read("sales-secrets", "k2")

    assert [r.headers["Authorization"] for r in seen] == ["Bearer tok-one", "Bearer tok-two"]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (403, "denied"),
        (404, "not_found"),
        (400, "not_found"),
        (401, "unavailable"),
        (429, "unavailable"),
        (500, "unavailable"),
        (503, "unavailable"),
        (302, "unavailable"),
    ],
)
async def test_an_answer_without_a_value_maps_to_a_reason(status: int, reason: str) -> None:
    failed = await failure_of(reader_for(status_reply(status, {"error_code": "X", "message": "m"})))

    assert (failed.scope, failed.reason) == ("sales-secrets", reason)


@pytest.mark.parametrize(
    "reply",
    [
        lambda _request: httpx.Response(200, text="not json"),
        lambda _request: httpx.Response(200, json={}),
        lambda _request: httpx.Response(200, json={"value": 7}),
        lambda _request: httpx.Response(200, json={"value": "!!! not base64 !!!"}),
        lambda _request: httpx.Response(200, json=[1, 2]),
        value_reply(b"\xff\xfe\x00 not utf-8"),
        value_reply("has a \x00 nul"),
    ],
)
async def test_a_200_that_holds_no_usable_text_is_malformed(reply: Reply) -> None:
    failed = await failure_of(reader_for(reply))

    assert failed.reason == "malformed"


async def test_a_transport_failure_is_unavailable_and_shows_neither_token_nor_value() -> None:
    def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused for Bearer {SP_TOKEN} {API_KEY_VALUE}")

    failed = await failure_of(reader_for(broken))

    assert failed.reason == "unavailable"
    shown = f"{failed} {failed!r} {failed.__cause__} {failed.__context__}"
    assert SP_TOKEN not in shown
    assert API_KEY_VALUE not in shown


async def test_a_token_provider_that_fails_is_unavailable_and_shows_no_text_of_the_failure() -> (
    None
):
    async def failing() -> SecretStr:
        raise RuntimeError(f"the SDK said {SP_TOKEN}")

    seen: list[httpx.Request] = []
    failed = await failure_of(reader_for(value_reply("v"), seen, token_provider=failing))

    assert failed.reason == "unavailable"
    assert seen == []
    assert SP_TOKEN not in f"{failed} {failed!r} {failed.__cause__} {failed.__context__}"


async def test_a_redirect_is_not_followed() -> None:
    seen: list[httpx.Request] = []

    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://elsewhere.example.invalid/x"})

    failed = await failure_of(reader_for(redirect, seen, follow_redirects=True))

    assert failed.reason == "unavailable"
    assert {r.url.host for r in seen} == {"workspace.example.invalid"}


async def test_the_service_principals_token_is_sent_even_if_the_client_carries_a_credential() -> (
    None
):
    seen: list[httpx.Request] = []
    reader = reader_for(
        value_reply("v"),
        seen,
        auth=httpx.BasicAuth("service", "placeholder-password"),
        headers={"Authorization": "Bearer placeholder-default-credential"},
    )

    await reader.read("sales-secrets", "openai-api-key")

    assert seen[0].headers["Authorization"] == f"Bearer {SP_TOKEN}"


@pytest.mark.parametrize(
    ("scope", "key"),
    [("a/b", "k"), ("s", "k/../x"), ("s p", "k"), ("s", "k&scope=other"), ("", "k"), ("s", "")],
)
async def test_a_scope_or_key_outside_the_secret_charset_is_refused_before_any_request(
    scope: str, key: str
) -> None:
    seen: list[httpx.Request] = []
    reader = reader_for(value_reply("v"), seen)

    with pytest.raises(SecretReadFailed) as failed:
        await reader.read(scope, key)

    assert failed.value.reason == "invalid_reference"
    assert seen == []


async def test_neither_the_token_nor_the_value_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    good = reader_for(value_reply(API_KEY_VALUE))
    bad = reader_for(status_reply(403, {"message": f"no READ; {API_KEY_VALUE}"}))

    await good.read("sales-secrets", "openai-api-key")
    with pytest.raises(SecretReadFailed):
        await bad.read("sales-secrets", "openai-api-key")

    assert SP_TOKEN not in caplog.text
    assert API_KEY_VALUE not in caplog.text


async def test_a_response_body_is_never_put_in_the_failure() -> None:
    reply = status_reply(403, {"message": f"PERMISSION_DENIED {API_KEY_VALUE} /home/app/x.py"})

    failed = await failure_of(reader_for(reply))

    shown = f"{failed} {failed!r}"
    assert "PERMISSION_DENIED" not in shown
    assert "/home/app" not in shown


# --- shapes the caller relies on -----------------------------------------------------------------


def test_the_reasons_a_read_can_fail_are_a_closed_set() -> None:
    for reason in ("denied", "not_found", "unavailable", "malformed", "invalid_reference"):
        assert SecretReadFailed("scope", reason).reason == reason
    with pytest.raises(ValueError, match="reason"):
        SecretReadFailed("scope", "something else")


def test_the_missing_keys_error_is_sorted_and_deduplicated() -> None:
    error = MissingResourceKeys(["b", "a", "b"])

    assert error.keys == ("a", "b")
    assert "a, b" in str(error)


def test_json_is_enough_to_build_the_expected_value_reply() -> None:
    # Guards the helper above: the reply shape is the documented `{"value": <base64>}`.
    body = json.loads(value_reply("v")(httpx.Request("GET", BASE_URL)).content)

    assert base64.b64decode(body["value"]) == b"v"
