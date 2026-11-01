"""From the pump's running totals to what moved since the last look (T168, design 10.2).

``sandbox/pump.py`` keeps cumulative counters for the life of one sandbox (``PumpStats``).
``MetricsAggregator.record_egress`` wants differences, because it adds them to the bucket of the
current minute. ``EgressTracker`` remembers the last totals it saw for each pump and returns the
difference.

A pump is identified by its app and an ``instance`` string that is different for every sandbox
(the caller builds it from what changes when a sandbox starts). Two copies of one app run at once
while an update swaps them, so the app alone would mix their totals. A counter that went down
without a new instance (a pump restarted in place) counts again from zero rather than as a
negative number.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from appmux.sandbox.pump import PumpStats


@dataclass(frozen=True, slots=True)
class EgressDelta:
    """What one pump moved and dropped since the previous reading.

    ``dropped_by_rule`` leaves out the rules that dropped nothing new.
    """

    bytes_out: int
    bytes_in: int
    dropped: int
    dropped_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.bytes_out or self.bytes_in or self.dropped or self.dropped_by_rule)


@dataclass(frozen=True, slots=True)
class _Totals:
    bytes_out: int
    bytes_in: int
    dropped: int
    by_rule: dict[str, int]

    @classmethod
    def of(cls, stats: PumpStats) -> _Totals:
        return cls(
            bytes_out=stats["bytes_to_slirp"],
            bytes_in=stats["bytes_to_child"],
            dropped=stats["dropped_total"],
            by_rule=dict(stats["by_rule"]),
        )


_NOTHING_YET = _Totals(0, 0, 0, {})


def _since(before: int, now: int) -> int:
    return now - before if now >= before else now


class EgressTracker:
    """The last totals of each live pump, keyed by ``(app slug, instance)``."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], _Totals] = {}

    @property
    def tracked(self) -> int:
        """How many pumps are remembered."""
        return len(self._last)

    def delta(self, app_slug: str, instance: str, stats: PumpStats) -> EgressDelta:
        """What the pump moved since the last call for it (everything, on the first call)."""
        now = _Totals.of(stats)
        before = self._last.get((app_slug, instance), _NOTHING_YET)
        self._last[(app_slug, instance)] = now
        by_rule = {
            rule_id: _since(before.by_rule.get(rule_id, 0), count)
            for rule_id, count in now.by_rule.items()
        }
        return EgressDelta(
            bytes_out=_since(before.bytes_out, now.bytes_out),
            bytes_in=_since(before.bytes_in, now.bytes_in),
            dropped=_since(before.dropped, now.dropped),
            dropped_by_rule={rule_id: count for rule_id, count in by_rule.items() if count},
        )

    def retain(self, live: Collection[tuple[str, str]]) -> None:
        """Forget every pump not in ``live``, so the memory follows the running sandboxes."""
        for key in [key for key in self._last if key not in live]:
            del self._last[key]
