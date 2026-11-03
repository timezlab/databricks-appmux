"""What a runtime needs from the sandbox's memory limit (T190).

The sandbox limits a child's address space (``RLIMIT_AS``) to its memory budget times
``ADDRESS_SPACE_HEADROOM_FACTOR`` (``sandbox/watchdog.py``). A runtime that reserves a large
address space at start dies, or hangs, below some budget, whatever the app itself would use. The
figures are measured, not derived, and are the one place both the start-failure hint
(``start_failure.py``) and the workspace browser's suggested budget read them.
"""

from __future__ import annotations

from typing import Final

from appmux.sandbox.watchdog import ADDRESS_SPACE_HEADROOM_FACTOR

# Measured 2026-11-03 with `node -e "console.log('ok')"` under RLIMIT_AS = 2 x budget: Node.js 22
# hangs at 64 MB, dies with "Failed to reserve virtual memory for CodeRange" at 128 and 256 MB and
# runs from 384 MB; Node.js 24 dies up to 512 MB and runs from 640 MB. 768 MB leaves room for the
# app's own code above the newest of the two.
NODE_MIN_MEMORY_MB: Final = 768
# Below this budget a fatal signal at start is blamed on the limit for a runtime with no measured
# minimum. A uvicorn app on Python died with SIGSEGV (and no output) below a 32 MB budget; an app
# that imports a data library reserves several times that, so the line is drawn well above.
SMALL_MEMORY_BUDGET_MB: Final = 256

__all__ = ["ADDRESS_SPACE_HEADROOM_FACTOR", "NODE_MIN_MEMORY_MB", "SMALL_MEMORY_BUDGET_MB"]


def memory_floor_mb(runtime: str | None) -> int:
    """The budget below which a start failure of ``runtime`` is likely the address-space limit."""
    return NODE_MIN_MEMORY_MB if runtime == "node" else SMALL_MEMORY_BUDGET_MB
