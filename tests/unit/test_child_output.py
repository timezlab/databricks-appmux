"""The output ring of an app: what ``GET /apps/{app}/logs`` reads (T189).

One ring per app holds the last 2000 lines or 256 KiB of its children's output, across restarts
and failed starts. ``seq`` rises by one per line, ``instance`` changes at every start, and every
line is masked before it is kept: credential shapes and the child's own environment values.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from appmux.reconciler.output import (
    OUTPUT_MAX_BYTES,
    OUTPUT_MAX_LINES,
    OutputBook,
    OutputRing,
)

START = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
# Built at run time, so no credential-shaped literal sits in the source.
TOKEN = "dapi" + "0a1b2c3d" * 4


class Ticks:
    """A wall clock that moves one second per reading."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def ring(**limits: int) -> OutputRing:
    return OutputRing(now=Ticks(), **limits)


def texts(page_lines: object) -> list[str]:
    return [line.text for line in page_lines]  # type: ignore[attr-defined]


def test_the_default_caps_are_the_contracts() -> None:
    assert (OUTPUT_MAX_LINES, OUTPUT_MAX_BYTES) == (2000, 256 * 1024)


def test_lines_get_a_seq_that_rises_by_one_and_come_back_oldest_first() -> None:
    output = ring()
    writer = output.begin()
    for text in ("one", "two", "three"):
        writer.add("stdout", text)

    page = output.read(after=0, limit=500)

    assert [line.seq for line in page.lines] == [1, 2, 3]
    assert texts(page.lines) == ["one", "two", "three"]
    assert page.next_after == 3
    assert page.dropped == 0
    assert page.instance == writer.instance
    assert page.lines[0].at < page.lines[1].at


def test_after_returns_only_the_lines_the_client_does_not_have() -> None:
    output = ring()
    writer = output.begin()
    for number in range(5):
        writer.add("stdout", f"line {number}")

    page = output.read(after=3, limit=500)

    assert texts(page.lines) == ["line 3", "line 4"]
    assert page.next_after == 5
    assert output.read(after=5, limit=500).lines == ()
    assert output.read(after=5, limit=500).next_after == 5


def test_an_after_beyond_the_newest_line_reads_from_the_start() -> None:
    # appmux restarted, so the ring is new and a client's old cursor is ahead of it.
    output = ring()
    output.begin().add("stdout", "fresh")

    page = output.read(after=900, limit=500)

    assert texts(page.lines) == ["fresh"]
    assert page.next_after == 1


def test_the_ring_keeps_the_newest_lines_up_to_its_line_cap() -> None:
    output = ring(max_lines=3)
    writer = output.begin()
    for number in range(5):
        writer.add("stdout", f"line {number}")

    page = output.read(after=0, limit=500)

    assert texts(page.lines) == ["line 2", "line 3", "line 4"]
    assert page.dropped == 2  # seq 1 and 2 are gone


def test_the_ring_keeps_the_newest_lines_up_to_its_byte_cap() -> None:
    output = ring(max_bytes=10)
    writer = output.begin()
    for text in ("aaaa", "bbbb", "cccc"):
        writer.add("stdout", text)

    page = output.read(after=0, limit=500)

    assert texts(page.lines) == ["bbbb", "cccc"]
    assert page.dropped == 1


def test_a_limit_answers_the_newest_lines_and_counts_the_ones_it_leaves_out() -> None:
    output = ring()
    writer = output.begin()
    for number in range(10):
        writer.add("stdout", f"line {number}")

    page = output.read(after=2, limit=3)

    assert texts(page.lines) == ["line 7", "line 8", "line 9"]
    assert page.next_after == 10
    assert page.dropped == 5  # seq 3 to 7


def test_the_ring_survives_a_restart_and_the_instance_changes_at_each_start() -> None:
    output = ring()
    first = output.begin()
    first.add("stdout", "first copy")
    first.note_exit(code=None, signal="SIGKILL", ran_for_s=3.0)
    second = output.begin()
    second.add("stderr", "second copy")

    page = output.read(after=0, limit=500)

    assert first.instance != second.instance
    assert page.instance == second.instance
    assert texts(page.lines) == [
        "first copy",
        "stopped by signal SIGKILL after 3.0 s",
        "second copy",
    ]
    assert [line.stream for line in page.lines] == ["stdout", "appmux", "stderr"]
    assert [line.seq for line in page.lines] == [1, 2, 3]


def test_the_last_exit_is_kept_with_its_code_signal_time_and_run_length() -> None:
    output = ring()
    writer = output.begin()
    assert output.read(after=0, limit=1).last_exit is None

    writer.note_exit(code=1, signal=None, ran_for_s=2.34)

    last_exit = output.read(after=0, limit=1).last_exit
    assert last_exit is not None
    assert (last_exit.code, last_exit.signal, last_exit.ran_for_s) == (1, None, 2.34)
    assert last_exit.at > START
    assert texts(output.read(after=0, limit=10).lines) == ["exited with code 1 after 2.3 s"]


def test_a_token_shaped_string_and_an_environment_value_are_masked_before_they_are_kept() -> None:
    output = ring()
    writer = output.begin(mask=lambda text: text.replace("hunter2-long-value", "***"))
    writer.add("stdout", f"calling the API with {TOKEN} now")
    writer.add("stderr", "password=hunter2-long-value")
    writer.add("stdout", "Authorization: Bearer abcdefghijklmnop")

    shown = "\n".join(texts(output.read(after=0, limit=10).lines))

    assert TOKEN not in shown
    assert "hunter2-long-value" not in shown
    assert "abcdefghijklmnop" not in shown
    assert "calling the API with" in shown
    assert "password=" in shown  # the name stays readable, the value goes


def test_a_nul_and_a_carriage_return_are_dropped_and_a_long_line_is_cut() -> None:
    output = ring()
    writer = output.begin()
    writer.add("stdout", "before\x00after\r")
    writer.add("stdout", "x" * 10_000)

    first, second = output.read(after=0, limit=10).lines

    assert first.text == "beforeafter"
    assert len(second.text) < 4200
    assert second.text.endswith("(line cut)")


def test_the_tail_of_one_start_holds_its_own_output_and_no_appmux_lines() -> None:
    output = ring()
    old = output.begin()
    old.add("stdout", "old copy")
    new = output.begin()
    new.note("started")
    for number in range(60):
        new.add("stderr" if number % 2 else "stdout", f"line {number}")

    tail = new.tail(lines=50)

    assert tail.splitlines() == [f"line {number}" for number in range(10, 60)]
    assert "old copy" not in tail
    assert "started" not in tail


def test_the_book_moves_a_ring_with_a_rename_and_forgets_it_with_the_app() -> None:
    book = OutputBook(now=Ticks())
    book.ring("sales").begin().add("stdout", "hello")

    book.rename("sales", "revenue")

    assert book.get("sales") is None
    moved = book.get("revenue")
    assert moved is not None
    assert texts(moved.read(after=0, limit=10).lines) == ["hello"]
    book.forget("revenue")
    assert book.get("revenue") is None
