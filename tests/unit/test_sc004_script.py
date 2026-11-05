"""The parts of the SC-004 walkthrough script that need no container (T167).

``tests/integration/test_sc004_walkthrough.py`` does every step of SC-004 against a running appmux,
so it is skipped here (``tests/conftest.py`` skips everything under ``tests/integration``). That
leaves a script nobody has run, so what can be checked without a container is checked here: every
request body is valid for its contract model, the audit actions the script expects exist and cover
the criterion, the slugs of a run are valid, and the order check fails when it should. The file is
loaded by path because the test directories are not packages.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from appmux.registry.audit import AUDIT_ACTIONS
from appmux.registry.models import SLUG_PATTERN

SCRIPT = Path(__file__).resolve().parents[1] / "integration" / "test_sc004_walkthrough.py"
MODULE_NAME = "sc004_walkthrough_script"

# One audit action per step of SC-004 that writes a row (spec, SC-004).
ACTIONS_THE_CRITERION_NEEDS = frozenset(
    {
        "app.register",
        "request.create",
        "request.approve",
        "request.deny",
        "app.rename",
        "settings.edit",
        "egress.edit",
        "app.view_as_user",
        "release.view_source",
        "grant.app",
        "resource.attach",
        "release.create",
        "config.edit",
        "release.tag",
        "release.label",
        "release.lock",
        "release.unlock",
        "release.activate",
        "release.delete",
        "app.restart",
        "app.stop",
    }
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A dataclass with string annotations looks its module up by name while it is being defined.
    sys.modules[MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[MODULE_NAME]
        raise
    return module


script = _load_script()


def _expected_actions_in_source() -> list[str]:
    """Every string the script passes to ``expect_audit``, read from its source."""
    found: list[str] = []
    for node in ast.walk(ast.parse(SCRIPT.read_text(encoding="utf-8"))):
        is_call = isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        if is_call and node.func.attr == "expect_audit":
            found.extend(
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            )
    return found


def test_every_body_of_the_walkthrough_fits_its_contract_model() -> None:
    config = {
        "version": 1,
        "env": [{"name": "LOG_LEVEL", "value": "info", "value_from": None}],
        "wheel_sources": [],
        "health_path": "/",
        "health_deadline_s": 60,
        "own_sp_client_id_key": None,
        "own_sp_client_secret_key": None,
    }
    folder = "/Workspace/Shared/fixtures/light_fastapi"
    secret_offer = {"type": "secret", "permission": "READ"}
    warehouse_offer = {"type": "sql_warehouse", "permission": "CAN_USE"}

    bodies = [
        script.register_body(
            slug="walk-direct-0a1b2c", name="Direct", folder=folder, owner=script.GRACE
        ),
        script.register_request_body(slug="walk-asked-0a1b2c", name="Asked", folder=folder),
        script.request_body(
            "delegation", "walk-asked-0a1b2c", {"obo": True}, "needs the visitor's data"
        ),
        script.request_body(
            "delegation", "walk-asked-0a1b2c", {"shared_sp": True}, "a nightly job"
        ),
        script.request_body(
            "settings",
            "walk-asked-0a1b2c",
            {"display_name": "Reports", "memory_budget_mb": script.RAISED_BUDGET_MB},
            "a better name and room to grow",
        ),
        script.request_body(
            "settings", "walk-asked-0a1b2c", {"slug": "walk-renamed-0a1b2c"}, "rename"
        ),
        script.decision_body(reason="the walkthrough approves", note="ok"),
        script.setting_body(True),
        script.egress_body(version=3),
        script.grant_body("user", script.SHARED_USER, "CAN_USE"),
        script.grant_body("group", "Walkthrough Group", "CAN_USE"),
        script.grant_body("user", script.APP_ADMIN, "CAN_MANAGE"),
        script.open_mode_body("as_user"),
        script.attach_body(secret_offer, target="walkthrough-scope/openai-api-key"),
        script.attach_body(warehouse_offer, target="0000000000000000"),
        script.release_body(name="first", tags=["stable"]),
        script.config_body(config=config, extra_env={"WALKTHROUGH_MARK": "two"}),
    ]

    assert all(isinstance(body, dict) and body for body in bodies)


def test_a_secret_is_attached_without_access_as_and_every_other_type_with_one() -> None:
    secret = script.attach_body({"type": "secret", "permission": "READ"}, target="scope/key")
    warehouse = script.attach_body(
        {"type": "sql_warehouse", "permission": "CAN_USE"}, target="0000000000000000"
    )

    assert secret["access_as"] is None and warehouse["access_as"] == "obo"
    assert secret["from_bundle"] is True and warehouse["from_bundle"] is True


def test_every_audit_action_the_script_expects_exists() -> None:
    expected = _expected_actions_in_source()

    assert expected, "the script must expect audit rows"
    assert set(expected) <= set(AUDIT_ACTIONS), sorted(set(expected) - set(AUDIT_ACTIONS))


def test_the_script_expects_an_audit_row_for_each_step_of_the_criterion() -> None:
    expected = set(_expected_actions_in_source())

    missing = sorted(ACTIONS_THE_CRITERION_NEEDS - expected)

    assert not missing, missing


def test_the_slugs_of_a_run_are_valid_and_differ_from_each_other_and_from_another_run() -> None:
    first, second = script.Slugs.fresh(), script.Slugs.fresh()
    names = [first.direct, first.asked, first.renamed, first.taken, first.bundle]

    assert all(re.fullmatch(SLUG_PATTERN, name) for name in names)
    assert len(set(names)) == len(names)
    assert not set(names) & {second.direct, second.asked, second.renamed, second.taken}


def test_the_order_check_allows_rows_between_and_refuses_a_missing_or_swapped_row() -> None:
    log = ["app.register", "grant.app", "request.create", "request.approve", "app.stop"]

    script.assert_in_order(log, ["app.register", "request.create", "app.stop"])
    with pytest.raises(AssertionError, match=r"row 2 of 2 \(app.register\)"):
        script.assert_in_order(log, ["request.approve", "app.register"])
    with pytest.raises(AssertionError, match=r"row 1 of 1 \(release.lock\)"):
        script.assert_in_order(log, ["release.lock"])


def test_a_call_that_hears_the_token_back_fails_and_a_clean_one_passes() -> None:
    """The token must never be in an answer; the check that says so is the script's own."""
    import httpx

    token = "tok-test"  # noqa: S105 - a placeholder, not a credential
    target = script.Target(
        base_url="http://appmux.invalid",
        admin_email="dana@example.com",
        user_token=token,
        fixtures_folder="/Workspace/Shared/fixtures",
        group="Walkthrough Group",
        scope_and_key="scope/key",
        warehouse_id="0000000000000000",
    )

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"echo": request.url.path})

    def echo_token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"leak": request.headers["x-forwarded-access-token"]})

    with httpx.Client(transport=httpx.MockTransport(answer), base_url=target.base_url) as client:
        walk = script.Walkthrough(client, target)
        assert walk.read(target.admin_email, "/me") == {"echo": "/_appmux/api/v1/me"}
    with httpx.Client(
        transport=httpx.MockTransport(echo_token), base_url=target.base_url
    ) as client:
        walk = script.Walkthrough(client, target)
        with pytest.raises(AssertionError, match="repeated the access token") as caught:
            walk.call(target.admin_email, "GET", "/me")
    assert token not in str(caught.value)


def test_the_admins_token_travels_with_every_call_of_the_admin_and_with_nobody_elses() -> None:
    """The Apps proxy puts the token on every request of the person it belongs to.

    The first three container runs (2026-11-05) each stopped at a call that the service answers
    ``403`` without the admin's token (register, a platform setting, an app's egress); a flag per
    call kept missing one, so the token goes with every call of the admin, and only theirs.
    """
    import httpx

    token = "tok-test"  # noqa: S105 - a placeholder, not a credential
    target = script.Target(
        base_url="http://appmux.invalid",
        admin_email="dana@example.com",
        user_token=token,
        fixtures_folder="/Workspace/Shared/fixtures",
        group="Walkthrough Group",
        scope_and_key="scope/key",
        warehouse_id="0000000000000000",
    )
    seen: list[tuple[str, str | None]] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.headers["x-forwarded-email"], request.headers.get("x-forwarded-access-token"))
        )
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(record), base_url=target.base_url) as client:
        walk = script.Walkthrough(client, target)
        walk.call(target.admin_email, "GET", "/me")
        walk.call(target.admin_email, "PUT", "/platform/settings/x", json={"value": True})
        walk.call(script.GRACE, "GET", "/me")

    assert seen == [(target.admin_email, token), (target.admin_email, token), (script.GRACE, None)]


def test_clean_up_waits_for_a_slow_delete_and_a_timeout_does_not_strand_the_other_apps() -> None:
    """Deleting a running app takes about 33 s (TD-031), longer than a call's 30 s.

    The fourth container run (2026-11-05) passed all ten steps, then its clean-up raised
    ``ReadTimeout`` on the first delete and left the app after it in the registry.
    """
    import httpx

    target = script.Target(
        base_url="http://appmux.invalid",
        admin_email="dana@example.com",
        user_token="tok-test",  # noqa: S106 - a placeholder, not a credential
        fixtures_folder="/Workspace/Shared/fixtures",
        group="Walkthrough Group",
        scope_and_key="scope/key",
        warehouse_id="0000000000000000",
    )
    asked: list[tuple[str, float | None]] = []

    def slow_then_fast(request: httpx.Request) -> httpx.Response:
        asked.append((request.url.path, request.extensions["timeout"]["read"]))
        if request.url.path.endswith("/second"):
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(204)

    with httpx.Client(
        transport=httpx.MockTransport(slow_then_fast), base_url=target.base_url
    ) as client:
        walk = script.Walkthrough(client, target)
        walk.created_apps.extend(["first", "second"])
        with pytest.warns(UserWarning, match="could not delete the walkthrough app second"):
            walk.delete_created_apps()

    # The newest app is deleted first; its timeout is a warning, and the older one is still asked.
    assert [path for path, _ in asked] == [f"{script.API}/apps/second", f"{script.API}/apps/first"]
    assert {read for _, read in asked} == {script.DELETE_TIMEOUT_S}
