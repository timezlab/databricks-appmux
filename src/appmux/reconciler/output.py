"""The output ring of each app: what ``GET /apps/{app}/logs`` reads (T189).

Design 5.6 keeps a child's output in memory only. Each app has one ring of its last
``OUTPUT_MAX_LINES`` lines or ``OUTPUT_MAX_BYTES`` of text, whichever is reached first, and the
ring outlives the child: a restart, a failed start and a release swap all write into the same ring,
so the lines of the copy that just died are still there to read. Every start begins an *instance*
(a random id) and every line gets the next ``seq`` of the ring, so a client asks for what came
after the last ``seq`` it has, and clears its view when the instance changes.

A line is masked before it is kept, never when it is read: the credential shapes of the audit log
(``registry.audit.mask_credentials``) and whatever the caller adds for that start (the reconciler
hides the child's own environment values). It also loses NUL characters and a trailing carriage
return, and a line longer than ``MAX_LINE_CHARS`` is cut. Lines with the ``appmux`` stream are what
appmux says about the app: ``started``, ``exited with code 1 after 2.3 s``, ``stopped by signal
SIGKILL after 0.4 s``.

The ring is filled by the reconciler (``Reconciler`` follows each child's output while it runs), and
the book of rings follows the app: a rename moves its ring, a delete forgets it.
"""

from __future__ import annotations

import secrets
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from appmux.reconciler.children import OutputStream
from appmux.reconciler.start_failure import end_line
from appmux.registry.audit import mask_credentials

OUTPUT_MAX_LINES: Final = 2000
OUTPUT_MAX_BYTES: Final = 256 * 1024
# A line longer than this is cut: one minified bundle printed whole would otherwise fill the ring.
MAX_LINE_CHARS: Final = 4096
_CUT_MARK: Final = " ... (line cut)"
_INSTANCE_BYTES: Final = 8


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _unmasked(text: str) -> str:
    return text


@dataclass(frozen=True, slots=True)
class OutputLine:
    seq: int
    at: datetime
    stream: OutputStream
    text: str
    instance: str


@dataclass(frozen=True, slots=True)
class LastExit:
    """The last end of a process of the app: its code or signal, when, and how long it ran."""

    code: int | None
    signal: str | None
    at: datetime
    ran_for_s: float


@dataclass(frozen=True, slots=True)
class OutputPage:
    """One read of a ring: the newest lines after a ``seq``, oldest first."""

    instance: str | None
    lines: tuple[OutputLine, ...]
    next_after: int
    dropped: int
    last_exit: LastExit | None


class OutputRing:
    """The last lines of one app's output, across its starts."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] = _utc_now,
        max_lines: int = OUTPUT_MAX_LINES,
        max_bytes: int = OUTPUT_MAX_BYTES,
    ) -> None:
        self._now = now
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._lines: deque[OutputLine] = deque()
        self._bytes = 0
        self._last_seq = 0
        self._instance: str | None = None
        self._last_exit: LastExit | None = None

    def begin(self, *, mask: Callable[[str], str] = _unmasked) -> OutputWriter:
        """A new start of the app: a new instance, and the writer of its lines."""
        instance = secrets.token_hex(_INSTANCE_BYTES)
        self._instance = instance
        return OutputWriter(self, instance, mask)

    def read(self, *, after: int, limit: int) -> OutputPage:
        """The newest ``limit`` lines after ``after``; ``dropped`` counts those it leaves out.

        A line after ``after`` is left out when the ring no longer holds it or when more than
        ``limit`` came after it. An ``after`` beyond the newest ``seq`` is a cursor from an earlier
        ring (appmux restarted), so the read starts from the beginning.
        """
        if after > self._last_seq:
            after = 0
        newer = [line for line in self._lines if line.seq > after]
        shown = newer[-limit:] if limit > 0 else []
        dropped = self._last_seq - after - len(shown)
        return OutputPage(
            instance=self._instance,
            lines=tuple(shown),
            next_after=self._last_seq,
            dropped=dropped,
            last_exit=self._last_exit,
        )

    def tail(self, instance: str, *, lines: int) -> str:
        """The last ``lines`` lines the child of ``instance`` printed (no ``appmux`` lines)."""
        own = [
            line.text
            for line in self._lines
            if line.instance == instance and line.stream != "appmux"
        ]
        return "\n".join(own[-lines:])

    def _append(self, instance: str, stream: OutputStream, text: str) -> None:
        self._last_seq += 1
        line = OutputLine(self._last_seq, self._now(), stream, text, instance)
        self._lines.append(line)
        self._bytes += _size(text)
        while len(self._lines) > self._max_lines or (
            self._bytes > self._max_bytes and len(self._lines) > 1
        ):
            self._bytes -= _size(self._lines.popleft().text)

    def _note_exit(self, last_exit: LastExit) -> None:
        self._last_exit = last_exit


class OutputWriter:
    """Writes the lines of one start into its app's ring, masked."""

    def __init__(self, ring: OutputRing, instance: str, mask: Callable[[str], str]) -> None:
        self._ring = ring
        self._mask = mask
        self.instance = instance

    def add(self, stream: OutputStream, text: str) -> None:
        cleaned = text.replace("\x00", "").removesuffix("\r")
        masked = mask_credentials(self._mask(cleaned))
        if len(masked) > MAX_LINE_CHARS:
            masked = masked[:MAX_LINE_CHARS] + _CUT_MARK
        self._ring._append(self.instance, stream, masked)

    def note(self, text: str) -> None:
        """A line appmux writes about the app."""
        self.add("appmux", text)

    def note_exit(self, *, code: int | None, signal: str | None, ran_for_s: float) -> None:
        self.note(end_line(code=code, signal=signal, ran_for_s=ran_for_s))
        self._ring._note_exit(LastExit(code, signal, self._ring._now(), ran_for_s))

    def tail(self, *, lines: int) -> str:
        return self._ring.tail(self.instance, lines=lines)


class OutputBook:
    """The ring of every app this process ran, by slug."""

    def __init__(self, *, now: Callable[[], datetime] = _utc_now) -> None:
        self._now = now
        self._rings: dict[str, OutputRing] = {}

    def ring(self, slug: str) -> OutputRing:
        """The ring of ``slug``, made on its first start."""
        ring = self._rings.get(slug)
        if ring is None:
            ring = self._rings[slug] = OutputRing(now=self._now)
        return ring

    def get(self, slug: str) -> OutputRing | None:
        """The ring of ``slug``; ``None`` if nothing of it was started by this process."""
        return self._rings.get(slug)

    def rename(self, old_slug: str, new_slug: str) -> None:
        ring = self._rings.pop(old_slug, None)
        if ring is not None:
            self._rings[new_slug] = ring

    def forget(self, slug: str) -> None:
        """The app is gone: its output must not reach a later app that takes the slug."""
        self._rings.pop(slug, None)


def _size(text: str) -> int:
    return len(text.encode("utf-8"))
