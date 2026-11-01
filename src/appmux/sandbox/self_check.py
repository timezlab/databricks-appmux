"""``self_check()``: prove the sandbox holds before any child runs (design 6.4, FR-013).

A *canary* (``sandbox/canary.py``) is a short program that runs in a real sandbox and tries to do
what a child must not be able to do. It prints ONE JSON object, one entry per check of design 6.4::

    {"checks": [{"id": "pid_isolation", "passed": true, "detail": ""}, ...]}

``self_check`` takes the text from an injected runner (the code that starts the sandbox and collects
the canary's output; this module does not import it, so it is testable without a container) and
builds the result. It fails closed in every way a result can be unreadable:

* any failed check fails the whole result;
* the output must list exactly the checks in ``CHECKS``: one that is missing, repeated or unknown,
  an entry of the wrong shape, an empty list and text that is not a single JSON object are all a
  failed result, never a pass;
* a canary that cannot start, does not finish in ``timeout_s`` or raises fails the result too. Such
  a result has no per-check entries; it has one failed entry named ``CANARY_CHECK_NAME`` that says
  why.

The result names its failed checks by fixed ids (``failed_names``). The edge shows only those ids on
its status page: a ``detail`` is text from a program that runs in the thing being tested, so it goes
to the platform admin's page and the log, never to a visitor.

``SelfCheckState`` is what the rest of appmux shares: the start-up runs it once, the edge asks it
whether children may be served, and ``POST /platform/self-check`` runs it again on demand.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, ValidationError

logger = logging.getLogger(__name__)

# The checks of design 6.4 in the order the canary runs them, each with what a child must not be
# able to do. The ids are the names the platform page and the status page show, and the keys of the
# canary's output; the CPU pin is the check design 6.4 adds under "Known gaps". Defined here once:
# the canary, the status page and the tests all read this table.
CHECKS: Final[Mapping[str, str]] = {
    "environ_isolation": "read the environment of appmux, of PID 1 or of a sibling sandbox",
    "pid_isolation": "see any process but its own",
    "loopback_and_container_ip": (
        "reach appmux's loopback ports, or any port of the container's own address"
    ),
    "metadata_address": "reach the metadata address 169.254.169.254",
    "platform_files": "read the platform's files and secrets",
    "capabilities": "regain a capability (mount, ip link, a raw socket)",
    "read_only_environment": "write to the app environment or to an overlay",
    "sibling_sockets": "reach a sibling's socket, run directory or release",
    "appmux_directories": "reach appmux's directories or another sandbox's temporary directories",
    "sysv_ipc": "see the shared memory or message queues of another process",
    "nested_user_namespace": "create a nested user namespace",
    "inherited_descriptors": "inherit an open file descriptor beyond 0, 1 and 2",
    "cpu_pin": "run on a CPU other than the one it is pinned to",
}
CHECK_IDS: Final = tuple(CHECKS)

# The one entry of a result that has no per-check answers (the canary could not run, timed out, or
# printed something unreadable).
CANARY_CHECK_NAME: Final = "canary"
_CANARY_DESCRIPTION: Final = "produce a trustworthy result"
_UNKNOWN_DESCRIPTION: Final = "pass a check this build does not know"

# A canary prints about a kilobyte; a megabyte is a runaway program, not a longer answer.
CANARY_OUTPUT_MAX_BYTES: Final = 64 * 1024
# The platform page shows a detail on one line; anything longer is cut.
DETAIL_MAX_LENGTH: Final = 200
# The canary tries every listening port of the container's address with a connect timeout (28 ports
# on Free Edition), so it takes seconds; this only stops a hung sandbox from holding the start-up.
CANARY_TIMEOUT_S: Final = 120.0

CanaryRunner = Callable[[], Awaitable[str]]


def describe_check(name: str) -> str:
    """What a child must not be able to do, for ``name``; a fixed text for any name."""
    if name == CANARY_CHECK_NAME:
        return _CANARY_DESCRIPTION
    return CHECKS.get(name, _UNKNOWN_DESCRIPTION)


class CanaryUnavailable(Exception):
    """The canary could not be run at all (no sandbox could start, no runner is configured).

    The message becomes the ``detail`` of the failed result and reaches the platform page and the
    log, so the code that raises it writes it for a person and copies no output of the canary,
    of a subprocess or of the system into it.
    """


class CanaryOutputRefused(ValueError):
    """The canary's text is not exactly the full list of checks; the message says what is wrong.

    The message is fixed text and the id of a check that this build knows; it never repeats what the
    canary printed.
    """


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check: its id, whether it passed, and a short text for the platform admin."""

    name: str
    passed: bool
    detail: str | None


@dataclass(frozen=True, slots=True)
class SelfCheckReport:
    """The result of one run. ``passed`` is not stored: it follows from ``checks``.

    A report passes only when it holds every check of ``CHECKS`` exactly once and all of them
    passed, so a report built any other way (no checks, a missing one) cannot pass.
    """

    ran_at: datetime
    duration_ms: int
    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        names = sorted(check.name for check in self.checks)
        return names == sorted(CHECK_IDS) and all(check.passed for check in self.checks)

    @property
    def failed_names(self) -> tuple[str, ...]:
        """The ids of the failed checks, in run order; fixed ids only, never a detail."""
        return tuple(check.name for check in self.checks if not check.passed)


class _CanaryEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    id: StrictStr
    passed: StrictBool
    detail: StrictStr = ""


class _CanaryOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    checks: list[_CanaryEntry]


def clean_detail(text: str) -> str | None:
    """``text`` on one printable line of at most ``DETAIL_MAX_LENGTH`` characters, or ``None``."""
    printable = "".join(character if character.isprintable() else " " for character in text)
    return " ".join(printable.split())[:DETAIL_MAX_LENGTH] or None


def parse_canary_output(text: str) -> tuple[CheckResult, ...]:
    """The results in ``text``, in the order the canary printed them; ``CanaryOutputRefused`` if
    the text is not one JSON object that lists every check of ``CHECKS`` exactly once.
    """
    if not isinstance(text, str):
        raise CanaryOutputRefused("the canary printed no text")
    if len(text.encode("utf-8", errors="replace")) > CANARY_OUTPUT_MAX_BYTES:
        raise CanaryOutputRefused(f"the canary printed more than {CANARY_OUTPUT_MAX_BYTES} bytes")
    try:
        output = _CanaryOutput.model_validate_json(text)
    except ValidationError as exc:
        # Pydantic's own text can repeat what was printed, so it is not used.
        is_valid_json = all(error["type"] != "json_invalid" for error in exc.errors())
        raise CanaryOutputRefused(
            "the canary's output is not the expected list of checks"
            if is_valid_json
            else "the canary did not print one JSON object"
        ) from None
    names = [entry.id for entry in output.checks]
    if not names:
        raise CanaryOutputRefused("the canary reported no checks")
    if any(name not in CHECKS for name in names):
        raise CanaryOutputRefused("the canary reported a check this build does not know")
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise CanaryOutputRefused(f"the canary reported the check {repeated[0]} more than once")
    missing = [check_id for check_id in CHECK_IDS if check_id not in names]
    if missing:
        raise CanaryOutputRefused(f"the canary did not report: {', '.join(missing)}")
    return tuple(
        CheckResult(entry.id, entry.passed, clean_detail(entry.detail)) for entry in output.checks
    )


def _canary_failed(reason: str) -> tuple[CheckResult, ...]:
    return (CheckResult(CANARY_CHECK_NAME, False, clean_detail(reason)),)


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def self_check(
    run_canary: CanaryRunner,
    *,
    timeout_s: float = CANARY_TIMEOUT_S,
    clock: Callable[[], datetime] = _utc_now,
    monotonic: Callable[[], float] = time.monotonic,
) -> SelfCheckReport:
    """Run the canary through ``run_canary`` and build the result; never raises but on cancel.

    A cancellation (``SIGTERM`` during the start-up) propagates and changes nothing; every other way
    the run can go wrong is a failed result.
    """
    ran_at = clock()
    started = monotonic()
    try:
        async with asyncio.timeout(timeout_s):
            text = await run_canary()
        checks = parse_canary_output(text)
    except CanaryUnavailable as exc:
        logger.error("self_check: the canary could not run: %s", clean_detail(str(exc)))
        checks = _canary_failed(str(exc) or "the canary could not run")
    except CanaryOutputRefused as exc:
        logger.error("self_check: the canary's output was refused: %s", exc)
        checks = _canary_failed(str(exc))
    except TimeoutError:
        logger.error("self_check: the canary did not finish within %s s", timeout_s)
        checks = _canary_failed(f"the canary did not finish within {timeout_s:g} seconds")
    except Exception as exc:
        # Operational boundary: a runner that fails in any other way (a sandbox that cannot start,
        # a defect) must give a failed result, not an exception that ends the start-up task. Only
        # the class is logged: the message of an OS error can quote a path or a credential.
        logger.error("self_check: the canary raised %s", type(exc).__name__)
        checks = _canary_failed("the canary raised an error; the server log names its class")
    report = SelfCheckReport(
        ran_at=ran_at,
        duration_ms=max(0, round((monotonic() - started) * 1000)),
        checks=checks,
    )
    if report.passed:
        logger.info("self_check passed: %d checks in %d ms", len(checks), report.duration_ms)
    else:
        logger.error("self_check failed: %s", ", ".join(report.failed_names))
    return report


class SelfCheckState:
    """The latest result, shared by the start-up, the edge and the platform page.

    Until the first run finishes nothing is passing, so an edge built before the run answers as if
    the check had failed. Runs never overlap (a canary is a whole sandbox), and a run that is
    cancelled leaves the previous result in place.
    """

    def __init__(
        self,
        run_canary: CanaryRunner,
        *,
        timeout_s: float = CANARY_TIMEOUT_S,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._run_canary = run_canary
        self._timeout_s = timeout_s
        self._clock = clock
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._latest: SelfCheckReport | None = None

    @property
    def latest(self) -> SelfCheckReport | None:
        """The result of the most recent finished run; ``None`` before the first."""
        return self._latest

    @property
    def is_passing(self) -> bool:
        return self._latest is not None and self._latest.passed

    @property
    def failed_names(self) -> tuple[str, ...]:
        """The ids to name on the status page; the canary's before the first run has finished."""
        if self._latest is None:
            return (CANARY_CHECK_NAME,)
        return self._latest.failed_names

    async def run(self) -> SelfCheckReport:
        """Run the canary now, keep the result and return it."""
        async with self._lock:
            report = await self_check(
                self._run_canary,
                timeout_s=self._timeout_s,
                clock=self._clock,
                monotonic=self._monotonic,
            )
            self._latest = report
            return report
