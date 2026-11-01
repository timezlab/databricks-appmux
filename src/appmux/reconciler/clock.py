"""The reconciler's view of time: a monotonic reading and a sleep, behind one small seam.

The health deadline (60 s by default), the drain after a swap (30 s) and the stop budget at
shutdown are all measured here. A test passes a clock whose ``sleep`` only moves a number, so none
of them is waited for.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock: ``time.monotonic`` and ``asyncio.sleep``."""

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
