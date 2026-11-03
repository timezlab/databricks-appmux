"""What a runtime needs from the memory budget of the app that runs it.

One place for the figure so the code that enforces it and the code that suggests it cannot drift
apart: the folder browser shows ``NODE_MIN_MEMORY_MB`` as the suggested budget for a folder that
holds a ``package.json``.
"""

from __future__ import annotations

from typing import Final

# Node.js needs about 768 MB (the contract addendum on ``failure_hint`` states the figure); a
# smaller budget makes the app fail to start, so the form suggests this one up front.
NODE_MIN_MEMORY_MB: Final = 768
