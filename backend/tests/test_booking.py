"""Integration tests for the booking transaction.

These run against a real database because the behaviour under test is largely
the database's: the exclusion constraint, the hold sweep, and the interaction
between the two. Skipped when TEST_APP_DATABASE_URL is unset.

The scenarios that matter are the racy ones. A booking flow that works when one
person uses it politely is not interesting; what matters is what happens when a
hold expires mid-conversation, when two callers want the same slot, and when a
retried request arrives after the response was lost.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models
from app.db import repository as repo
from app.domain import errors
from app.domain.intervals import Interval
from app.providers.calendar import CalendarError, ExternalEvent, register
from app.providers.calendar.null import NullCalendarProvider
from app.services import booking

APP_URL = os.environ.get("TEST_APP_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not APP_URL, reason="set TEST_APP_DATABASE_URL to run database tests"
)

CLINIC = uuid.UUID("33333333-3333-3333-3333-333333333333")
SENIOR = uuid.UUID("aaaaaaaa-1111-1111-1111-111111111111")
JUNIOR = uuid.UUID("bbbbbbbb-1111-1111-1111-111111111111")

# Mon-Fri 09:00-18:00 in the clinic's timezone.
WINDOWS = [{"weekday": d, "start": "09:00", "end": "18:00"} for d in range(5)]

SERVICES = [
    ("A", "Routine Cleaning", 60),
    ("B", "Dental Examination", 60),
    ("C", "Root Canal Treatment", 150),
    ("E", "Full Mouth Restoration", 360),
]

PATIENT = booking.PatientDetails(full_name="Alex Tran", phone="+886912345678")


class _FakeCalendar:
    """Stands in for Google or Outlook. Class attributes are reset per test.

    Registered as "google" because the schema restricts `provider` to the two
    real ones; what matters here is the port, not which vendor is behind it.
    """

    name = "google"
    busy: list[Interval] = []
    error: Exception | None = None
    write_error: Exception | None = None

    async def get_busy(self, credentials, window):
        if self.error:
            raise self.error
        return [b for b in self.busy if b.overlaps(window)]

    async def create_event(self, credentials, *, start, end, summary, description, idempotency_key):
        if self.write_error:
            raise self.write_error
        return ExternalEvent(provider=self.name, event_id=f"fake-{idempotency_key}")

    async def delete_event(self, credentials, event_id):
        return None


@pytest.fixture(autouse=True)
def _reset_fake_calendar():
    register(_FakeCalendar())
    _FakeCalendar.busy = []
    _FakeCalendar.error = None
    _FakeCalendar.write_error = None
    yield
    register(NullCalendarProvider())


def _connect_calendar(s, practitioner_id: uuid.UUID) -> None:
    """Give a practitioner a connected calendar, so the sync layer engages."""
    s.add(
        models.CalendarAccount(
            clinic_id=CLINIC,
            practitioner_id=practitioner_id,
            provider="google",
            account_email="dr.senior@example.com",
            calendar_id="primary",
            encrypted_refresh_token=b"",
            scopes=["https://www.googleapis.com/auth/calendar.events"],
        )
    )


def _async_url(url: str) -> str:
    """psycopg-style test URL -> asyncpg, including the ssl parameter spelling."""
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url.replace("sslmode=", "ssl=")


CLINIC_TZ = ZoneInfo("America/New_York")


def _monday(hour: int, minute: int = 0) -> datetime:
    """Clinic-local wall clock on a fixed Monday, as a UTC instant.

    Converted rather than offset by hand: 2026-10-05 falls in EDT, so nine in
    the morning is 13:00Z, not 14:00Z. Hard-coding the offset is exactly the
    bug the engine's daylight-saving handling exists to avoid.
    """
    return datetime(2026, 10, 5, hour, minute, tzinfo=CLINIC_TZ).astimezone(UTC)


@pytest_asyncio.fixture
async def session():
    """A tenant-scoped session that is always rolled back.

    Nothing is cleaned up explicitly, because nothing is committed. That also
    keeps the fixture honest about the application role's privileges: it holds
    INSERT and SELECT on audit_log and nothing else, so a fixture that tried to
    delete its own audit rows would fail — as an earlier version of this one did.
    """
    engine = create_async_engine(_async_url(APP_URL), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        # Transaction-local, matching what tenant_session() does per request.
        await s.execute(text("SELECT set_config('app.clinic_id', :c, true)"), {"c": str(CLINIC)})
        await _seed(s)
        try:
            yield s
        finally:
            await s.rollback()
    await engine.dispose()


async def _seed(s) -> None:
    """A clinic with one senior and one junior practitioner."""
    s.add(
        models.Clinic(
            id=CLINIC,
            slug=f"test-{CLINIC.hex[:8]}",
            name="Test Dental",
            timezone="America/New_York",
            scheduling_policy={},
        )
    )
    for code, name, minutes in SERVICES:
        s.add(
            models.Service(
                clinic_id=CLINIC, code=code, name=name, duration_minutes=minutes, description=""
            )
        )
    s.add(
        models.Practitioner(
            id=SENIOR,
            clinic_id=CLINIC,
            slug="dr-senior",
            name="Dr. Senior",
            title="Senior Dentist",
            seniority="senior",
            service_codes=["A", "B", "C", "E"],
            working_windows=WINDOWS,
        )
    )
    s.add(
        models.Practitioner(
            id=JUNIOR,
            clinic_id=CLINIC,
            slug="dr-junior",
            name="Dr. Junior",
            title="Associate Dentist",
            seniority="junior",
            service_codes=["A", "B"],
            working_windows=WINDOWS,
        )
    )
    await s.flush()


async def _audit_actions(s) -> list[str]:
    rows = await s.execute(
        text("SELECT action FROM audit_log WHERE clinic_id = :c ORDER BY created_at"),
        {"c": CLINIC},
    )
    return [r[0] for r in rows]


# --- holds -----------------------------------------------------------------


async def test_a_hold_reserves_the_slot_with_an_expiry(session) -> None:
    hold = await booking.create_hold(
        session,
        CLINIC,
        service_code="A",
        practitioner_slug="dr-senior",
        starts_at=_monday(9),
    )
    assert hold.status == "held"
    assert hold.ends_at - hold.starts_at == timedelta(hours=1)
    assert hold.hold_expires_at is not None
    assert hold.hold_expires_at > datetime.now(UTC)
    assert await _audit_actions(session) == ["hold.created"]


async def test_a_hold_blocks_a_second_hold_on_the_same_slot(session) -> None:
    await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    with pytest.raises(errors.SlotUnavailable):
        await booking.create_hold(
            session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
        )


async def test_a_hold_blocks_the_turnaround_buffer_around_it(session) -> None:
    """The engine, not the constraint, is what keeps appointments apart."""
    await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    with pytest.raises(errors.SlotUnavailable):
        # 10:00 abuts the 09:00-10:00 hold; the 15-minute buffer forbids it.
        await booking.create_hold(
            session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(10)
        )
    await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(10, 15)
    )


async def test_the_other_practitioner_is_unaffected(session) -> None:
    await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    hold = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-junior", starts_at=_monday(9)
    )
    assert hold.status == "held"


async def test_a_junior_cannot_be_held_for_a_senior_only_service(session) -> None:
    with pytest.raises(errors.PractitionerNotQualified):
        await booking.create_hold(
            session, CLINIC, service_code="C", practitioner_slug="dr-junior", starts_at=_monday(9)
        )


async def test_an_unknown_service_or_practitioner_is_rejected(session) -> None:
    with pytest.raises(errors.UnknownService):
        await booking.create_hold(
            session, CLINIC, service_code="Z", practitioner_slug="dr-senior", starts_at=_monday(9)
        )
    with pytest.raises(errors.UnknownPractitioner):
        await booking.create_hold(
            session, CLINIC, service_code="A", practitioner_slug="dr-nobody", starts_at=_monday(9)
        )


async def test_a_slot_outside_working_hours_is_rejected(session) -> None:
    with pytest.raises(errors.SlotUnavailable):
        await booking.create_hold(
            session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(19)
        )


async def test_a_six_hour_service_only_fits_the_morning(session) -> None:
    hold = await booking.create_hold(
        session, CLINIC, service_code="E", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    assert hold.ends_at == _monday(15)

    await booking.cancel_appointment(session, CLINIC, appointment_id=hold.id)
    with pytest.raises(errors.SlotUnavailable):
        # 13:00 + 6h runs past the 18:00 close.
        await booking.create_hold(
            session, CLINIC, service_code="E", practitioner_slug="dr-senior", starts_at=_monday(13)
        )


# --- expiry ----------------------------------------------------------------


async def test_an_expired_hold_releases_its_slot(session) -> None:
    """There is no background worker; the sweep runs inside the next booking."""
    hold = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    await session.execute(
        text("UPDATE appointments SET hold_expires_at = now() - interval '1 minute' WHERE id = :i"),
        {"i": hold.id},
    )

    replacement = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    assert replacement.id != hold.id
    await session.refresh(hold)
    assert hold.status == "expired"


# --- confirmation ----------------------------------------------------------


async def test_confirming_a_hold_books_the_appointment_and_records_the_patient(session) -> None:
    hold = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    appointment = await booking.confirm_appointment(
        session, CLINIC, hold_id=hold.id, patient=PATIENT
    )

    assert appointment.id == hold.id  # same row, promoted
    assert appointment.status == "confirmed"
    assert appointment.hold_expires_at is None
    assert appointment.patient_id is not None

    patient = await session.get(models.Patient, appointment.patient_id)
    assert (patient.full_name, patient.phone) == ("Alex Tran", "+886912345678")
    assert await _audit_actions(session) == ["hold.created", "appointment.confirmed"]


async def test_confirming_an_expired_hold_is_refused(session) -> None:
    hold = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    await session.execute(
        text("UPDATE appointments SET hold_expires_at = now() - interval '1 minute' WHERE id = :i"),
        {"i": hold.id},
    )
    with pytest.raises(errors.HoldExpired):
        await booking.confirm_appointment(session, CLINIC, hold_id=hold.id, patient=PATIENT)


async def test_confirming_an_unknown_hold_is_refused(session) -> None:
    with pytest.raises(errors.HoldNotFound):
        await booking.confirm_appointment(session, CLINIC, hold_id=uuid.uuid4(), patient=PATIENT)


async def test_a_retried_confirmation_returns_the_same_appointment(session) -> None:
    """A dropped response must not produce a second booking."""
    hold = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    first = await booking.confirm_appointment(
        session, CLINIC, hold_id=hold.id, patient=PATIENT, idempotency_key="call-42"
    )
    second = await booking.confirm_appointment(
        session, CLINIC, hold_id=hold.id, patient=PATIENT, idempotency_key="call-42"
    )
    assert first.id == second.id
    assert await _audit_actions(session) == ["hold.created", "appointment.confirmed"]


async def test_a_retry_of_the_whole_flow_does_not_produce_a_second_booking(session) -> None:
    """The case the idempotency key actually exists for.

    A client that times out mid-confirmation may retry from the top: a fresh
    hold, then a fresh confirmation. Without the key that is two appointments,
    because the second hold is a different row and passes every other check.
    """
    first_hold = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    first = await booking.confirm_appointment(
        session, CLINIC, hold_id=first_hold.id, patient=PATIENT, idempotency_key="call-99"
    )

    # The client never saw the response and starts again on another slot.
    second_hold = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(11)
    )
    second = await booking.confirm_appointment(
        session, CLINIC, hold_id=second_hold.id, patient=PATIENT, idempotency_key="call-99"
    )

    assert second.id == first.id
    assert second.starts_at == _monday(9)
    confirmed = await session.execute(
        text("SELECT count(*) FROM appointments WHERE clinic_id = :c AND status = 'confirmed'"),
        {"c": CLINIC},
    )
    assert confirmed.scalar_one() == 1


async def test_confirming_twice_without_a_key_is_still_not_a_double_booking(session) -> None:
    hold = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    first = await booking.confirm_appointment(session, CLINIC, hold_id=hold.id, patient=PATIENT)
    second = await booking.confirm_appointment(session, CLINIC, hold_id=hold.id, patient=PATIENT)
    assert first.id == second.id


async def test_a_slot_taken_in_an_external_calendar_is_caught_at_confirm_time(session) -> None:
    """The gap between offering a slot and agreeing to it is where races live.

    We read free/busy live rather than subscribing to change notifications, so a
    practitioner can accept a meeting in Outlook while the patient is still
    deciding. The confirmation must notice.
    """
    hold = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )

    # The practitioner's calendar now reports the slot as taken.
    _connect_calendar(session, SENIOR)
    _FakeCalendar.busy = [Interval(_monday(9, 30), _monday(10, 30))]

    with pytest.raises(errors.SlotUnavailable):
        await booking.confirm_appointment(session, CLINIC, hold_id=hold.id, patient=PATIENT)


async def test_an_unreadable_calendar_fails_the_booking_rather_than_guessing(session) -> None:
    """Offering a slot we could not verify is worse than admitting we cannot check."""
    _connect_calendar(session, SENIOR)
    _FakeCalendar.error = RuntimeError("provider timed out")

    with pytest.raises(CalendarError):
        await booking.create_hold(
            session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
        )


async def test_a_confirmed_appointment_records_where_it_was_mirrored(session) -> None:
    hold = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    _connect_calendar(session, SENIOR)

    appointment = await booking.confirm_appointment(
        session, CLINIC, hold_id=hold.id, patient=PATIENT
    )
    assert appointment.google_event_id == f"fake-{appointment.id}"
    assert appointment.mirror_error is None


async def test_a_calendar_that_fails_to_mirror_does_not_lose_the_booking(session) -> None:
    """The appointment already exists. A calendar outage must not undo it."""
    hold = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    _connect_calendar(session, SENIOR)
    _FakeCalendar.write_error = RuntimeError("calendar write rejected")

    appointment = await booking.confirm_appointment(
        session, CLINIC, hold_id=hold.id, patient=PATIENT
    )
    assert appointment.status == "confirmed"
    assert appointment.google_event_id is None
    assert "calendar write rejected" in appointment.mirror_error


# --- cancellation ----------------------------------------------------------


async def test_cancelling_frees_the_slot_for_someone_else(session) -> None:
    hold = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    appointment = await booking.confirm_appointment(
        session, CLINIC, hold_id=hold.id, patient=PATIENT
    )
    await booking.cancel_appointment(
        session, CLINIC, appointment_id=appointment.id, reason="patient rang back"
    )

    rebooked = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    assert rebooked.status == "held"
    assert await _audit_actions(session) == [
        "hold.created",
        "appointment.confirmed",
        "appointment.cancelled",
        "hold.created",
    ]


async def test_cancelling_twice_is_harmless(session) -> None:
    hold = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    await booking.cancel_appointment(session, CLINIC, appointment_id=hold.id)
    again = await booking.cancel_appointment(session, CLINIC, appointment_id=hold.id)
    assert again.status == "cancelled"


async def test_cancelling_something_that_does_not_exist_is_refused(session) -> None:
    with pytest.raises(errors.AppointmentNotFound):
        await booking.cancel_appointment(session, CLINIC, appointment_id=uuid.uuid4())


# --- availability ----------------------------------------------------------


async def test_availability_reflects_holds_taken_so_far(session) -> None:
    from app.domain.intervals import Interval

    search = Interval(_monday(0), _monday(23))
    _, before = await booking.find_availability(
        session, CLINIC, service_code="A", search=search, practitioner_slug="dr-senior", limit=500
    )
    await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    _, after = await booking.find_availability(
        session, CLINIC, service_code="A", search=search, practitioner_slug="dr-senior", limit=500
    )

    assert len(after) < len(before)
    assert all(s.start != _monday(9) for s in after)


async def test_a_lost_race_leaves_the_session_usable() -> None:
    """A constraint violation must not close the surrounding transaction.

    This test opens its own session with `async with session.begin()`, exactly
    as `tenant_session` does per request, because that is what makes the bug
    visible. When a flush fails, SQLAlchemy rolls back — and with no savepoint
    that rollback closes the outer `begin()` block, so every later statement in
    the request raises "Can't operate on closed transaction" with an error that
    names neither the slot nor the constraint.

    In production the crash landed several steps away: the agent caught the
    SlotUnavailable, returned a polite message to the model, and then died
    writing the transcript.

    Losing a race is an ordinary event in a booking conversation. The rest of
    the turn has to survive it.
    """
    engine = create_async_engine(_async_url(APP_URL), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clinic = uuid.uuid4()

    try:
        async with factory() as s, s.begin():
            await s.execute(
                text("SELECT set_config('app.clinic_id', :c, true)"), {"c": str(clinic)}
            )
            practitioner_id = uuid.uuid4()
            s.add(
                models.Clinic(
                    id=clinic,
                    slug=f"race-{clinic.hex[:8]}",
                    name="Race Dental",
                    timezone="America/New_York",
                    scheduling_policy={},
                )
            )
            s.add(
                models.Service(
                    clinic_id=clinic,
                    code="A",
                    name="Routine Cleaning",
                    duration_minutes=60,
                    description="",
                )
            )
            s.add(
                models.Practitioner(
                    id=practitioner_id,
                    clinic_id=clinic,
                    slug="dr-race",
                    name="Dr. Race",
                    title="Dentist",
                    seniority="senior",
                    service_codes=["A"],
                    working_windows=WINDOWS,
                )
            )
            await s.flush()

            await booking.create_hold(
                s, clinic, service_code="A", practitioner_slug="dr-race", starts_at=_monday(9)
            )
            with pytest.raises(errors.SlotUnavailable):
                await booking.create_hold(
                    s, clinic, service_code="A", practitioner_slug="dr-race", starts_at=_monday(9)
                )

            # The transaction is still alive: the turn can carry on and offer
            # something else.
            later = await booking.create_hold(
                s, clinic, service_code="A", practitioner_slug="dr-race", starts_at=_monday(11)
            )
            assert later.status == "held"

            await s.rollback()
    finally:
        await engine.dispose()


async def test_an_appointment_can_move_to_an_overlapping_time(session) -> None:
    """The case the exclusion constraint made impossible.

    Moving 09:00 to 09:30 was rejected by the database after the engine had
    approved it: the engine can be told to ignore the appointment being given
    up, Postgres cannot. The old appointment now steps aside in the same
    transaction as the new hold.
    """
    first = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    booked = await booking.confirm_appointment(session, CLINIC, hold_id=first.id, patient=PATIENT)

    moved = await booking.create_hold(
        session,
        CLINIC,
        service_code="A",
        practitioner_slug="dr-senior",
        starts_at=_monday(9, 30),
        replaces_appointment_id=booked.id,
    )
    assert moved.status == "held"
    await session.refresh(booked)
    assert booked.status == "superseded", "the old slot must free up for its replacement"

    await booking.confirm_appointment(session, CLINIC, hold_id=moved.id, patient=PATIENT)
    await session.refresh(booked)
    assert booked.status == "cancelled"
    await session.refresh(moved)
    assert moved.status == "confirmed"


async def test_an_abandoned_move_gives_the_original_appointment_back(session) -> None:
    """A patient who walks away mid-move must still have what they arrived with."""
    first = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    booked = await booking.confirm_appointment(session, CLINIC, hold_id=first.id, patient=PATIENT)
    moved = await booking.create_hold(
        session,
        CLINIC,
        service_code="A",
        practitioner_slug="dr-senior",
        starts_at=_monday(9, 30),
        replaces_appointment_id=booked.id,
    )

    await session.execute(
        text("UPDATE appointments SET hold_expires_at = now() - interval '1 minute' WHERE id = :i"),
        {"i": moved.id},
    )
    await repo.expire_stale_holds(session)

    await session.refresh(booked)
    await session.refresh(moved)
    assert moved.status == "expired"
    assert booked.status == "confirmed", "the original appointment has to come back"


async def test_a_failed_move_leaves_the_original_untouched(session) -> None:
    """If the new time cannot be held, nothing is given up."""
    first = await booking.create_hold(
        session, CLINIC, service_code="A", practitioner_slug="dr-senior", starts_at=_monday(9)
    )
    booked = await booking.confirm_appointment(session, CLINIC, hold_id=first.id, patient=PATIENT)

    with pytest.raises(errors.SlotUnavailable):
        await booking.create_hold(
            session,
            CLINIC,
            service_code="A",
            practitioner_slug="dr-senior",
            starts_at=_monday(19),  # outside working hours
            replaces_appointment_id=booked.id,
        )

    await session.refresh(booked)
    assert booked.status == "confirmed"
