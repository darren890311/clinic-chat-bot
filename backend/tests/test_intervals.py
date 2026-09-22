from __future__ import annotations

import pytest

from app.domain.intervals import Interval, merge, subtract, subtract_all
from tests.conftest import local, span

MONDAY = "2026-03-02"


def test_rejects_naive_and_empty_intervals() -> None:
    import datetime as dt

    with pytest.raises(ValueError, match="timezone-aware"):
        Interval(dt.datetime(2026, 3, 2, 9), dt.datetime(2026, 3, 2, 10))
    with pytest.raises(ValueError, match="non-empty"):
        Interval(local(MONDAY, 9), local(MONDAY, 9))


def test_touching_intervals_do_not_overlap() -> None:
    a = span(MONDAY, 9, 10)
    b = span(MONDAY, 10, 11)
    assert not a.overlaps(b)
    assert a.overlaps(span(MONDAY, 9, 10, end_m=1))


def test_merge_coalesces_overlapping_and_touching() -> None:
    merged = merge([span(MONDAY, 12, 13), span(MONDAY, 9, 10), span(MONDAY, 10, 11)])
    assert merged == [span(MONDAY, 9, 11), span(MONDAY, 12, 13)]


def test_subtract_splits_punches_and_trims() -> None:
    day = span(MONDAY, 9, 18)
    assert subtract(day, [span(MONDAY, 12, 13)]) == [span(MONDAY, 9, 12), span(MONDAY, 13, 18)]
    assert subtract(day, [span(MONDAY, 8, 10)]) == [span(MONDAY, 10, 18)]
    assert subtract(day, [span(MONDAY, 8, 20)]) == []
    assert subtract(day, []) == [day]


def test_subtract_handles_blocks_extending_past_the_base() -> None:
    assert subtract(span(MONDAY, 9, 18), [span(MONDAY, 17, 23)]) == [span(MONDAY, 9, 17)]


def test_subtract_all_normalises_overlapping_blocks() -> None:
    free = subtract_all([span(MONDAY, 9, 18)], [span(MONDAY, 11, 13), span(MONDAY, 12, 14)])
    assert free == [span(MONDAY, 9, 11), span(MONDAY, 14, 18)]
