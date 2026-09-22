"""Half-open time interval algebra: [start, end).

Everything here is pure and timezone-aware (UTC). The scheduling engine is built
on these three operations, so they are kept deliberately small and total.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, order=True)
class Interval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Interval bounds must be timezone-aware")
        if self.end <= self.start:
            raise ValueError(f"Interval must be non-empty: {self.start} >= {self.end}")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def overlaps(self, other: Interval) -> bool:
        # Half-open: touching endpoints do not overlap.
        return self.start < other.end and other.start < self.end

    def contains(self, other: Interval) -> bool:
        return self.start <= other.start and other.end <= self.end

    def clamp(self, bounds: Interval) -> Interval | None:
        start = max(self.start, bounds.start)
        end = min(self.end, bounds.end)
        return Interval(start, end) if start < end else None


def merge(intervals: list[Interval]) -> list[Interval]:
    """Normalize into sorted, disjoint intervals. Adjacent intervals are coalesced."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    out = [ordered[0]]
    for cur in ordered[1:]:
        last = out[-1]
        if cur.start <= last.end:  # overlapping or touching
            out[-1] = Interval(last.start, max(last.end, cur.end))
        else:
            out.append(cur)
    return out


def subtract(base: Interval, blocks: list[Interval]) -> list[Interval]:
    """Return the parts of `base` not covered by any block."""
    free: list[Interval] = []
    cursor = base.start
    for block in merge(blocks):
        if block.end <= cursor:
            continue
        if block.start >= base.end:
            break
        if block.start > cursor:
            free.append(Interval(cursor, block.start))
        cursor = max(cursor, block.end)
        if cursor >= base.end:
            return free
    if cursor < base.end:
        free.append(Interval(cursor, base.end))
    return free


def subtract_all(bases: list[Interval], blocks: list[Interval]) -> list[Interval]:
    normalized = merge(blocks)
    return [free for base in bases for free in subtract(base, normalized)]
