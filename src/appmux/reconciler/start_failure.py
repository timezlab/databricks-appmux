"""How a child that dies at start explains itself: the reason and the hint (T190).

The reason names how a started child ended while its health check was waiting: ``the app exited
with code 1 after 2.3 s`` or ``the app was stopped by signal SIGTRAP after 1.2 s``. The same words,
without "the app", are the line appmux writes into the app's output when a process ends
(``output.py``).

The hint is one sentence a person can act on. It is judged when a release is read, from what the
registry kept (the reason, the tail of the output, the detected runtime) and the app's memory
budget now, so a budget raised since the failure takes the hint away. Three rules, each a shape
measured on 2026-11-03 (``runtime_limits.py``), and anything else gets no hint:

1. the app exited at start and its output holds a known out-of-memory text;
2. the app was ended at start by a fatal signal (or exited with 128 + one, as a shell reports it)
   while its budget is below what its runtime needs;
3. a Node.js app below its minimum never answered and printed nothing: Node.js at a 64 MB budget
   hangs at start, because a thread's stack cannot be mapped and the main thread waits for it
   forever, so the health check runs to its deadline with an empty log.
"""

from __future__ import annotations

import re
import signal
from typing import Final

from appmux.reconciler.health import HEALTH_TIMEOUT_PREFIX
from appmux.reconciler.runtime_limits import (
    ADDRESS_SPACE_HEADROOM_FACTOR,
    NODE_MIN_MEMORY_MB,
    memory_floor_mb,
)

_EXIT_REASON_PREFIX: Final = "the app "
_EARLY_EXIT: Final = re.compile(
    r"^the app (?:exited with code (?P<code>[0-9]+)|was stopped by signal (?P<signal>SIG[A-Z0-9]+))"
    r" after "
)
# What a runtime prints when it cannot get memory: V8's fatal error and its heap limit, Python's
# exception, and the C library's text for ENOMEM.
_MEMORY_TEXT: Final = re.compile(
    r"Failed to reserve virtual memory|Fatal process out of memory"
    r"|JavaScript heap out of memory|MemoryError|Cannot allocate memory"
)
# The signals a process dies of when an allocation fails under RLIMIT_AS: V8's fatal error is a
# trap instruction (SIGTRAP), abort() is SIGABRT, a failed mapping touched is SIGSEGV or SIGBUS.
# SIGKILL is the RSS watchdog's (and the kernel's) answer to memory beyond the budget.
_FATAL_SIGNALS: Final = frozenset({"SIGSEGV", "SIGABRT", "SIGKILL", "SIGTRAP", "SIGBUS", "SIGILL"})
_SHELL_SIGNAL_BASE: Final = 128


def end_line(*, code: int | None, signal: str | None, ran_for_s: float) -> str:
    """How a process ended, as appmux writes it into the app's output."""
    if signal is not None:
        return f"stopped by signal {signal} after {ran_for_s:.1f} s"
    if code is None:
        return f"ended after {ran_for_s:.1f} s"
    return f"exited with code {code} after {ran_for_s:.1f} s"


def start_exit_reason(*, code: int | None, signal: str | None, ran_for_s: float) -> str | None:
    """The ``status_reason`` of a release whose child ended before its health check passed.

    ``None`` when neither the code nor the signal is known; the caller then says what it knows.
    """
    if code is None and signal is None:
        return None
    line = end_line(code=code, signal=signal, ran_for_s=ran_for_s)
    return _EXIT_REASON_PREFIX + (f"was {line}" if signal is not None else line)


def failure_hint(
    *, reason: str | None, log_tail: str | None, runtime: str | None, memory_budget_mb: int
) -> str | None:
    """One sentence on what to change for a release that failed at start, or ``None``."""
    if reason is None:
        return None
    output = (log_tail or "").strip()
    early_exit = _EARLY_EXIT.match(reason)
    is_below_floor = memory_budget_mb < memory_floor_mb(runtime)
    if early_exit is not None:
        if _MEMORY_TEXT.search(output) is not None:
            return _memory_hint(runtime)
        if is_below_floor and _ended_by_fatal_signal(early_exit):
            return _memory_hint(runtime)
        return None
    is_silent_timeout = reason.startswith(HEALTH_TIMEOUT_PREFIX) and not output
    if is_silent_timeout and runtime == "node" and is_below_floor:
        return _memory_hint(runtime)
    return None


def _ended_by_fatal_signal(early_exit: re.Match[str]) -> bool:
    name = early_exit.group("signal")
    if name is None:
        code = int(early_exit.group("code"))
        try:
            name = signal.Signals(code - _SHELL_SIGNAL_BASE).name
        except ValueError:
            return False
    return name in _FATAL_SIGNALS


def _memory_hint(runtime: str | None) -> str:
    limit = (
        f"the sandbox limits a child's address space to {ADDRESS_SPACE_HEADROOM_FACTOR:g} times "
        "its memory budget"
    )
    if runtime == "node":
        return (
            f"The memory budget is below what Node.js needs: {limit}, and Node.js needs a budget "
            f"of about {NODE_MIN_MEMORY_MB} MB, so raise the app's memory budget."
        )
    return (
        f"The memory budget is below what this app needs to start: {limit}, so raise the app's "
        "memory budget."
    )
