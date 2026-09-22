from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.domain.catalog import PRACTITIONERS
from app.domain.intervals import Interval
from app.domain.scheduling import PractitionerSchedule, SchedulingPolicy, WorkingWindow

CLINIC_TZ = ZoneInfo("America/New_York")

# Mon-Fri 09:00-18:00, Sat 09:00-13:00, closed Sunday.
WEEKDAY_WINDOWS = tuple(WorkingWindow(d, time(9, 0), time(18, 0)) for d in range(0, 5))
SATURDAY_WINDOW = (WorkingWindow(5, time(9, 0), time(13, 0)),)
STANDARD_WINDOWS = WEEKDAY_WINDOWS + SATURDAY_WINDOW


def local(day: str, hh: int, mm: int = 0) -> datetime:
    """Clinic-local wall clock -> UTC instant."""
    return (
        datetime.combine(date.fromisoformat(day), time(hh, mm))
        .replace(tzinfo=CLINIC_TZ)
        .astimezone(UTC)
    )


def span(day: str, start_h: int, end_h: int, start_m: int = 0, end_m: int = 0) -> Interval:
    return Interval(local(day, start_h, start_m), local(day, end_h, end_m))


@pytest.fixture
def policy() -> SchedulingPolicy:
    return SchedulingPolicy(timezone="America/New_York")


@pytest.fixture
def senior() -> PractitionerSchedule:
    return PractitionerSchedule(
        practitioner=PRACTITIONERS["dr-hale"], working_windows=STANDARD_WINDOWS
    )


@pytest.fixture
def junior() -> PractitionerSchedule:
    return PractitionerSchedule(
        practitioner=PRACTITIONERS["dr-ramos"], working_windows=STANDARD_WINDOWS
    )
