"""Booking as a two-phase transaction.

A patient on a voice call takes tens of seconds between hearing a slot and
agreeing to it. Holding the slot for that window is the difference between a
booking flow that works and one that loses races to itself.

    hold     soft-lock the slot for a few minutes, nothing else needed yet
    confirm  re-check against live calendars, then commit with patient details

Every scheduling question here is answered by `app.domain.scheduling`, which is
pure. This module supplies it with data and writes the result down; it does not
decide what is available.

Three independent defences against double booking, in the order they fire:

1. the engine, from busy intervals read at that moment
2. a second engine check at confirm time, against freshly fetched calendars
3. the database exclusion constraint, which is the only one that holds when two
   requests are inside the same millisecond
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import models
from app.db import repository as repo
from app.domain import errors
from app.domain.catalog import Service
from app.domain.intervals import Interval, merge
from app.domain.scheduling import (
    PractitionerSchedule,
    SchedulingPolicy,
    Slot,
    compute_availability,
    is_slot_available,
)
from app.services import calendar_sync

settings = get_settings()

# The constraint added in migration b1c0de000002.
OVERLAP_CONSTRAINT = "appointments_no_overlap"


@dataclass(frozen=True)
class PatientDetails:
    full_name: str
    phone: str
    email: str | None = None
    notes: str | None = None


async def find_availability(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    service_code: str,
    search: Interval,
    practitioner_slug: str | None = None,
    limit: int = 20,
    now: datetime | None = None,
) -> tuple[Service, list[Slot]]:
    """Slots where `service_code` can actually be delivered.

    Includes external calendar busy, so what is offered has already been checked
    against the practitioners' own diaries.
    """
    now = now or datetime.now(UTC)
    service, policy, schedules = await _context(
        session,
        clinic_id,
        service_code=service_code,
        window=search,
        practitioner_slug=practitioner_slug,
    )
    slots = compute_availability(
        service=service,
        schedules=schedules,
        search=search,
        policy=policy,
        now=now,
        limit=limit,
    )
    return service, slots


async def create_hold(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    service_code: str,
    practitioner_slug: str,
    starts_at: datetime,
    conversation_id: uuid.UUID | None = None,
    replaces_appointment_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> models.Appointment:
    """Reserve a slot for a few minutes while the patient decides.

    A hold occupies the practitioner's time exactly as a confirmed appointment
    does — same table, same exclusion constraint — so nothing else can be booked
    over it while it lives.
    """
    now = now or datetime.now(UTC)

    # Release anything whose TTL has passed before consulting the constraint,
    # or a patient who hesitated would keep the slot until someone noticed.
    await repo.expire_stale_holds(session)

    service, policy, schedule = await _practitioner_context(
        session,
        clinic_id,
        service_code=service_code,
        practitioner_slug=practitioner_slug,
        starts_at=starts_at,
    )

    slot = Slot(
        start=starts_at,
        end=starts_at + service.duration,
        practitioner_slug=practitioner_slug,
    )
    if not is_slot_available(service=service, schedule=schedule, slot=slot, policy=policy, now=now):
        raise errors.SlotUnavailable(
            f"{starts_at.isoformat()} is not available for {service.name}."
        )

    practitioner = await repo.get_practitioner(session, slug=practitioner_slug)
    assert practitioner is not None  # _practitioner_context already resolved it

    hold = models.Appointment(
        clinic_id=clinic_id,
        practitioner_id=practitioner.id,
        service_code=service.code,
        starts_at=slot.start,
        ends_at=slot.end,
        status="held",
        hold_expires_at=now + timedelta(seconds=settings.hold_ttl_seconds),
        conversation_id=conversation_id,
        replaces_appointment_id=replaces_appointment_id,
    )
    session.add(hold)
    await _flush_or_conflict(session, slot)

    await repo.record_audit(
        session,
        clinic_id,
        actor="bot",
        action="hold.created",
        entity_type="appointment",
        entity_id=hold.id,
        detail={
            "service": service.code,
            "practitioner": practitioner_slug,
            "starts_at": slot.start.isoformat(),
            "expires_at": hold.hold_expires_at.isoformat(),
            "replaces": str(replaces_appointment_id) if replaces_appointment_id else None,
        },
    )
    return hold


async def confirm_appointment(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    hold_id: uuid.UUID,
    patient: PatientDetails,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> models.Appointment:
    """Turn a hold into a booking, after checking the world has not moved.

    The re-check is not belt and braces. This deployment reads free/busy live
    rather than subscribing to change notifications, so a practitioner can
    accept a meeting in Outlook during the pause between offer and agreement.
    """
    now = now or datetime.now(UTC)

    if idempotency_key:
        existing = await repo.find_by_idempotency_key(session, key=idempotency_key)
        if existing is not None:
            # A retried request, not a second booking.
            return existing

    await repo.expire_stale_holds(session)

    hold = await repo.get_appointment(session, hold_id)
    if hold is None:
        raise errors.HoldNotFound("That reservation no longer exists.")
    if hold.status == "confirmed":
        return hold
    if hold.status != "held":
        raise errors.HoldExpired("That reservation has expired. Let me find you another time.")

    service, policy, schedule = await _practitioner_context(
        session,
        clinic_id,
        service_code=hold.service_code,
        practitioner_id=hold.practitioner_id,
        starts_at=hold.starts_at,
        # The hold itself occupies this slot; it must not conflict with itself.
        exclude_appointment_id=hold.id,
    )

    slot = Slot(
        start=hold.starts_at, end=hold.ends_at, practitioner_slug=schedule.practitioner.slug
    )
    if not is_slot_available(service=service, schedule=schedule, slot=slot, policy=policy, now=now):
        raise errors.SlotUnavailable(
            "That time was taken while we were talking. Let me find you another."
        )

    patient_row = await repo.upsert_patient(
        session,
        clinic_id,
        full_name=patient.full_name,
        phone=patient.phone,
        email=patient.email,
    )

    hold.status = "confirmed"
    hold.hold_expires_at = None
    hold.patient_id = patient_row.id
    hold.idempotency_key = idempotency_key
    hold.notes = patient.notes
    await _flush_or_conflict(session, slot)

    await repo.record_audit(
        session,
        clinic_id,
        actor="bot",
        action="appointment.confirmed",
        entity_type="appointment",
        entity_id=hold.id,
        detail={
            "service": service.code,
            "practitioner": schedule.practitioner.slug,
            "starts_at": hold.starts_at.isoformat(),
            "patient_id": str(patient_row.id),
        },
    )

    # A rescheduling completes here or not at all. The old appointment is
    # released in the same transaction that books the new one, so the patient
    # is never left holding both or neither.
    if hold.replaces_appointment_id:
        # Already gone is fine; the move still stands.
        with contextlib.suppress(errors.AppointmentNotFound):
            await cancel_appointment(
                session,
                clinic_id,
                appointment_id=hold.replaces_appointment_id,
                reason="rescheduled",
                actor="bot",
            )

    # The booking now exists. Mirroring runs afterwards and never raises: a
    # calendar outage leaves an appointment that is not yet mirrored, which
    # staff can see and retry, rather than a patient who was told no.
    practitioner_row = await repo.get_practitioner(session, slug=schedule.practitioner.slug)
    if practitioner_row is not None:
        mirror = await calendar_sync.mirror_appointment(
            session,
            appointment=hold,
            practitioner=practitioner_row,
            summary=f"{service.name} — {patient.full_name}",
            description=f"Booked by the clinic assistant. Contact: {patient.phone}",
        )
        hold.google_event_id = mirror.google_event_id
        hold.microsoft_event_id = mirror.microsoft_event_id
        hold.mirror_error = mirror.error

    return hold


async def cancel_appointment(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    appointment_id: uuid.UUID,
    reason: str | None = None,
    actor: str = "bot",
) -> models.Appointment:
    """Cancel a booking and withdraw it from the external calendars."""
    appointment = await repo.get_appointment(session, appointment_id)
    if appointment is None:
        raise errors.AppointmentNotFound("I could not find that appointment.")
    if appointment.status == "cancelled":
        return appointment
    if appointment.status not in ("held", "confirmed"):
        raise errors.AppointmentNotCancellable(
            f"That appointment is {appointment.status} and cannot be cancelled."
        )

    await calendar_sync.withdraw_appointment(session, appointment=appointment)

    appointment.status = "cancelled"
    appointment.hold_expires_at = None
    appointment.google_event_id = None
    appointment.microsoft_event_id = None

    await repo.record_audit(
        session,
        clinic_id,
        actor=actor,
        action="appointment.cancelled",
        entity_type="appointment",
        entity_id=appointment.id,
        detail={"reason": reason} if reason else {},
    )
    return appointment


# --- internals -------------------------------------------------------------


async def _flush_or_conflict(session: AsyncSession, slot: Slot) -> None:
    """Push the write to the database and translate an overlap into a domain error.

    This is the defence that actually holds when two conversations confirm the
    same slot in the same instant: both pass their engine checks, and Postgres
    rejects the second.
    """
    try:
        await session.flush()
    except IntegrityError as exc:
        if OVERLAP_CONSTRAINT in str(exc.orig):
            raise errors.SlotUnavailable(
                "Someone just booked that time. Let me find you another."
            ) from exc
        raise


async def _context(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    service_code: str,
    window: Interval,
    practitioner_slug: str | None = None,
    exclude_appointment_id: uuid.UUID | None = None,
) -> tuple[Service, SchedulingPolicy, list[PractitionerSchedule]]:
    """Assemble everything the engine needs, including live external busy."""
    services = await repo.load_services(session, clinic_id)
    code = service_code.strip().upper()
    if code not in services:
        raise errors.UnknownService(
            f"We do not offer {service_code!r}. Available: {', '.join(sorted(services))}."
        )
    service = services[code]
    policy = await repo.load_policy(session, clinic_id)

    schedules = await repo.load_schedules(
        session,
        clinic_id,
        window=window,
        service_code=code,
        exclude_appointment_id=exclude_appointment_id,
    )
    if practitioner_slug:
        schedules = [s for s in schedules if s.practitioner.slug == practitioner_slug]

    ids = {p.slug: p.id for p in await _practitioner_rows(session)}
    enriched = [
        _with_external_busy(
            schedule,
            await calendar_sync.external_busy(
                session, practitioner_id=ids[schedule.practitioner.slug], window=window
            ),
        )
        for schedule in schedules
    ]
    return service, policy, enriched


def _with_external_busy(
    schedule: PractitionerSchedule, external: list[Interval]
) -> PractitionerSchedule:
    """Fold a practitioner's external commitments into their busy list.

    The engine sees one flat, merged list and never learns which source a block
    came from.
    """
    return dataclasses.replace(schedule, busy=tuple(merge([*schedule.busy, *external])))


async def _practitioner_rows(session: AsyncSession) -> list[models.Practitioner]:
    return list((await session.execute(select(models.Practitioner))).scalars())


async def _practitioner_context(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    service_code: str,
    starts_at: datetime,
    practitioner_slug: str | None = None,
    practitioner_id: uuid.UUID | None = None,
    exclude_appointment_id: uuid.UUID | None = None,
) -> tuple[Service, SchedulingPolicy, PractitionerSchedule]:
    """The single-practitioner view used by hold and confirm.

    Widened by a day on each side so the turnaround buffer sees neighbouring
    appointments that start before the requested slot.
    """
    services = await repo.load_services(session, clinic_id)
    code = service_code.strip().upper()
    if code not in services:
        raise errors.UnknownService(f"We do not offer {service_code!r}.")
    service = services[code]

    if practitioner_slug is None:
        row = next((p for p in await _practitioner_rows(session) if p.id == practitioner_id), None)
        if row is None:
            raise errors.UnknownPractitioner("That practitioner is no longer available.")
        practitioner_slug = row.slug

    practitioner = await repo.get_practitioner(session, slug=practitioner_slug)
    if practitioner is None:
        raise errors.UnknownPractitioner(f"We have no practitioner called {practitioner_slug!r}.")
    if code not in set(practitioner.service_codes):
        raise errors.PractitionerNotQualified(
            f"{practitioner.name} does not perform {service.name}."
        )

    window = Interval(
        starts_at - timedelta(days=1), starts_at + service.duration + timedelta(days=1)
    )
    policy = await repo.load_policy(session, clinic_id)

    schedules = await repo.load_schedules(
        session,
        clinic_id,
        window=window,
        service_code=code,
        exclude_appointment_id=exclude_appointment_id,
    )
    schedule = next((s for s in schedules if s.practitioner.slug == practitioner_slug), None)
    if schedule is None:
        raise errors.UnknownPractitioner(f"We have no practitioner called {practitioner_slug!r}.")

    external = await calendar_sync.external_busy(
        session, practitioner_id=practitioner.id, window=window
    )
    return service, policy, _with_external_busy(schedule, external)
