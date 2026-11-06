"""The outside world of the running service, replaced, for the tests of its wiring (T095).

``Service`` (``appmux/assembly.py``) and the real wiring of ``__main__`` reach three things no test
may touch: the workspace over the network, the container's memory limit and address, and the
reconciler that starts sandboxes. This module gives them recording stand-ins and one place that
installs them, so a test that runs the real start-up opens no connection and starts no process.

``log`` is one list a test shares between the doubles and its own fakes; reading it afterwards
shows in what order things happened.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, ClassVar, Final

import httpx
import pytest
from edge_harness import NOW, seed_standard_rows
from stub_registry import StubRegistry

import appmux.assembly as assembly
from appmux import __main__ as entry
from appmux.reconciler.admission import MemoryLedger
from appmux.reconciler.output import OutputRing
from appmux.reconciler.sandbox_adapter import RULES_FILE_NAME, SandboxRunningChild

CONTAINER_IP: Final = IPv4Address("10.0.2.15")
MEMORY_LIMIT_MB: Final = 8192
RECORDED_LEDGER: Final = MemoryLedger(limit_mb=MEMORY_LIMIT_MB, reserve_mb=512, committed_mb=269)
RECORDED_RING: Final = OutputRing()
SERVICE_PRINCIPAL_TOKEN: Final = "service-principal-token-placeholder"  # noqa: S105 - a fake value


class Closer:
    """A ``ConnectionCloser`` with nothing open."""

    async def close_connections(self, slug: str, release_no: int) -> None:
        return None


class Workspace:
    """A workspace that answers every call with ``status`` and keeps what it was asked."""

    def __init__(self, status: int = 200, *, log: list[str] | None = None) -> None:
        self.status = status
        self.log = log if log is not None else []
        self.requests: list[httpx.Request] = []
        self.client = httpx.AsyncClient(
            base_url="https://workspace.example.invalid",
            transport=httpx.MockTransport(self._answer),
        )

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.log.append(f"workspace {request.method} {request.url.path}")
        return httpx.Response(self.status, json={})


class RecordingReconciler:
    """Stands in for ``Reconciler``: keeps the arguments it was built with and what was called."""

    instances: ClassVar[list[RecordingReconciler]] = []

    def __init__(self, *, log: list[str] | None = None, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.log = log if log is not None else []
        self.calls: list[tuple[str, Any]] = []
        self.stop_error: Exception | None = None
        # What the metrics sampler reads; a test sets them.
        self.children: list[tuple[str, Any]] = []
        self.app_count = 0
        # Why the last copy started beside the running one did not come up, per app (T220).
        self.restart_failures: dict[str, str] = {}
        RecordingReconciler.instances.append(self)

    async def start(self) -> None:
        self.calls.append(("start", None))
        self.log.append("reconciler start")

    def wake(self, slug: str) -> None:
        self.calls.append(("wake", slug))

    def run_state(self, slug: str, desired_state: str) -> str:
        self.calls.append(("run_state", (slug, desired_state)))
        return "crashed"

    def last_restart_failure(self, slug: str) -> str | None:
        self.calls.append(("last_restart_failure", slug))
        return self.restart_failures.get(slug)

    def request_restart(self, slug: str) -> None:
        self.calls.append(("request_restart", slug))

    def app_renamed(self, old_slug: str, new_slug: str) -> None:
        self.calls.append(("app_renamed", (old_slug, new_slug)))

    async def stop_app(self, slug: str) -> None:
        self.calls.append(("stop_app", slug))

    def memory_ledger(self) -> MemoryLedger:
        self.calls.append(("memory_ledger", None))
        return RECORDED_LEDGER

    def running_children(self) -> list[tuple[str, Any]]:
        return list(self.children)

    def running_app_count(self) -> int:
        return self.app_count

    def output(self, slug: str) -> OutputRing | None:
        self.calls.append(("output", slug))
        return RECORDED_RING

    async def stop_all(self, within_s: float) -> None:
        self.calls.append(("stop_all", within_s))
        self.log.append("reconciler stop_all")
        if self.stop_error is not None:
            raise self.stop_error


class RulesFileSandbox:
    """A running sandbox whose supervisor reads its rules file when signalled, as the pump does.

    ``rules_seen`` holds what the file said at each signal, so a test sees both the content and
    that it was written before the signal.
    """

    sandbox_pid: int | None = 4242

    def __init__(self, rules_path: Path) -> None:
        self._rules_path = rules_path
        self.rules_seen: list[dict[str, Any]] = []

    def reload_rules(self) -> bool:
        self.rules_seen.append(json.loads(self._rules_path.read_text(encoding="utf-8")))
        return True

    async def wait_started(self, *, timeout_s: float) -> None:
        return None

    async def wait(self) -> Any:
        raise AssertionError("not used")

    async def stop(self, *, grace_s: float) -> Any:
        raise AssertionError("not used")

    async def read_stats(self, *, timeout_s: float) -> Any:
        return None


def sandbox_with_rules(
    run_base: Path, slug: str, instance: str
) -> tuple[SandboxRunningChild, RulesFileSandbox]:
    """A running sandbox child with a scratch directory of its own, and its supervisor."""
    scratch = run_base / slug / instance
    scratch.mkdir(parents=True)
    process = RulesFileSandbox(scratch / RULES_FILE_NAME)
    child = SandboxRunningChild(
        process,
        socket_path=scratch / "relay.sock",
        log_path=scratch / "child.log",
        stderr_path=scratch / "child.err",
        memory_budget_mb=256,
        rules_path=scratch / RULES_FILE_NAME,
    )
    return child, process


class ManagedRegistry(StubRegistry):
    """The in-memory registry with the two extras ``start_application`` needs, and a close log."""

    def __init__(self, *, log: list[str] | None = None) -> None:
        super().__init__(clock=lambda: NOW)
        self.log = log if log is not None else []
        self.is_closed = False

    async def poll(self) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.is_closed = True
        self.log.append("registry closed")


def managed_registry(log: list[str]) -> ManagedRegistry:
    """A managed registry holding the standard rows of ``seeded_registry``."""
    return seed_standard_rows(ManagedRegistry(log=log))


@dataclass(slots=True)
class ServiceDoubles:
    log: list[str] = field(default_factory=list)
    workspace: Workspace = field(init=False)

    def __post_init__(self) -> None:
        self.workspace = Workspace(log=self.log)

    @property
    def reconcilers(self) -> list[RecordingReconciler]:
        return RecordingReconciler.instances


def install_reconciler(monkeypatch: pytest.MonkeyPatch, *, log: list[str] | None = None) -> None:
    """``Service`` builds a ``RecordingReconciler`` instead of a ``Reconciler``."""
    RecordingReconciler.instances = []

    def build(**kwargs: Any) -> RecordingReconciler:
        return RecordingReconciler(log=log, **kwargs)

    monkeypatch.setattr(assembly, "Reconciler", build)


def install_service_doubles(monkeypatch: pytest.MonkeyPatch) -> ServiceDoubles:
    """Make the real wiring of ``__main__`` touch nothing outside the process.

    The workspace client and the token for it, the container's address and memory limit and the
    reconciler are replaced; everything else in the wiring stays real.
    """
    doubles = ServiceDoubles()
    install_reconciler(monkeypatch, log=doubles.log)
    monkeypatch.setattr(assembly, "read_container_memory_limit_mb", lambda: MEMORY_LIMIT_MB)
    monkeypatch.setattr(entry, "_workspace_client", lambda settings: doubles.workspace.client)
    monkeypatch.setattr(
        entry, "_service_principal_token", lambda settings: lambda: SERVICE_PRINCIPAL_TOKEN
    )
    monkeypatch.setattr(entry, "_container_ip", lambda: CONTAINER_IP)
    return doubles
