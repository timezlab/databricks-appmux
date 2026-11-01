"""The attach check: the attacher's own token, three answers, and the rules around them (T120).

FR-037 and data model rules 4 to 6. appmux never grants access to a resource: it asks Databricks,
with the token of the person who attaches it, whether that person could use it, and stores the
answer. The answers are ``ok``, ``unverified`` (no usable check for the type, or the appmux App does
not declare the scope the check needs) and no access, which refuses the attach.

Two layers are tested here, both without Databricks:

* ``verify_attach`` (``registry/resources.py``) applies the rules and asks an ``AttachChecker``.
  The checker is a recording stub, so a test can say what it was asked and, as important, when it
  was *not* asked.
* ``DatabricksAttachChecker`` (``admin/resources.py``) is the real adapter. The calls and the
  refusal texts are those measured in spike 16 (2026-10-30, Free Edition only); the transport is an
  ``httpx.MockTransport``, so what these tests prove is how an answer is read, never that
  Databricks answers that way today.

Every token, host and id in this file is a fake placeholder.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import pytest
from pydantic import SecretStr

from appmux.admin.resources import DatabricksAttachChecker
from appmux.registry.models import App, AppConfig, AppResource, PlatformSetting
from appmux.registry.resources import (
    RESOURCE_TYPES,
    VERIFIABLE_TYPES,
    AttachCheckUnavailable,
    AttachProbe,
    AttachRefused,
    CheckVerdict,
    check_warehouse_id,
    own_sp_key_in_use,
    own_sp_keys_problem,
    shared_sp_delegation_allowed,
    verify_attach,
)
from appmux.registry.snapshot import SNAPSHOT_TABLES, build_snapshot

pytestmark = pytest.mark.anyio

NOW: Final = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
TOKEN: Final = SecretStr("tok-test")
BASE_URL: Final = "https://workspace.example.invalid"
CHECK_WAREHOUSE: Final = "checkwarehouse01"
NOT_A_LEVEL: Final = "NOT_A_LEVEL"

# type -> (a target of the right form, a level of the type)
VERIFIABLE: Final = {
    "secret": ("sales-secrets/openai-api-key", "READ"),
    "sql_warehouse": ("a1b2c3d4e5f67890", "CAN_USE"),
    "serving_endpoint": ("sales-forecast", "CAN_QUERY"),
    "uc_table": ("main.sales.orders", "SELECT"),
    "uc_volume": ("/Volumes/main/sales/raw", "READ_VOLUME"),
    "uc_function": ("main.sales.margin", "EXECUTE"),
    "uc_connection": ("crm-connection", "USE_CONNECTION"),
    "genie_space": ("01ef00000000000000000000000000aa", "CAN_RUN"),
}
UNVERIFIABLE: Final = {
    "mlflow_experiment": ("1111111111", "CAN_EDIT"),
    "lakebase_database": (
        "projects/sales/branches/main/endpoints/primary",
        "CAN_CONNECT_AND_CREATE",
    ),
    "job": ("2222222222", "CAN_VIEW"),
    "vector_search_index": ("main.sales.index", "SELECT"),
    "databricks_app": ("other-app", "CAN_USE"),
    "gateway_model_service": ("main.sales.service", "EXECUTE"),
}
EVERY_TYPE: Final = {**VERIFIABLE, **UNVERIFIABLE}


# --- builders ------------------------------------------------------------------------------------


def make_app(*, obo: bool = True, shared_sp: bool = True, slug: str = "sales") -> App:
    return App(
        slug=slug,
        display_name="Sales",
        owner="alice@example.com",
        source_path="/Workspace/Users/alice@example.com/sales",
        memory_budget_mb=256,
        obo_delegated=obo,
        shared_sp_delegated=shared_sp,
        created_by="dana@example.com",
        created_at=NOW,
        updated_by="dana@example.com",
        updated_at=NOW,
    )


def make_config(id_key: str | None = None, secret_key: str | None = None) -> AppConfig:
    return AppConfig(
        app_slug="sales",
        own_sp_client_id_key=id_key,
        own_sp_client_secret_key=secret_key,
        updated_by="alice@example.com",
        updated_at=NOW,
    )


def make_resource(key: str, type_name: str = "secret") -> AppResource:
    target, permission = EVERY_TYPE[type_name]
    return AppResource(
        app_slug="sales",
        key=key,
        type=type_name,
        target=target,
        permission=permission,
        access_as=None if type_name == "secret" else "obo",
        check_result="ok",
        attached_by="alice@example.com",
        attached_at=NOW,
    )


def setting(key: str, value: bool | int | str) -> PlatformSetting:
    return PlatformSetting(key=key, value=value, updated_by="dana@example.com", updated_at=NOW)


class StubChecker:
    """Answers with a fixed verdict (or raises) and remembers every probe it was given."""

    def __init__(
        self, verdict: CheckVerdict = CheckVerdict.OK, *, raises: Exception | None = None
    ) -> None:
        self.verdict = verdict
        self.raises = raises
        self.probes: list[AttachProbe] = []

    async def check(self, probe: AttachProbe) -> CheckVerdict:
        self.probes.append(probe)
        if self.raises is not None:
            raise self.raises
        return self.verdict


async def attach(
    checker: StubChecker,
    *,
    type_name: str = "sql_warehouse",
    target: str | None = None,
    permission: str | None = None,
    access_as: str | None = "obo",
    app: App | None = None,
    config: AppConfig | None = None,
    resources: Iterable[AppResource] = (),
    shared_sp_allowed: bool = True,
    warehouse_id: str | None = CHECK_WAREHOUSE,
    token: SecretStr | None = TOKEN,
) -> str:
    default_target, default_permission = EVERY_TYPE.get(type_name, ("x", "READ"))
    return await verify_attach(
        resource_type=type_name,
        target=target if target is not None else default_target,
        permission=permission if permission is not None else default_permission,
        access_as=None if type_name == "secret" else access_as,
        app=app if app is not None else make_app(),
        config=config if config is not None else make_config(),
        resources=tuple(resources),
        shared_sp_allowed=shared_sp_allowed,
        warehouse_id=warehouse_id,
        token=token,
        checker=checker,
    )


async def refusal(checker: StubChecker, **options: Any) -> AttachRefused:
    with pytest.raises(AttachRefused) as refused:
        await attach(checker, **options)
    return refused.value


# --- the catalogue -------------------------------------------------------------------------------


def test_the_catalogue_has_fourteen_types_and_eight_of_them_have_a_check() -> None:
    assert set(RESOURCE_TYPES) == set(EVERY_TYPE)
    assert set(VERIFIABLE) == VERIFIABLE_TYPES
    assert len(RESOURCE_TYPES) == 14
    assert len(VERIFIABLE_TYPES) == 8


# --- ok, unverified, no access -------------------------------------------------------------------


@pytest.mark.parametrize("type_name", sorted(VERIFIABLE))
async def test_a_checked_type_is_ok_when_the_attachers_token_reaches_it(type_name: str) -> None:
    checker = StubChecker(CheckVerdict.OK)

    result = await attach(checker, type_name=type_name)

    assert result == "ok"
    (probe,) = checker.probes
    target, permission = VERIFIABLE[type_name]
    assert (probe.resource_type, probe.target, probe.permission) == (type_name, target, permission)
    assert probe.token.get_secret_value() == "tok-test"


async def test_the_check_runs_with_the_attachers_token_and_no_other_credential() -> None:
    checker = StubChecker()

    await attach(checker, type_name="sql_warehouse", token=SecretStr("tok-of-the-attacher"))

    (probe,) = checker.probes
    assert probe.token.get_secret_value() == "tok-of-the-attacher"
    assert "tok-of-the-attacher" not in repr(probe)


async def test_a_scope_the_app_does_not_declare_gives_unverified() -> None:
    checker = StubChecker(CheckVerdict.UNVERIFIED)

    assert await attach(checker, type_name="uc_table") == "unverified"


@pytest.mark.parametrize("type_name", sorted(UNVERIFIABLE))
async def test_a_type_with_no_check_is_unverified_and_nothing_is_asked(type_name: str) -> None:
    checker = StubChecker(CheckVerdict.NO_ACCESS)  # would refuse, if it were asked

    # No token and no check warehouse either: this path needs neither.
    result = await attach(checker, type_name=type_name, token=None, warehouse_id=None)

    assert result == "unverified"
    assert checker.probes == []


async def test_no_access_refuses_in_the_words_of_the_design_for_a_warehouse() -> None:
    refused = await refusal(StubChecker(CheckVerdict.NO_ACCESS), type_name="sql_warehouse")

    assert refused.code == "no_access"
    assert refused.message == "you do not have access to this SQL warehouse"


async def test_a_secret_the_attacher_cannot_read_names_the_scope_and_not_the_key() -> None:
    refused = await refusal(
        StubChecker(CheckVerdict.NO_ACCESS), type_name="secret", target="sales-secrets/the-key"
    )

    assert refused.code == "no_access"
    assert refused.message == "you do not have READ on secret scope 'sales-secrets'"
    assert "the-key" not in refused.message


@pytest.mark.parametrize("type_name", sorted(VERIFIABLE))
async def test_every_checked_type_refuses_no_access_without_repeating_the_target(
    type_name: str,
) -> None:
    refused = await refusal(StubChecker(CheckVerdict.NO_ACCESS), type_name=type_name)

    assert refused.code == "no_access"
    assert VERIFIABLE[type_name][0] not in refused.message


async def test_a_check_that_could_not_run_is_not_taken_as_a_pass() -> None:
    checker = StubChecker(raises=AttachCheckUnavailable("databricks did not answer; try again"))

    with pytest.raises(AttachCheckUnavailable):
        await attach(checker)


# --- shared_sp accepts only ok -------------------------------------------------------------------

UNVERIFIED_SHARED_SP = (
    "cannot verify access to this resource type; shared_sp requires a verified check"
)


@pytest.mark.parametrize("type_name", sorted(UNVERIFIABLE))
async def test_shared_sp_is_refused_for_a_type_that_cannot_be_checked(type_name: str) -> None:
    checker = StubChecker()

    refused = await refusal(checker, type_name=type_name, access_as="shared_sp")

    assert (refused.code, refused.message) == ("unverified_shared_sp", UNVERIFIED_SHARED_SP)
    assert checker.probes == []


async def test_shared_sp_is_refused_when_the_check_could_only_say_unverified() -> None:
    refused = await refusal(
        StubChecker(CheckVerdict.UNVERIFIED), type_name="uc_table", access_as="shared_sp"
    )

    assert (refused.code, refused.message) == ("unverified_shared_sp", UNVERIFIED_SHARED_SP)


async def test_shared_sp_is_accepted_with_a_verified_check() -> None:
    result = await attach(StubChecker(CheckVerdict.OK), type_name="uc_table", access_as="shared_sp")

    assert result == "ok"


async def test_obo_and_own_sp_accept_unverified() -> None:
    own_sp_app = make_config("sp-id", "sp-secret")
    resources = [make_resource("sp-id"), make_resource("sp-secret")]

    obo = await attach(StubChecker(), type_name="job", access_as="obo")
    own = await attach(
        StubChecker(), type_name="job", access_as="own_sp", config=own_sp_app, resources=resources
    )

    assert (obo, own) == ("unverified", "unverified")


# --- permission and type -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_name", "level"),
    [
        (name, level)
        for name, resource_type in RESOURCE_TYPES.items()
        for level in resource_type.levels
    ],
)
async def test_every_level_of_a_type_is_accepted(type_name: str, level: str) -> None:
    result = await attach(StubChecker(), type_name=type_name, permission=level)

    assert result in ("ok", "unverified")


@pytest.mark.parametrize("type_name", sorted(EVERY_TYPE))
async def test_a_permission_that_is_not_a_level_of_the_type_is_refused_before_any_call(
    type_name: str,
) -> None:
    checker = StubChecker()

    refused = await refusal(checker, type_name=type_name, permission=NOT_A_LEVEL)

    assert refused.code == "bad_permission"
    assert NOT_A_LEVEL not in refused.message
    for level in RESOURCE_TYPES[type_name].levels:
        assert level in refused.message
    assert checker.probes == []


async def test_a_secret_takes_the_level_read_only() -> None:
    refused = await refusal(StubChecker(), type_name="secret", permission="CAN_USE")

    assert refused.code == "bad_permission"


async def test_a_level_of_another_type_is_refused() -> None:
    refused = await refusal(StubChecker(), type_name="sql_warehouse", permission="CAN_QUERY")

    assert refused.code == "bad_permission"


async def test_a_type_the_catalogue_does_not_hold_is_refused() -> None:
    checker = StubChecker()

    refused = await refusal(checker, type_name="made_up_type", permission="READ")

    assert refused.code == "unknown_type"
    assert "made_up_type" not in refused.message
    assert checker.probes == []


# --- access_as obeys the delegation flags --------------------------------------------------------


async def test_obo_needs_the_obo_delegation_of_the_app() -> None:
    checker = StubChecker()

    refused = await refusal(checker, access_as="obo", app=make_app(obo=False))

    assert refused.code == "delegation_missing"
    assert "obo" in refused.message
    assert checker.probes == []


async def test_shared_sp_needs_the_shared_sp_delegation_of_the_app() -> None:
    checker = StubChecker()

    refused = await refusal(checker, access_as="shared_sp", app=make_app(shared_sp=False))

    assert refused.code == "delegation_missing"
    assert "shared_sp" in refused.message
    assert checker.probes == []


async def test_shared_sp_needs_the_platform_switch_even_for_a_delegated_app() -> None:
    checker = StubChecker()

    refused = await refusal(checker, access_as="shared_sp", shared_sp_allowed=False)

    assert refused.code == "delegation_missing"
    assert checker.probes == []


async def test_the_delegation_of_one_identity_does_not_open_another() -> None:
    only_obo = make_app(obo=True, shared_sp=False)
    only_shared = make_app(obo=False, shared_sp=True)

    assert (await refusal(StubChecker(), access_as="shared_sp", app=only_obo)).code == (
        "delegation_missing"
    )
    assert (await refusal(StubChecker(), access_as="obo", app=only_shared)).code == (
        "delegation_missing"
    )


async def test_own_sp_needs_the_own_service_principal_keys_in_the_config() -> None:
    checker = StubChecker()

    refused = await refusal(checker, access_as="own_sp", config=make_config())

    assert refused.code == "own_sp_keys_missing"
    assert "own_sp_client_id_key" in refused.message
    assert checker.probes == []


async def test_own_sp_is_refused_when_a_key_names_no_resource_of_the_app() -> None:
    refused = await refusal(
        StubChecker(),
        access_as="own_sp",
        config=make_config("sp-id", "sp-secret"),
        resources=[make_resource("sp-id")],
    )

    assert refused.code == "own_sp_keys_invalid"
    assert "own_sp_client_secret_key" in refused.message


async def test_own_sp_is_refused_when_a_key_names_a_resource_that_is_not_a_secret() -> None:
    refused = await refusal(
        StubChecker(),
        access_as="own_sp",
        config=make_config("sp-id", "sp-secret"),
        resources=[make_resource("sp-id"), make_resource("sp-secret", "sql_warehouse")],
    )

    assert refused.code == "own_sp_keys_invalid"


async def test_own_sp_is_accepted_when_both_keys_name_secrets_of_the_app() -> None:
    result = await attach(
        StubChecker(CheckVerdict.OK),
        type_name="sql_warehouse",
        access_as="own_sp",
        config=make_config("sp-id", "sp-secret"),
        resources=[make_resource("sp-id"), make_resource("sp-secret")],
    )

    assert result == "ok"


async def test_a_secret_has_no_access_as_and_every_other_type_needs_one() -> None:
    with pytest.raises(AttachRefused) as with_one:
        await verify_attach(
            resource_type="secret",
            target="sales-secrets/k",
            permission="READ",
            access_as="obo",
            app=make_app(),
            config=make_config(),
            resources=(),
            shared_sp_allowed=True,
            warehouse_id=CHECK_WAREHOUSE,
            token=TOKEN,
            checker=StubChecker(),
        )
    without_one = await refusal(StubChecker(), type_name="job", access_as=None)

    assert with_one.value.code == "access_as_invalid"
    assert without_one.code == "access_as_invalid"


# --- the SQL warehouse of the checks -------------------------------------------------------------


@pytest.mark.parametrize("type_name", ["secret", "uc_function"])
async def test_a_check_that_runs_a_statement_needs_the_check_warehouse_setting(
    type_name: str,
) -> None:
    checker = StubChecker()

    refused = await refusal(checker, type_name=type_name, warehouse_id=None)

    assert refused.code == "check_warehouse_unset"
    assert "attach_check_warehouse_id" in refused.message
    assert checker.probes == []


@pytest.mark.parametrize("type_name", ["secret", "uc_function"])
async def test_the_probe_carries_the_check_warehouse_for_the_statement_checks(
    type_name: str,
) -> None:
    checker = StubChecker()

    await attach(checker, type_name=type_name)

    assert checker.probes[0].warehouse_id == CHECK_WAREHOUSE


@pytest.mark.parametrize(
    "type_name", ["sql_warehouse", "serving_endpoint", "uc_table", "genie_space"]
)
async def test_the_other_checks_do_not_need_the_setting(type_name: str) -> None:
    result = await attach(StubChecker(), type_name=type_name, warehouse_id=None)

    assert result == "ok"


# --- the attacher's token must be there ----------------------------------------------------------


@pytest.mark.parametrize("type_name", sorted(VERIFIABLE))
async def test_a_checked_type_with_no_token_is_refused_and_nothing_is_asked(
    type_name: str,
) -> None:
    checker = StubChecker()

    refused = await refusal(checker, type_name=type_name, token=None)

    assert refused.code == "token_missing"
    assert checker.probes == []


# --- a target is of the form its call needs ------------------------------------------------------


@pytest.mark.parametrize(
    ("type_name", "target"),
    [
        ("secret", "no-slash"),
        ("secret", "a/b/c"),
        ("secret", "scope'; DROP TABLE x; --/key"),
        ("secret", "scope/key') OR ('1'='1"),
        ("secret", "sco pe/key"),
        ("secret", "scope/"),
        ("secret", "/key"),
        ("sql_warehouse", "../../etc"),
        ("sql_warehouse", "a b"),
        ("sql_warehouse", "x" * 65),
        ("serving_endpoint", ".."),
        ("serving_endpoint", "a/../../b"),
        ("serving_endpoint", "name?x=1"),
        ("uc_table", "only.two"),
        ("uc_table", "a.b.c.d"),
        ("uc_table", "a.b.c`; DROP TABLE t"),
        ("uc_function", "a.b"),
        ("uc_function", "a.b.c`) --"),
        ("uc_volume", "/Volumes/a/b"),
        ("uc_volume", "/Volumes/a/b/c/../../d"),
        ("uc_volume", "Volumes/a/b/c"),
        ("uc_volume", "a.b.c"),
        ("uc_connection", "a/b"),
        ("genie_space", "id/../x"),
    ],
)
async def test_a_target_that_is_not_the_form_its_check_needs_is_refused_before_any_call(
    type_name: str, target: str
) -> None:
    checker = StubChecker()

    refused = await refusal(checker, type_name=type_name, target=target)

    assert refused.code == "bad_target"
    assert target not in refused.message
    assert checker.probes == []


@pytest.mark.parametrize("type_name", sorted(UNVERIFIABLE))
async def test_a_type_with_no_check_takes_any_printable_target_and_nothing_else(
    type_name: str,
) -> None:
    assert await attach(StubChecker(), type_name=type_name, target="any value: 7/9 (ok)") == (
        "unverified"
    )
    for bad in ("with\nnewline", "x" * 600, "tab\there"):
        assert (await refusal(StubChecker(), type_name=type_name, target=bad)).code == "bad_target"


# --- own-SP keys: the helpers the config route and the detach route share ---


def test_unset_own_sp_keys_are_not_a_problem() -> None:
    assert own_sp_keys_problem(make_config(), ()) is None


def test_own_sp_keys_that_name_secret_resources_are_not_a_problem() -> None:
    config = make_config("sp-id", "sp-secret")

    assert own_sp_keys_problem(config, [make_resource("sp-id"), make_resource("sp-secret")]) is None


def test_an_own_sp_key_with_no_resource_is_a_problem_that_names_the_field() -> None:
    config = make_config("sp-id", "sp-secret")

    problem = own_sp_keys_problem(config, [make_resource("sp-id")])

    assert problem is not None
    assert "own_sp_client_secret_key" in problem


def test_an_own_sp_key_that_names_another_type_is_a_problem() -> None:
    config = make_config("sp-id", "sp-secret")

    problem = own_sp_keys_problem(
        config, [make_resource("sp-id", "job"), make_resource("sp-secret")]
    )

    assert problem is not None
    assert "own_sp_client_id_key" in problem


def test_a_resource_is_in_use_when_an_own_sp_key_names_it() -> None:
    config = make_config("sp-id", "sp-secret")

    assert own_sp_key_in_use(config, "sp-id") is True
    assert own_sp_key_in_use(config, "sp-secret") is True
    assert own_sp_key_in_use(config, "other") is False
    assert own_sp_key_in_use(make_config(), "sp-id") is False


# --- the two platform settings the check reads ---------------------------------------------------


def snapshot_with(*settings: PlatformSetting) -> Any:
    rows: dict[str, list[Any]] = {table: [] for table in SNAPSHOT_TABLES}
    rows["platform_settings"] = list(settings)
    return build_snapshot(rows, loaded_at=NOW)


def test_the_check_warehouse_is_the_setting_and_unset_means_none() -> None:
    assert check_warehouse_id(snapshot_with()) is None
    assert (
        check_warehouse_id(snapshot_with(setting("attach_check_warehouse_id", "wh-check-01")))
        == "wh-check-01"
    )


@pytest.mark.parametrize("value", ["", "   ", 7, True])
def test_a_check_warehouse_setting_that_is_not_usable_text_counts_as_unset(
    value: bool | int | str,
) -> None:
    assert check_warehouse_id(snapshot_with(setting("attach_check_warehouse_id", value))) is None


def test_the_shared_sp_switch_defaults_to_off_and_follows_the_setting() -> None:
    assert shared_sp_delegation_allowed(snapshot_with()) is False
    assert (
        shared_sp_delegation_allowed(snapshot_with(setting("shared_sp_delegation_allowed", True)))
        is True
    )
    assert (
        shared_sp_delegation_allowed(snapshot_with(setting("shared_sp_delegation_allowed", "yes")))
        is False
    )


# --- the real adapter, against a stubbed Databricks ----------------------------------------------

Reply = Callable[[httpx.Request], httpx.Response]


def checker_for(reply: Reply, seen: list[httpx.Request] | None = None, **client: Any) -> Any:
    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return reply(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url=BASE_URL, **client)
    return DatabricksAttachChecker(http)


def probe_of(type_name: str, *, level: str | None = None, bearer: str = "tok-test") -> AttachProbe:
    target, permission = VERIFIABLE[type_name]
    return AttachProbe(
        resource_type=type_name,
        target=target,
        permission=level if level is not None else permission,
        token=SecretStr(bearer),
        warehouse_id=CHECK_WAREHOUSE,
    )


def json_reply(status: int, body: Any = None) -> Reply:
    return lambda _request: httpx.Response(status, json=body if body is not None else {})


def statement_reply(state: str, message: str = "failed") -> Reply:
    body: dict[str, Any] = {"status": {"state": state}}
    if state == "FAILED":
        body["status"]["error"] = {"error_code": "BAD_REQUEST", "message": message}
    return json_reply(200, body)


REST_CALLS: Final = {
    "serving_endpoint": ("GET", "/api/2.0/serving-endpoints/sales-forecast"),
    "uc_table": ("GET", "/api/2.1/unity-catalog/tables/main.sales.orders"),
    "uc_volume": ("GET", "/api/2.0/fs/directories/Volumes/main/sales/raw"),
    "uc_connection": ("GET", "/api/2.1/unity-catalog/connections/crm-connection"),
    "genie_space": ("GET", "/api/2.0/genie/spaces/01ef00000000000000000000000000aa"),
}
STATEMENT_PATH: Final = "/api/2.0/sql/statements"


@pytest.mark.parametrize("type_name", sorted(REST_CALLS))
async def test_a_rest_checked_type_makes_one_read_with_the_attachers_bearer_token(
    type_name: str,
) -> None:
    seen: list[httpx.Request] = []
    checker = checker_for(json_reply(200), seen)

    verdict = await checker.check(probe_of(type_name))

    method, path = REST_CALLS[type_name]
    assert verdict is CheckVerdict.OK
    assert [(r.method, r.url.path) for r in seen] == [(method, path)]
    assert seen[0].headers["Authorization"] == "Bearer tok-test"
    assert seen[0].url.host == "workspace.example.invalid"
    assert seen[0].url.query == b""


@pytest.mark.parametrize("type_name", sorted(REST_CALLS))
async def test_a_refusal_with_no_permission_is_no_access(type_name: str) -> None:
    note = "Principal 'dana' does not have SELECT on Table 'main.sales.orders'."
    checker = checker_for(json_reply(403, {"message": note}))

    assert await checker.check(probe_of(type_name)) is CheckVerdict.NO_ACCESS


@pytest.mark.parametrize("type_name", sorted(REST_CALLS))
async def test_a_missing_object_fails_like_no_access(type_name: str) -> None:
    checker = checker_for(json_reply(404, {"message": "does not exist"}))

    assert await checker.check(probe_of(type_name)) is CheckVerdict.NO_ACCESS


@pytest.mark.parametrize(
    "message",
    [
        "Provided OAuth token does not have required scopes: unity-catalog",
        "Invalid scope, required scopes: sql",
    ],
)
async def test_a_scope_the_app_does_not_declare_is_unverified_not_a_refusal(message: str) -> None:
    checker = checker_for(json_reply(403, {"message": message}))

    assert await checker.check(probe_of("uc_table")) is CheckVerdict.UNVERIFIED


@pytest.mark.parametrize("status", [401, 429, 500, 502, 503, 302])
async def test_an_answer_that_says_nothing_about_access_is_unavailable(status: int) -> None:
    checker = checker_for(json_reply(status))

    with pytest.raises(AttachCheckUnavailable):
        await checker.check(probe_of("uc_table"))


async def test_a_network_failure_is_unavailable_and_never_shows_the_token() -> None:
    def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused for Bearer tok-test")

    checker = checker_for(broken)

    with pytest.raises(AttachCheckUnavailable) as unavailable:
        await checker.check(probe_of("uc_table"))
    # Neither the message nor the exception it was raised after may keep the transport's text.
    shown = f"{unavailable.value} {unavailable.value.__cause__} {unavailable.value.__context__}"
    assert "tok-test" not in shown


async def test_a_redirect_is_not_followed() -> None:
    seen: list[httpx.Request] = []

    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://elsewhere.example.invalid/x"})

    checker = checker_for(redirect, seen, follow_redirects=True)

    with pytest.raises(AttachCheckUnavailable):
        await checker.check(probe_of("uc_table"))
    assert {r.url.host for r in seen} == {"workspace.example.invalid"}


async def test_a_client_that_carries_another_credential_still_sends_the_attachers() -> None:
    seen: list[httpx.Request] = []
    checker = checker_for(
        json_reply(200),
        seen,
        auth=httpx.BasicAuth("service", "placeholder-password"),
        headers={"Authorization": "Bearer placeholder-default-credential"},
    )

    await checker.check(probe_of("uc_table", bearer="tok-of-the-attacher"))

    assert seen[0].headers["Authorization"] == "Bearer tok-of-the-attacher"


async def test_the_token_is_not_logged_on_success_or_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    good = checker_for(json_reply(200))
    bad = checker_for(json_reply(500, {"message": "boom"}))

    await good.check(probe_of("uc_table", bearer="tok-secret-marker"))
    with pytest.raises(AttachCheckUnavailable):
        await bad.check(probe_of("uc_table", bearer="tok-secret-marker"))

    assert "tok-secret-marker" not in caplog.text


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("CAN_VIEW", CheckVerdict.OK),
        ("CAN_QUERY", CheckVerdict.OK),
        ("CAN_MANAGE", CheckVerdict.NO_ACCESS),
    ],
)
async def test_a_serving_endpoint_compares_the_level_the_caller_holds_with_the_one_asked(
    requested: str, expected: CheckVerdict
) -> None:
    checker = checker_for(
        json_reply(200, {"name": "sales-forecast", "permission_level": "CAN_QUERY"})
    )

    verdict = await checker.check(probe_of("serving_endpoint", level=requested))

    assert verdict is expected


async def test_a_serving_endpoint_level_appmux_does_not_know_is_unverified() -> None:
    checker = checker_for(json_reply(200, {"permission_level": "CAN_SOMETHING_NEW"}))

    assert await checker.check(probe_of("serving_endpoint")) is CheckVerdict.UNVERIFIED


async def test_a_serving_endpoint_answer_without_a_level_proves_reach_only() -> None:
    checker = checker_for(json_reply(200, {"name": "sales-forecast"}))

    assert await checker.check(probe_of("serving_endpoint", level="CAN_MANAGE")) is CheckVerdict.OK


async def test_other_types_ignore_a_level_field_in_the_answer() -> None:
    checker = checker_for(json_reply(200, {"permission_level": "CAN_VIEW"}))

    assert await checker.check(probe_of("uc_table", level="MODIFY")) is CheckVerdict.OK


async def test_an_answer_that_is_not_json_still_proves_reach_when_the_status_is_200() -> None:
    checker = checker_for(lambda _request: httpx.Response(200, text="not json"))

    assert await checker.check(probe_of("genie_space")) is CheckVerdict.OK


# the statement checks


def statement_of(request: httpx.Request) -> dict[str, Any]:
    assert request.method == "POST"
    assert request.url.path == STATEMENT_PATH
    return json.loads(request.content)  # type: ignore[no-any-return]


async def test_the_secret_check_asks_for_the_length_of_the_value_never_the_value() -> None:
    seen: list[httpx.Request] = []
    checker = checker_for(statement_reply("SUCCEEDED"), seen)

    verdict = await checker.check(probe_of("secret"))

    body = statement_of(seen[0])
    assert verdict is CheckVerdict.OK
    assert body["statement"] == "SELECT length(secret('sales-secrets', 'openai-api-key'))"
    assert body["warehouse_id"] == CHECK_WAREHOUSE
    assert body["wait_timeout"] == "50s"
    assert body["on_wait_timeout"] == "CANCEL"
    assert seen[0].headers["Authorization"] == "Bearer tok-test"


async def test_the_warehouse_check_is_a_use_test_on_the_warehouse_itself() -> None:
    seen: list[httpx.Request] = []
    checker = checker_for(statement_reply("SUCCEEDED"), seen)

    verdict = await checker.check(probe_of("sql_warehouse"))

    body = statement_of(seen[0])
    assert verdict is CheckVerdict.OK
    assert body["statement"] == "SELECT 1"
    assert body["warehouse_id"] == "a1b2c3d4e5f67890"


async def test_the_function_check_describes_the_function_on_the_check_warehouse() -> None:
    seen: list[httpx.Request] = []
    checker = checker_for(statement_reply("SUCCEEDED"), seen)

    await checker.check(probe_of("uc_function"))

    body = statement_of(seen[0])
    assert body["statement"] == "DESCRIBE FUNCTION `main`.`sales`.`margin`"
    assert body["warehouse_id"] == CHECK_WAREHOUSE


@pytest.mark.parametrize("type_name", ["secret", "sql_warehouse", "uc_function"])
async def test_a_failed_statement_is_no_access_even_though_http_said_200(type_name: str) -> None:
    checker = checker_for(statement_reply("FAILED", "PERMISSION_DENIED: no READ on the scope"))

    assert await checker.check(probe_of(type_name)) is CheckVerdict.NO_ACCESS


@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "CANCELED", "CLOSED"])
async def test_a_statement_that_did_not_finish_is_unavailable(state: str) -> None:
    checker = checker_for(statement_reply(state))

    with pytest.raises(AttachCheckUnavailable) as unavailable:
        await checker.check(probe_of("secret"))
    assert "try again" in str(unavailable.value)


async def test_a_statement_answer_with_no_state_is_unavailable_not_a_pass() -> None:
    checker = checker_for(json_reply(200, {}))

    with pytest.raises(AttachCheckUnavailable):
        await checker.check(probe_of("secret"))


async def test_an_attacher_who_cannot_use_the_check_warehouse_is_told_so() -> None:
    checker = checker_for(json_reply(403, {"message": "no CAN_USE on warehouse"}))

    with pytest.raises(AttachRefused) as refused:
        await checker.check(probe_of("secret"))

    assert refused.value.code == "check_warehouse_denied"
    assert "attach_check_warehouse_id" in refused.value.message


async def test_an_attacher_who_cannot_use_the_warehouse_being_attached_has_no_access() -> None:
    checker = checker_for(json_reply(403, {"message": "no CAN_USE on warehouse"}))

    assert await checker.check(probe_of("sql_warehouse")) is CheckVerdict.NO_ACCESS


async def test_a_statement_scope_the_app_does_not_declare_is_unverified() -> None:
    checker = checker_for(json_reply(403, {"message": "Invalid scope, required scopes: sql"}))

    assert await checker.check(probe_of("secret")) is CheckVerdict.UNVERIFIED


async def test_a_statement_check_with_no_warehouse_is_refused_not_sent() -> None:
    seen: list[httpx.Request] = []
    checker = checker_for(statement_reply("SUCCEEDED"), seen)
    probe = AttachProbe("secret", "sales-secrets/k", "READ", TOKEN, None)

    with pytest.raises(AttachRefused) as refused:
        await checker.check(probe)

    assert refused.value.code == "check_warehouse_unset"
    assert seen == []


async def test_a_type_without_a_check_is_not_something_the_adapter_can_answer() -> None:
    seen: list[httpx.Request] = []
    checker = checker_for(json_reply(200), seen)
    probe = AttachProbe("job", "2222222222", "CAN_VIEW", TOKEN, None)

    with pytest.raises(ValueError, match="no attach check"):
        await checker.check(probe)
    assert seen == []
