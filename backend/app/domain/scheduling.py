"""Deterministic scheduling engine.

The language model never decides when an appointment happens. It extracts intent
(service, practitioner preference, rough time window) and calls into this module,
which is pure: no database, no HTTP, no clock reads except the `now` passed in.
That makes every scheduling rule exhaustively testable, and it means a model
swap can never change booking behaviour.

Availability is computed as:

    working windows
      - (busy blocks padded by the turnaround buffer)
      - anything before `now + lead time` or after the booking horizon
      = free intervals, sliced into slots on the local-time grid
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.domain.catalog import Practitioner, Service
from app.domain.intervals import Interval, merge, subtract_all


@dataclass(frozen=True)
class WorkingWindow:
    """A recurring weekly window in the clinic's local time. weekday: 0=Monday."""

    weekday: int
    start: time
    end: time

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError(f"weekday must be 0-6, got {self.weekday}")
        if self.end <= self.start:
            raise ValueError("Working windows may not span midnight; split them per day")


@dataclass(frozen=True)
class SchedulingPolicy:
    """Clinic-wide booking rules. Stored per tenant, not hard-coded."""

    timezone: str = "America/New_York"
    slot_granularity_minutes: int = 15
    turnaround_buffer_minutes: int = 15
    minimum_lead_time_minutes: int = 120
    booking_horizon_days: int = 60

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def buffer(self) -> timedelta:
        return timedelta(minutes=self.turnaround_buffer_minutes)

    @property
    def lead_time(self) -> timedelta:
        return timedelta(minutes=self.minimum_lead_time_minutes)


@dataclass(frozen=True)
class PractitionerSchedule:
    """Everything the engine needs to know about one practitioner.

    `busy` is the union of our own confirmed appointments, active holds, and the
    free/busy blocks read live from that practitioner's external calendars. The
    engine does not care which source a block came from.
    """

    practitioner: Practitioner
    working_windows: tuple[WorkingWindow, ...]
    busy: tuple[Interval, ...] = field(default_factory=tuple)


@dataclass(frozen=True, order=True)
class Slot:
    start: datetime
    end: datetime
    practitioner_slug: str

    @property
    def interval(self) -> Interval:
        return Interval(self.start, self.end)


def compute_availability(
    *,
    service: Service,
    schedules: list[PractitionerSchedule],
    search: Interval,
    policy: SchedulingPolicy,
    now: datetime,
    limit: int | None = None,
) -> list[Slot]:
    """All slots in `search` where `service` can actually be delivered.

    Results are sorted by start time, then by practitioner slug, so the caller
    gets a stable "earliest first" ordering across practitioners.
    """
    bookable = _bookable_window(search=search, policy=policy, now=now)
    if bookable is None:
        return []

    slots: list[Slot] = []
    for schedule in schedules:
        if not schedule.practitioner.can_perform(service.code):
            continue
        slots.extend(
            _slots_for_practitioner(
                service=service, schedule=schedule, bookable=bookable, policy=policy
            )
        )

    slots.sort()
    return slots[:limit] if limit is not None else slots


def is_slot_available(
    *,
    service: Service,
    schedule: PractitionerSchedule,
    slot: Slot,
    policy: SchedulingPolicy,
    now: datetime,
) -> bool:
    """Re-validate a single slot immediately before writing the booking.

    We do not subscribe to calendar change notifications, so a practitioner may
    have added something to Outlook while the patient was still talking. This is
    called again at confirm time against freshly fetched free/busy data; the
    database exclusion constraint is the last line of defence behind it.
    """
    if slot.practitioner_slug != schedule.practitioner.slug:
        return False
    if slot.end - slot.start != service.duration:
        return False
    candidates = _slots_for_practitioner(
        service=service,
        schedule=schedule,
        bookable=_bookable_window(search=Interval(slot.start, slot.end), policy=policy, now=now)
        or Interval(slot.start, slot.end),
        policy=policy,
        ignore_grid=True,
    )
    return any(c.start == slot.start and c.end == slot.end for c in candidates)


def _bookable_window(
    *, search: Interval, policy: SchedulingPolicy, now: datetime
) -> Interval | None:
    """Intersect the requested range with the clinic's lead time and horizon."""
    horizon = Interval(now + policy.lead_time, now + timedelta(days=policy.booking_horizon_days))
    return search.clamp(horizon)


def _slots_for_practitioner(
    *,
    service: Service,
    schedule: PractitionerSchedule,
    bookable: Interval,
    policy: SchedulingPolicy,
    ignore_grid: bool = False,
) -> list[Slot]:
    windows = _expand_working_windows(
        windows=schedule.working_windows, bounds=bookable, tz=policy.tz
    )
    if not windows:
        return []

    padded_busy = [Interval(b.start - policy.buffer, b.end + policy.buffer) for b in schedule.busy]
    free = subtract_all(windows, padded_busy)

    slots: list[Slot] = []
    for gap in free:
        starts = (
            [gap.start]
            if ignore_grid
            else _grid_starts(gap, policy.slot_granularity_minutes, policy.tz)
        )
        for start in starts:
            end = start + service.duration
            if end > gap.end:
                break
            slots.append(Slot(start=start, end=end, practitioner_slug=schedule.practitioner.slug))
    return slots


def _expand_working_windows(
    *, windows: tuple[WorkingWindow, ...], bounds: Interval, tz: ZoneInfo
) -> list[Interval]:
    """Project recurring weekly windows onto real dates, in UTC.

    Windows are defined in clinic-local wall-clock time, so "9am Monday" stays
    9am across a daylight-saving transition even though the UTC offset moves.
    """
    by_weekday: dict[int, list[WorkingWindow]] = defaultdict(list)
    for window in windows:
        by_weekday[window.weekday].append(window)

    # Widen by a day on each side so windows that straddle the bounds in UTC
    # are still generated before clamping.
    day = bounds.start.astimezone(tz).date() - timedelta(days=1)
    last = bounds.end.astimezone(tz).date() + timedelta(days=1)

    out: list[Interval] = []
    while day <= last:
        for window in by_weekday.get(day.weekday(), []):
            start = _local(day, window.start, tz)
            end = _local(day, window.end, tz)
            if end <= start:  # pathological DST case; skip rather than invert
                continue
            clamped = Interval(start, end).clamp(bounds)
            if clamped is not None:
                out.append(clamped)
        day += timedelta(days=1)
    return merge(out)


def _local(day: date, wall: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, wall).replace(tzinfo=tz).astimezone(UTC)


def _grid_starts(gap: Interval, granularity_minutes: int, tz: ZoneInfo) -> list[datetime]:
    """Candidate start times inside `gap`, aligned to the local-time grid.

    Alignment is done on local wall-clock minutes so slots land on :00/:15/:30/:45
    for the receptionist, including in zones with a half-hour UTC offset.
    """
    step = timedelta(minutes=granularity_minutes)
    local = gap.start.astimezone(tz).replace(tzinfo=None)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    steps = math.ceil((local - midnight) / step)
    cursor = midnight + steps * step

    starts: list[datetime] = []
    gap_end_local = gap.end.astimezone(tz).replace(tzinfo=None)
    while cursor < gap_end_local:
        starts.append(cursor.replace(tzinfo=tz).astimezone(UTC))
        cursor += step
    return starts
