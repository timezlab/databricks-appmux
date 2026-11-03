"""How a child that dies at start explains itself: the reason and the hint (T190).

Measured on 2026-11-03 under the sandbox's address-space limit (``RLIMIT_AS`` = 2 x the budget):
Node.js 22 at a 64 MB budget never exits and prints nothing (a thread's stack cannot be mapped and
the main thread waits for it forever); at 128 and 256 MB it dies at once with ``SIGTRAP`` and
``Fatal process out of memory: Failed to reserve virtual memory for CodeRange``; Node.js 24 needs
640 MB. A uvicorn app on Python dies with ``SIGSEGV`` and no output below a 32 MB budget. Each
rule of ``failure_hint`` below is one of those shapes, and a crash that is not about memory gets
no hint.
"""

from __future__ import annotations

import pytest

from appmux.reconciler.runtime_limits import NODE_MIN_MEMORY_MB
from appmux.reconciler.start_failure import end_line, failure_hint, start_exit_reason
from appmux.sandbox.watchdog import ADDRESS_SPACE_HEADROOM_FACTOR

V8_OUT_OF_MEMORY = (
    "\n#\n# Fatal process out of memory: Failed to reserve virtual memory for CodeRange\n#\n"
)
HEALTH_TIMEOUT = "health check did not pass within 60 s (last error: connection refused)"


# --- the reason -------------------------------------------------------------------------------


def test_an_exit_with_a_code_names_the_code_and_how_long_the_app_ran() -> None:
    assert end_line(code=1, signal=None, ran_for_s=2.34) == "exited with code 1 after 2.3 s"
    assert start_exit_reason(code=1, signal=None, ran_for_s=2.34) == (
        "the app exited with code 1 after 2.3 s"
    )


def test_an_end_by_a_signal_names_the_signal() -> None:
    assert end_line(code=None, signal="SIGKILL", ran_for_s=0.42) == (
        "stopped by signal SIGKILL after 0.4 s"
    )
    assert start_exit_reason(code=None, signal="SIGKILL", ran_for_s=0.42) == (
        "the app was stopped by signal SIGKILL after 0.4 s"
    )


def test_an_end_with_neither_a_code_nor_a_signal_has_no_structured_reason() -> None:
    # Something else ended the child first (the run-directory watchdog, say).
    assert start_exit_reason(code=None, signal=None, ran_for_s=1.0) is None
    assert end_line(code=None, signal=None, ran_for_s=1.0) == "ended after 1.0 s"


# --- the hint: one rule per measured shape -----------------------------------------------------


def node_hint(**overrides: object) -> str | None:
    facts: dict[str, object] = {
        "reason": "the app was stopped by signal SIGTRAP after 1.2 s",
        "log_tail": V8_OUT_OF_MEMORY,
        "runtime": "node",
        "memory_budget_mb": 256,
    }
    facts.update(overrides)
    return failure_hint(**facts)  # type: ignore[arg-type]


def test_v8s_out_of_memory_text_gives_the_node_hint_in_one_sentence() -> None:
    hint = node_hint()

    assert hint is not None
    assert "below what Node.js needs" in hint
    assert f"{ADDRESS_SPACE_HEADROOM_FACTOR:g} times" in hint
    assert f"about {NODE_MIN_MEMORY_MB} MB" in hint
    assert hint.count(". ") == 0 and hint.endswith(".")


def test_the_node_threshold_is_the_one_constant_the_suggestions_also_read() -> None:
    assert NODE_MIN_MEMORY_MB == 768


@pytest.mark.parametrize(
    "text",
    [
        "Traceback (most recent call last):\nMemoryError",
        "OSError: [Errno 12] Cannot allocate memory",
        "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory",
    ],
)
def test_each_known_memory_text_in_the_output_of_an_early_exit_gives_a_hint(text: str) -> None:
    hint = failure_hint(
        reason="the app exited with code 1 after 0.8 s",
        log_tail=text,
        runtime="python",
        memory_budget_mb=4096,
    )

    assert hint is not None
    assert "memory budget" in hint
    assert "Node.js" not in hint


@pytest.mark.parametrize("signal", ["SIGSEGV", "SIGABRT", "SIGKILL", "SIGTRAP", "SIGBUS"])
def test_a_fatal_signal_at_start_with_a_small_budget_gives_a_hint(signal: str) -> None:
    hint = failure_hint(
        reason=f"the app was stopped by signal {signal} after 0.1 s",
        log_tail="",
        runtime="python",
        memory_budget_mb=24,
    )

    assert hint is not None
    assert f"{ADDRESS_SPACE_HEADROOM_FACTOR:g} times" in hint


def test_an_exit_code_of_128_plus_a_fatal_signal_counts_as_that_signal() -> None:
    # A shell or an older ``unshare`` reports the death of its child as 128 + the signal number.
    assert node_hint(reason="the app exited with code 133 after 1.1 s", log_tail="") is not None


def test_a_fatal_signal_with_a_budget_the_runtime_has_is_not_blamed_on_memory() -> None:
    assert node_hint(log_tail="", memory_budget_mb=NODE_MIN_MEMORY_MB) is None
    assert (
        failure_hint(
            reason="the app was stopped by signal SIGSEGV after 3.0 s",
            log_tail="",
            runtime="python",
            memory_budget_mb=1024,
        )
        is None
    )


def test_a_node_app_that_never_answers_and_never_prints_below_its_minimum_gets_the_hint() -> None:
    # The 64 MB shape: no exit, no output, the health deadline passes.
    hint = node_hint(reason=HEALTH_TIMEOUT, log_tail="", memory_budget_mb=64)

    assert hint is not None
    assert f"about {NODE_MIN_MEMORY_MB} MB" in hint


def test_a_silent_timeout_is_not_blamed_on_memory_when_the_budget_suffices_or_it_printed() -> None:
    assert node_hint(reason=HEALTH_TIMEOUT, log_tail="", memory_budget_mb=1024) is None
    assert (
        node_hint(reason=HEALTH_TIMEOUT, log_tail="listening on 3000", memory_budget_mb=64) is None
    )
    assert (
        failure_hint(reason=HEALTH_TIMEOUT, log_tail="", runtime="python", memory_budget_mb=64)
        is None
    )


def test_a_crash_that_is_not_about_memory_gets_no_hint() -> None:
    assert (
        failure_hint(
            reason="the app exited with code 1 after 0.5 s",
            log_tail="Traceback (most recent call last):\nValueError: no config",
            runtime="python",
            memory_budget_mb=64,
        )
        is None
    )


def test_a_build_failure_that_mentions_memory_gets_no_start_hint() -> None:
    assert (
        failure_hint(
            reason="pip could not install the requirements",
            log_tail="MemoryError",
            runtime="python",
            memory_budget_mb=64,
        )
        is None
    )
    assert failure_hint(reason=None, log_tail=None, runtime="node", memory_budget_mb=64) is None
