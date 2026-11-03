"""How the reconciler starts and stops a child, as small types and one seam (``ChildLauncher``).

Children start only through ``sandbox/launcher.py`` (constitution I; design 6.1). The reconciler
does not call it directly: it asks a ``ChildLauncher`` to start a ``ChildStart`` and holds the
``RunningChild`` that comes back. ``sandbox_adapter.py`` is the one implementation, a thin layer
over ``start_sandbox``; a test passes a fake that starts nothing, which is what lets the build,
start, health and swap steps be tested without namespaces or a process.

A ``ChildStart`` carries the child's environment, which can hold credentials and resolved secrets.
Its ``repr`` lists names only, so a log line that formats it cannot leak a value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol

from appmux.sandbox.rules import EgressRule

# How long the supervisor may take to report that the sandbox runs (the launcher's own steps wait
# up to 20 s for stage 1, 30 s for the network and 20 s for the exec; this is the outer bound).
CHILD_START_TIMEOUT_S: Final = 90.0
# Between asking a child to stop and killing it, when a release is replaced or an app stopped.
CHILD_STOP_GRACE_S: Final = 10.0
# The port every child listens on inside its own network namespace; the relay forwards to it.
DEFAULT_CHILD_PORT: Final = 8000


class ChildStartError(Exception):
    """The child could not be started. The message says which step failed and what to check.

    It never carries an environment value; the launcher's reasons name steps, not content.
    """


@dataclass(frozen=True, slots=True)
class ChildExit:
    """How a child ended, in words fit for a release's ``status_reason``.

    ``code`` (the exit status) or ``signal`` (the name of the signal that ended it, ``SIGKILL``) is
    set when the end was a plain exit; both stay ``None`` when something else ended it first (the
    run-directory watchdog, say), and ``description`` then says what.
    """

    description: str
    code: int | None = None
    signal: str | None = None


# Where a line of a child's output came from; ``appmux`` is a line appmux wrote about the child.
OutputStream = Literal["stdout", "stderr", "appmux"]


@dataclass(frozen=True, slots=True)
class ChildStart:
    """Everything the launcher needs to start one copy of one release."""

    slug: str
    release_no: int
    command: tuple[str, ...]
    environment: Mapping[str, str] = field(repr=False)
    release_dir: Path
    port: int
    memory_budget_mb: int
    overlays: tuple[Path, ...] = ()
    app_egress: tuple[EgressRule, ...] = ()

    def __repr__(self) -> str:
        names = ", ".join(sorted(self.environment))
        return (
            f"ChildStart(slug={self.slug!r}, release_no={self.release_no}, "
            f"command={list(self.command)!r}, environment=<names: {names}>)"
        )


class RunningChild(Protocol):
    """A child that was started: where it listens, how it ends, how to stop it."""

    @property
    def socket_path(self) -> Path:
        """The unix socket the child's relay listens on; the route table points at it."""
        ...

    async def wait_started(self, *, timeout_s: float) -> None:
        """Return once the process runs; raise ``ChildStartError`` if it did not start."""
        ...

    async def wait(self) -> ChildExit:
        """Wait for the child to end and say how."""
        ...

    async def stop(self, *, grace_s: float) -> ChildExit:
        """Ask the child to stop, kill it after ``grace_s``, and return how it ended."""
        ...

    def read_output(self, *, final: bool = False) -> list[tuple[OutputStream, str]]:
        """The complete lines the child wrote since the last call, oldest first, with their stream.

        ``final`` (the child has ended) also returns a last line that has no newline. A line may
        hold anything the child printed, credentials included; the caller masks it.
        """
        ...


class ChildLauncher(Protocol):
    async def start(self, child: ChildStart) -> RunningChild:
        """Start the child and return at once; the caller then waits for it to be ready."""
        ...
