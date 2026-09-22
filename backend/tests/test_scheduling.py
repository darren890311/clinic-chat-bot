"""Behavioural tests for the scheduling engine.

These encode the clinic's actual booking rules. If a rule changes, a test here
should fail before anything reaches a patient.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from app.domain.catalog import PRACTITIONERS, get_service, practitioners_for
from app.domain.intervals import Interval
from app.domain.scheduling import (
    PractitionerSchedule,
    Slot,
    compute_availability,
    is_slot_available,
)
from tests.conftest import STANDARD_WINDOWS, local, span

MONDAY = "2026-03-02"  # EST (UTC-5)
MONDAY_AFTER_DST = "2026-03-09"  # EDT (UTC-4); clocks moved on Sunday the 8th
SATURDAY = "2026-03-07"
SUNDAY = "2026-03-08"

SUNDAY_BEFORE = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)  # well before the test week


def avail(service_code, schedules, day_span, policy, now=SUNDAY_BEFORE, **kw):
    return compute_availability(
        service=get_service(service_code),
        schedules=schedules,
        search=day_span,
        policy=policy,
        now=now,
        **kw,
    )


# --- competency ------------------------------------------------------------


def test_junior_cannot_be_booked_for_senior_only_services(junior, policy) -> None:
    for code in ("C", "D", "E"):
        assert avail(code, [junior], span(MONDAY, 0, 23), policy) == []


def test_junior_can_be_booked_for_basic_services(junior, policy) -> None:
    assert avail("A", [junior], span(MONDAY, 9, 18), policy)


def test_catalog_offers_the_junior_first_for_basic_services() -> None:
    assert [p.slug for p in practitioners_for("A")][0] == "dr-ramos"
    assert all(p.seniority == "senior" for p in practitioners_for("E"))


# --- slot generation -------------------------------------------------------


def test_one_hour_service_fills_an_empty_day_on_the_quarter_hour(senior, policy) -> None:
    slots = avail("A", [senior], span(MONDAY, 9, 18), policy)
    # 09:00 through 17:00 inclusive, every 15 minutes.
    assert len(slots) == 33
    assert slots[0].start == local(MONDAY, 9)
    assert slots[-1].start == local(MONDAY, 17)
    assert all(s.start.astimezone(policy.tz).minute % 15 == 0 for s in slots)
    assert all(s.end - s.start == timedelta(hours=1) for s in slots)


def test_six_hour_service_does_not_fit_the_short_saturday(senior, policy) -> None:
    assert avail("E", [senior], span(SATURDAY, 0, 23), policy) == []
    assert avail("A", [senior], span(SATURDAY, 0, 23), policy)


def test_six_hour_service_fits_a_weekday_only_in_the_morning(senior, policy) -> None:
    slots = avail("E", [senior], span(MONDAY, 0, 23), policy)
    assert slots[0].start == local(MONDAY, 9)
    assert slots[-1].start == local(MONDAY, 12)  # 12:00 + 6h = 18:00, the last fit
    assert len(slots) == 13


def test_clinic_is_closed_on_sunday(senior, policy) -> None:
    assert avail("A", [senior], span(SUNDAY, 0, 23), policy) == []


# --- busy blocks and buffer ------------------------------------------------


def test_busy_block_is_padded_by_the_turnaround_buffer(senior, policy) -> None:
    booked = dataclasses.replace(senior, busy=(span(MONDAY, 12, 13),))
    slots = avail("A", [booked], span(MONDAY, 9, 18), policy)
    starts = [s.start for s in slots]

    # 15-minute buffer each side: last morning slot must end by 11:45.
    assert local(MONDAY, 10, 45) in starts
    assert local(MONDAY, 11) not in starts
    # ...and the afternoon cannot resume until 13:15.
    assert local(MONDAY, 13) not in starts
    assert local(MONDAY, 13, 15) in starts


def test_buffer_is_configurable_and_zero_allows_back_to_back(senior, policy) -> None:
    no_buffer = dataclasses.replace(policy, turnaround_buffer_minutes=0)
    booked = dataclasses.replace(senior, busy=(span(MONDAY, 12, 13),))
    starts = [s.start for s in avail("A", [booked], span(MONDAY, 9, 18), no_buffer)]
    assert local(MONDAY, 11) in starts
    assert local(MONDAY, 13) in starts


def test_overlapping_busy_blocks_from_two_calendars_are_deduplicated(senior, policy) -> None:
    """Google and Outlook routinely report the same meeting twice."""
    doubled = dataclasses.replace(
        senior, busy=(span(MONDAY, 12, 13), span(MONDAY, 12, 13), span(MONDAY, 12, 14))
    )
    once = dataclasses.replace(senior, busy=(span(MONDAY, 12, 14),))
    assert avail("A", [doubled], span(MONDAY, 9, 18), policy) == avail(
        "A", [once], span(MONDAY, 9, 18), policy
    )


def test_fully_booked_day_yields_nothing(senior, policy) -> None:
    blocked = dataclasses.replace(senior, busy=(span(MONDAY, 8, 19),))
    assert avail("A", [blocked], span(MONDAY, 9, 18), policy) == []


# --- lead time and horizon -------------------------------------------------


def test_slots_inside_the_minimum_lead_time_are_hidden(senior, policy) -> None:
    now = local(MONDAY, 8)  # two-hour lead time -> nothing before 10:00
    slots = avail("A", [senior], span(MONDAY, 9, 18), policy, now=now)
    assert slots[0].start == local(MONDAY, 10)


def test_booking_horizon_is_enforced(senior, policy) -> None:
    short_horizon = dataclasses.replace(policy, booking_horizon_days=3)
    far = Interval(local(MONDAY, 9) + timedelta(days=30), local(MONDAY, 18) + timedelta(days=30))
    assert (
        compute_availability(
            service=get_service("A"),
            schedules=[senior],
            search=far,
            policy=short_horizon,
            now=local(MONDAY, 8),
        )
        == []
    )


# --- daylight saving -------------------------------------------------------


def test_working_hours_track_wall_clock_across_a_dst_transition(senior, policy) -> None:
    """9am stays 9am for the receptionist even though the UTC offset moves."""
    before = avail("A", [senior], span(MONDAY, 9, 18), policy)
    after = avail("A", [senior], span(MONDAY_AFTER_DST, 9, 18), policy)

    assert before[0].start == datetime(2026, 3, 2, 14, 0, tzinfo=UTC)  # EST
    assert after[0].start == datetime(2026, 3, 9, 13, 0, tzinfo=UTC)  # EDT
    assert len(before) == len(after)


# --- multi-practitioner ordering ------------------------------------------


def test_results_are_earliest_first_across_practitioners(senior, junior, policy) -> None:
    busy_senior = dataclasses.replace(senior, busy=(span(MONDAY, 9, 12),))
    slots = avail("A", [busy_senior, junior], span(MONDAY, 9, 18), policy)
    assert slots[0].practitioner_slug == "dr-ramos"
    assert slots[0].start == local(MONDAY, 9)
    assert slots == sorted(slots)


def test_limit_truncates_without_reordering(senior, junior, policy) -> None:
    full = avail("A", [senior, junior], span(MONDAY, 9, 18), policy)
    assert avail("A", [senior, junior], span(MONDAY, 9, 18), policy, limit=5) == full[:5]


# --- confirm-time revalidation --------------------------------------------


def test_revalidation_rejects_a_slot_taken_since_it_was_offered(senior, policy) -> None:
    """The gap between offering a slot and confirming it is where races live."""
    service = get_service("A")
    slot = Slot(local(MONDAY, 10), local(MONDAY, 11), "dr-hale")
    now = local(MONDAY, 8)

    assert is_slot_available(service=service, schedule=senior, slot=slot, policy=policy, now=now)

    # The practitioner accepted a meeting in Outlook while the patient was talking.
    conflicted = dataclasses.replace(senior, busy=(span(MONDAY, 10, 11, start_m=30),))
    assert not is_slot_available(
        service=service, schedule=conflicted, slot=slot, policy=policy, now=now
    )


def test_revalidation_rejects_a_mismatched_duration_or_practitioner(senior, policy) -> None:
    service = get_service("A")
    now = local(MONDAY, 8)

    wrong_duration = Slot(local(MONDAY, 10), local(MONDAY, 10, 30), "dr-hale")
    assert not is_slot_available(
        service=service, schedule=senior, slot=wrong_duration, policy=policy, now=now
    )

    wrong_person = Slot(local(MONDAY, 10), local(MONDAY, 11), "dr-okafor")
    assert not is_slot_available(
        service=service, schedule=senior, slot=wrong_person, policy=policy, now=now
    )


def test_revalidation_rejects_a_slot_outside_working_hours(senior, policy) -> None:
    after_hours = Slot(local(MONDAY, 19), local(MONDAY, 20), "dr-hale")
    assert not is_slot_available(
        service=get_service("A"),
        schedule=senior,
        slot=after_hours,
        policy=policy,
        now=local(MONDAY, 8),
    )


# --- the demo scenario -----------------------------------------------------


def test_full_mouth_restoration_finds_the_one_remaining_slot_in_a_busy_week(policy) -> None:
    """The Tuesday demo: only Thursday morning can still absorb a six-hour case."""
    week = Interval(local(MONDAY, 0), local("2026-03-08", 0))

    def sched(slug, busy):
        return PractitionerSchedule(
            practitioner=PRACTITIONERS[slug], working_windows=STANDARD_WINDOWS, busy=tuple(busy)
        )

    seniors = [
        sched(
            "dr-hale",
            [
                span(MONDAY, 9, 18),
                span("2026-03-03", 9, 18),
                span("2026-03-04", 9, 18),
                span("2026-03-05", 15, 18, start_m=15),  # Thursday afternoon booked
                span("2026-03-06", 9, 18),
            ],
        ),
        sched(
            "dr-okafor",
            [
                span(d, 9, 18)
                for d in ("2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06")
            ],
        ),
    ]

    slots = avail("E", seniors, week, policy)
    assert len(slots) == 1
    assert slots[0].practitioner_slug == "dr-hale"
    assert slots[0].start == local("2026-03-05", 9)
    assert slots[0].end == local("2026-03-05", 15)
