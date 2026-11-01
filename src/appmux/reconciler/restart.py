"""What the reconciler does when a running child dies (T105; FR-007, design 5.6).

The policy is small and fixed, so it is plain data here and the reconciler only follows it:

* a child that exits while its route is open is started again, **the same release**, after 1, 2, 4,
  8 and 16 seconds, one delay per restart;
* when those five restarts are used up the next crash leaves the app ``crashed``: its route stays
  closed (the edge answers ``503`` for that app only) and nothing starts it again by itself;
* an older release is never started in its place. A person decides that, by activating one
  (``activate.py``);
* a copy that ran for ``STABLE_RUN_S`` before it died starts the count again: a service that ran
  for an hour and then hit one bad request is not "crash-looping".

``RestartBudget`` is the counter; ``CrashReport`` is what is kept of the last crash, for the log
and for whoever asks why the app is down. Neither does I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Design 5.6: seconds to wait before restart number 1, 2, 3, 4 and 5.
RESTART_BACKOFF_S: Final[tuple[float, ...]] = (1.0, 2.0, 4.0, 8.0, 16.0)
# A copy that served this long before it died is not part of a crash loop (not in design 5.6; the
# only way to tell a loop from an app that crashes once a week without a person's help).
STABLE_RUN_S: Final = 60.0


class RestartBudget:
    """How many restarts a crashing app has used since it last ran stably."""

    def __init__(self) -> None:
        self._restarts = 0

    @property
    def restarts(self) -> int:
        return self._restarts

    def next_delay(self) -> float | None:
        """Seconds to wait before the next restart, or ``None`` when the budget is spent."""
        if self._restarts >= len(RESTART_BACKOFF_S):
            return None
        return RESTART_BACKOFF_S[self._restarts]

    def record_restart(self) -> None:
        self._restarts += 1

    def note_crash(self, *, ran_for_s: float) -> None:
        """A copy died after ``ran_for_s`` seconds; a long enough run starts the count again."""
        if ran_for_s >= STABLE_RUN_S:
            self.reset()

    def reset(self) -> None:
        self._restarts = 0


@dataclass(frozen=True, slots=True)
class CrashReport:
    """The last crash of an app: which release, how it ended and the end of its log.

    ``log_tail`` has every long-enough value of the child's environment replaced, because the
    child may have printed a credential it was given (design 5.6: the log is kept, the value never).
    """

    release_no: int
    exit_description: str
    log_tail: str
