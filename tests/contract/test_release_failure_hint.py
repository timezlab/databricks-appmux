"""``ReleaseDetail.failure_hint``: one sentence on what to change for a release that failed (T190).

The hint is judged when the release is read, from the stored reason, the stored tail, the detected
runtime and the app's memory budget now (``reconciler/start_failure.py``); the seeded app ``sales``
has a 256 MB budget, below what Node.js needs.
"""

from __future__ import annotations

from typing import Final

from edge_harness import CAROL, NOW, AdminApi, seeded_registry
from pydantic import SecretStr
from stub_registry import StubRegistry

from appmux.admin.releases import ReleaseRoutes
from appmux.admin.schemas import ReleaseDetail
from appmux.identity.actor import Actor
from appmux.reconciler.ports import ChangeWriter, UpdateResult
from appmux.reconciler.runtime_limits import NODE_MIN_MEMORY_MB

DIGEST: Final = "sha256:" + "cd" * 32
V8_OUT_OF_MEMORY: Final = (
    "#\n# Fatal process out of memory: Failed to reserve virtual memory for CodeRange\n#"
)
HEALTH_TIMEOUT: Final = "health check did not pass within 60 s (last error: connection refused)"


class NoUpdater:
    async def update(
        self, *, actor: Actor, app_slug: str, user_token: SecretStr, write: ChangeWriter
    ) -> UpdateResult:
        raise AssertionError("reading a release started an Update")


def registry_with(
    *, runtime: str, status: str, reason: str | None, tail: str | None
) -> StubRegistry:
    registry = seeded_registry()
    registry.seed(
        "releases",
        {
            "app_slug": "sales",
            "release_no": 1,
            "snapshot_digest": DIGEST,
            "detected": {"runtime": runtime, "command": ["npm", "run", "start"]},
            "status": status,
            "status_reason": reason,
            "build_log_tail": tail,
            "status_at": NOW,
            "created_by": CAROL,
            "created_at": NOW,
        },
    )
    return registry


def hint_of(registry: StubRegistry) -> str | None:
    api = AdminApi(registry, lambda authorizer: [ReleaseRoutes(NoUpdater())])
    reply = api.call("GET", "/apps/sales/releases/1", CAROL)
    assert reply.status_code == 200, reply.text
    return ReleaseDetail.model_validate_json(reply.content).failure_hint


def test_a_node_release_killed_by_v8s_out_of_memory_error_gets_the_memory_hint() -> None:
    registry = registry_with(
        runtime="node",
        status="failed",
        reason="the app was stopped by signal SIGTRAP after 1.2 s",
        tail=V8_OUT_OF_MEMORY,
    )

    hint = hint_of(registry)

    assert hint is not None
    assert f"about {NODE_MIN_MEMORY_MB} MB" in hint
    assert "2 times" in hint


def test_a_silent_node_timeout_below_the_minimum_gets_the_hint_from_the_apps_budget() -> None:
    registry = registry_with(runtime="node", status="failed", reason=HEALTH_TIMEOUT, tail=None)

    assert hint_of(registry) is not None


def test_a_crash_that_is_not_about_memory_has_no_hint() -> None:
    registry = registry_with(
        runtime="python",
        status="failed",
        reason="the app exited with code 1 after 0.5 s",
        tail="Traceback (most recent call last):\nValueError: no config",
    )

    assert hint_of(registry) is None


def test_a_release_that_did_not_fail_has_no_hint() -> None:
    registry = registry_with(runtime="node", status="active", reason=None, tail=None)

    assert hint_of(registry) is None
