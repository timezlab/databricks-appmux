"""Fakes shared by the reconciler's tests: a clock that never sleeps, a probe, a launcher, a closer.

Nothing here starts a process, opens a socket or reads the time: a test moves the clock by hand
(``FakeClock.sleep`` advances it and yields once), says what the probe answers at each second, and
decides when a fake child exits. That is what makes the 60-second health deadline and the 30-second
drain testable without waiting.

``StubRegistry`` lives in ``tests/contract``; the helper below loads it from there so a unit test
can use the in-memory registry without a second copy of it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import sys
import tarfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from appmux.packages.store import ContentStoreError, MissingFromStore
from appmux.reconciler.build import BuildFailed, BuiltRelease
from appmux.reconciler.children import ChildExit, ChildStart, ChildStartError, OutputStream
from appmux.reconciler.health import ProbeFailed
from appmux.registry.models import Release

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
CONTRACT_DIRECTORY = Path(__file__).resolve().parents[1] / "contract"
SOCKET_DIRECTORY = Path("/run/appmux-example")


def stub_registry_class() -> Any:
    """``StubRegistry`` from ``tests/contract``, loaded once whichever test imported it first."""
    module: ModuleType | None = sys.modules.get("stub_registry")
    if module is None:
        spec = importlib.util.spec_from_file_location(
            "stub_registry", CONTRACT_DIRECTORY / "stub_registry.py"
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["stub_registry"] = module
        spec.loader.exec_module(module)
    return module.StubRegistry


class FakeClock:
    """Time that moves only when something sleeps (or a test sets ``now``)."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class FakeProbe:
    """A health probe that answers from a function of the time, or from a list of answers.

    An answer is an ``int`` (the status) or a string (the probe failed with that description).
    """

    def __init__(
        self,
        clock: FakeClock,
        answer: Callable[[float], int | str]
        | Sequence[int | str]
        | int
        | str = "connection refused",
        *,
        cost_s: float = 0.0,
    ) -> None:
        self._clock = clock
        self._answer = answer
        self._cost_s = cost_s
        self.times: list[float] = []
        self.timeouts: list[float] = []
        self.targets: list[tuple[Path, str]] = []

    async def probe(self, socket_path: Path, health_path: str, *, timeout_s: float) -> int:
        self.times.append(self._clock.now)
        self.timeouts.append(timeout_s)
        self.targets.append((socket_path, health_path))
        answer = self._pick(len(self.times) - 1)
        self._clock.now += self._cost_s
        await asyncio.sleep(0)
        if isinstance(answer, str):
            raise ProbeFailed(answer)
        return answer

    def _pick(self, index: int) -> int | str:
        if callable(self._answer):
            return self._answer(self.times[-1])
        if isinstance(self._answer, int | str):
            return self._answer
        return self._answer[min(index, len(self._answer) - 1)]


class FakeChild:
    """A running child: it exits when ``exit`` is called, and stopping it ends it.

    ``log`` is what it printed to its standard output; ``read_output`` hands each line over once.
    """

    def __init__(self, socket_path: Path, *, log: str = "") -> None:
        self.socket_path = socket_path
        self.log = log
        self.stops: list[float] = []
        self.started_calls = 0
        self._lines_read = 0
        self._ended = asyncio.Event()
        self._exit: ChildExit | None = None

    async def wait_started(self, *, timeout_s: float) -> None:
        self.started_calls += 1

    async def wait(self) -> ChildExit:
        await self._ended.wait()
        assert self._exit is not None
        return self._exit

    async def stop(self, *, grace_s: float) -> ChildExit:
        self.stops.append(grace_s)
        if not self._ended.is_set():
            self.exit("was stopped")
        return await self.wait()

    def read_output(self, *, final: bool = False) -> list[tuple[OutputStream, str]]:
        lines = self.log.splitlines()
        new, self._lines_read = lines[self._lines_read :], len(lines)
        return [("stdout", line) for line in new]

    def exit(
        self,
        description: str = "exited with status 1",
        *,
        code: int | None = None,
        signal: str | None = None,
    ) -> None:
        self._exit = ChildExit(description, code=code, signal=signal)
        self._ended.set()

    @property
    def is_running(self) -> bool:
        return not self._ended.is_set()


class FakeLauncher:
    """Starts ``FakeChild`` objects; ``fail_with`` makes the starts raise ``ChildStartError``."""

    def __init__(self) -> None:
        self.started: list[ChildStart] = []
        self.children: list[FakeChild] = []
        self.fail_with: str | None = None
        self.log_of_next_child = ""
        self.events: list[str] = []

    async def start(self, child: ChildStart) -> FakeChild:
        self.events.append(f"start {child.slug} v{child.release_no}")
        if self.fail_with is not None:
            raise ChildStartError(self.fail_with)
        self.started.append(child)
        running = FakeChild(
            SOCKET_DIRECTORY / f"{child.slug}-{child.release_no}.sock", log=self.log_of_next_child
        )
        self.children.append(running)
        return running

    def child_of(self, slug: str, release_no: int) -> FakeChild:
        for started, running in zip(self.started, self.children, strict=True):
            if started.slug == slug and started.release_no == release_no:
                return running
        raise AssertionError(f"{slug} v{release_no} was never started")


class FakeBuilder:
    """Builds nothing: it answers a fixed ``BuiltRelease`` or raises the failure it was told to."""

    def __init__(self, release_dir: Path | None = None) -> None:
        self.release_dir = release_dir or Path("/srv/releases-example/app")
        self.built: list[tuple[str, int]] = []
        self.fail: BuildFailed | None = None

    async def build(self, release: Release) -> BuiltRelease:
        self.built.append((release.app_slug, release.release_no))
        if self.fail is not None:
            raise self.fail
        return BuiltRelease(release_dir=self.release_dir)


class FakeEnvironment:
    """Child environments that name the app and release; ``fail_with`` simulates a missing key."""

    def __init__(self) -> None:
        self.fail_with: Exception | None = None
        self.environment: Mapping[str, str] = {}

    async def environment_for(self, release: Release, *, port: int) -> Mapping[str, str]:
        if self.fail_with is not None:
            raise self.fail_with
        return {
            "APPMUX_TEST_RELEASE": f"{release.app_slug}-{release.release_no}",
            **self.environment,
        }


class FakeCloser:
    """Records the connections it was asked to close, with the time on the fake clock."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.calls: list[tuple[str, int, float]] = []

    async def close_connections(self, slug: str, release_no: int) -> None:
        self.calls.append((slug, release_no, self._clock.now))


def archive_bytes(entries: Mapping[str, bytes | tarfile.TarInfo]) -> bytes:
    """An uncompressed tar of ``entries``: name to content, or to a ready ``TarInfo`` (a link)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in entries.items():
            if isinstance(content, tarfile.TarInfo):
                content.name = name
                archive.addfile(content)
            else:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class FakeArchiveStore:
    """The content store's ``get``: archives by digest, or the error it was told to raise."""

    def __init__(self) -> None:
        self.archives: dict[str, bytes] = {}
        self.error: ContentStoreError | None = None
        self.gets: list[str] = []

    async def get(self, digest: str, destination: Path) -> None:
        self.gets.append(digest)
        if self.error is not None:
            raise self.error
        if digest not in self.archives:
            raise MissingFromStore(f"{digest} is not in the content store")
        await asyncio.to_thread(destination.write_bytes, self.archives[digest])


# --- registry rows -------------------------------------------------------------------------------


def digest_of(number: int) -> str:
    return f"sha256:{number:064x}"


def seed_app(
    registry: Any,
    slug: str = "sales",
    *,
    desired_state: str = "running",
    target_release_no: int | None = None,
    memory_budget_mb: int = 256,
    health_deadline_s: int = 60,
    health_path: str = "/",
) -> None:
    """An app of the starting data: owned by alice, with its ``app_config`` row."""
    registry.seed(
        "apps",
        {
            "slug": slug,
            "display_name": slug.title(),
            "owner": "alice@example.com",
            "source_path": f"/Workspace/Shared/apps/{slug}",
            "memory_budget_mb": memory_budget_mb,
            "created_by": "dana@example.com",
            "created_at": NOW,
            "updated_by": "dana@example.com",
            "updated_at": NOW,
        },
    )
    registry.seed(
        "app_config",
        {
            "app_slug": slug,
            "desired_state": desired_state,
            "target_release_no": target_release_no,
            "health_path": health_path,
            "health_deadline_s": health_deadline_s,
            "updated_by": "dana@example.com",
            "updated_at": NOW,
        },
    )


def seed_release(
    registry: Any,
    slug: str,
    release_no: int,
    status: str = "pending",
    *,
    command: Sequence[str] = ("python", "app.py"),
    status_reason: str | None = None,
    deleted: bool = False,
) -> None:
    fields: dict[str, Any] = {
        "app_slug": slug,
        "release_no": release_no,
        "snapshot_digest": None if deleted else digest_of(release_no),
        "detected": {"runtime": "python", "command": list(command)},
        "status": status,
        "status_reason": status_reason,
        "status_at": NOW,
        "created_by": "alice@example.com",
        "created_at": NOW,
    }
    if deleted:
        fields.update(deleted_by="alice@example.com", deleted_at=NOW)
    registry.seed("releases", fields)


def status_of(registry: Any, slug: str, release_no: int) -> str:
    release = registry.snapshot.release(slug, release_no)
    assert release is not None, f"{slug} v{release_no} is not in the registry"
    return str(release.status)
