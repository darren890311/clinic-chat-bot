"""Loading domain objects out of the database.

Everything here returns the plain dataclasses the scheduling engine expects, so
the engine never sees a SQLAlchemy object and stays trivially testable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.domain.catalog import Practitioner, Seniority, Service
from app.domain.intervals import Interval
from app.domain.scheduling import PractitionerSchedule, SchedulingPolicy, WorkingWindow

OCCUPYING = ("held", "confirmed")


async def resolve_clinic_id(session: AsyncSession, slug: str) -> uuid.UUID | None:
    """Slug -> id, before any tenant scope exists.

    Goes through the SECURITY DEFINER function rather than a gap in the RLS
    policy, so this is the only pre-tenant read the application can perform.
    """
    result = await session.execute(text("SELECT public.resolve_clinic(:slug)"), {"slug": slug})
    return result.scalar_one_or_none()


async def load_policy(session: AsyncSession, clinic_id: uuid.UUID) -> SchedulingPolicy:
    clinic = await session.get(models.Clinic, clinic_id)
    if clinic is None:
        raise LookupError(f"clinic {clinic_id} not visible in this session")
    overrides = clinic.scheduling_policy or {}
    return SchedulingPolicy(timezone=clinic.timezone, **overrides)


async def load_services(session: AsyncSession, clinic_id: uuid.UUID) -> dict[str, Service]:
    rows = (
        await session.execute(select(models.Service).where(models.Service.is_active.is_(True)))
    ).scalars()
    return {
        r.code: Service(
            code=r.code, name=r.name, duration_minutes=r.duration_minutes, description=r.description
        )
        for r in rows
    }


def _to_practitioner(row: models.Practitioner) -> Practitioner:
    return Practitioner(
        slug=row.slug,
        name=row.name,
        title=row.title,
        seniority=Seniority(row.seniority),
        service_codes=frozenset(row.service_codes),
    )


def _to_windows(raw: list[dict]) -> tuple[WorkingWindow, ...]:
    return tuple(
        WorkingWindow(
            weekday=int(w["weekday"]),
            start=time.fromisoformat(w["start"]),
            end=time.fromisoformat(w["end"]),
        )
        for w in raw
    )


async def load_schedules(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    window: Interval,
    service_code: str | None = None,
    exclude_appointment_id: uuid.UUID | None = None,
) -> list[PractitionerSchedule]:
    """Practitioners plus every block of time they are already committed for.

    Internal bookings only. The booking service layers external calendar busy on
    top, fetched live per request.
    """
    stmt = select(models.Practitioner).where(models.Practitioner.is_active.is_(True))
    if service_code:
        stmt = stmt.where(models.Practitioner.service_codes.any(service_code))
    practitioners = list((await session.execute(stmt)).scalars())

    occupied = select(models.Appointment).where(
        models.Appointment.status.in_(OCCUPYING),
        models.Appointment.ends_at > window.start,
        models.Appointment.starts_at < window.end,
    )
    if exclude_appointment_id is not None:
        # Confirming a hold must not treat that hold as a conflict with itself.
        occupied = occupied.where(models.Appointment.id != exclude_appointment_id)
    appointments = list((await session.execute(occupied)).scalars())

    busy_by_practitioner: dict[uuid.UUID, list[Interval]] = {}
    for appt in appointments:
        busy_by_practitioner.setdefault(appt.practitioner_id, []).append(
            Interval(appt.starts_at, appt.ends_at)
        )

    return [
        PractitionerSchedule(
            practitioner=_to_practitioner(row),
            working_windows=_to_windows(row.working_windows),
            busy=tuple(busy_by_practitioner.get(row.id, [])),
        )
        for row in practitioners
    ]


async def practitioner_ids_by_slug(
    session: AsyncSession, clinic_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    rows = (await session.execute(select(models.Practitioner))).scalars()
    return {r.slug: r.id for r in rows}


async def expire_stale_holds(session: AsyncSession) -> int:
    """Release holds whose TTL has passed.

    There is no background worker by design, so this runs at the start of every
    booking transaction. A hold that has expired must stop blocking its slot
    before the exclusion constraint is consulted, or a patient who hesitated
    would lock the slot until someone noticed.
    """
    result = await session.execute(
        update(models.Appointment)
        .where(
            models.Appointment.status == "held",
            models.Appointment.hold_expires_at < func.now(),
        )
        .values(status="expired")
        # A bulk UPDATE bypasses the identity map, so an Appointment already
        # loaded in this session would keep reporting itself as held. Fetching
        # the affected rows keeps the in-memory objects honest.
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount or 0


async def get_practitioner(session: AsyncSession, *, slug: str) -> models.Practitioner | None:
    return (
        await session.execute(
            select(models.Practitioner).where(
                models.Practitioner.slug == slug,
                models.Practitioner.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def get_practitioner_by_id(
    session: AsyncSession, practitioner_id: uuid.UUID
) -> models.Practitioner | None:
    return await session.get(models.Practitioner, practitioner_id)


async def upsert_patient(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    full_name: str,
    phone: str,
    email: str | None,
) -> models.Patient:
    """Match an existing patient on phone number, otherwise create one.

    Phone is the practical key for a clinic taking bookings by voice: it is what
    the patient reliably knows and what reception already uses to find them.
    """
    existing = (
        await session.execute(select(models.Patient).where(models.Patient.phone == phone))
    ).scalar_one_or_none()

    if existing is not None:
        existing.full_name = full_name or existing.full_name
        if email:
            existing.email = email
        return existing

    patient = models.Patient(clinic_id=clinic_id, full_name=full_name, phone=phone, email=email)
    session.add(patient)
    await session.flush()
    return patient


async def get_appointment(
    session: AsyncSession, appointment_id: uuid.UUID
) -> models.Appointment | None:
    return (
        await session.execute(
            select(models.Appointment).where(models.Appointment.id == appointment_id)
        )
    ).scalar_one_or_none()


async def find_by_idempotency_key(session: AsyncSession, *, key: str) -> models.Appointment | None:
    return (
        await session.execute(
            select(models.Appointment).where(models.Appointment.idempotency_key == key)
        )
    ).scalar_one_or_none()


async def load_calendar_accounts(
    session: AsyncSession, *, practitioner_id: uuid.UUID
) -> list[models.CalendarAccount]:
    """Connected calendars for one practitioner, ignoring any that need reconnecting."""
    return list(
        (
            await session.execute(
                select(models.CalendarAccount).where(
                    models.CalendarAccount.practitioner_id == practitioner_id,
                    models.CalendarAccount.invalidated_at.is_(None),
                )
            )
        ).scalars()
    )


async def record_audit(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    detail: dict | None = None,
) -> None:
    """Append to the audit trail.

    The application role holds INSERT and SELECT on this table and nothing else,
    so a bug cannot rewrite history here even if it tries.
    """
    session.add(
        models.AuditLog(
            clinic_id=clinic_id,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail or {},
        )
    )
    # Flush immediately so the row exists at the point the action happened,
    # rather than whenever the surrounding transaction happens to flush next.
    await session.flush()


async def practitioner_slugs_by_id(
    session: AsyncSession, clinic_id: uuid.UUID
) -> dict[uuid.UUID, str]:
    rows = (await session.execute(select(models.Practitioner))).scalars()
    return {r.id: r.slug for r in rows}


async def upcoming_for_phone(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    phone: str,
    now: datetime,
    limit: int = 10,
) -> list[models.Appointment]:
    """A patient's future bookings, found by the number they gave.

    Phone is the handle a patient reliably has over the telephone. Row level
    security keeps the lookup inside this clinic, so a number that exists at
    another practice finds nothing here.
    """
    return list(
        (
            await session.execute(
                select(models.Appointment)
                .join(models.Patient, models.Appointment.patient_id == models.Patient.id)
                .where(
                    models.Patient.phone == phone,
                    models.Appointment.status == "confirmed",
                    models.Appointment.starts_at >= now,
                )
                .order_by(models.Appointment.starts_at)
                .limit(limit)
            )
        ).scalars()
    )


async def get_or_create_conversation(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    conversation_id: uuid.UUID | None,
    channel: str,
    llm_provider: str,
    llm_model: str,
) -> models.Conversation:
    if conversation_id is not None:
        existing = await session.get(models.Conversation, conversation_id)
        if existing is not None:
            return existing

    conversation = models.Conversation(
        id=conversation_id or uuid.uuid4(),
        clinic_id=clinic_id,
        channel=channel,
        llm_provider=llm_provider,
        llm_model=llm_model,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def load_transcript(
    session: AsyncSession, *, conversation_id: uuid.UUID, limit: int = 60
) -> list[models.Message]:
    """The stored turns, oldest first.

    Capped because a conversation that runs away should cost a bounded amount
    rather than an unbounded one; a booking that needs sixty turns has already
    failed and belongs with a person.
    """
    rows = list(
        (
            await session.execute(
                select(models.Message)
                .where(models.Message.conversation_id == conversation_id)
                .order_by(models.Message.seq.desc())
                .limit(limit)
            )
        ).scalars()
    )
    return list(reversed(rows))


async def append_message(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    tool_calls: list[dict] | None = None,
) -> models.Message:
    message = models.Message(
        clinic_id=clinic_id,
        conversation_id=conversation_id,
        role=role,
        content=content,
        tool_calls=tool_calls,
    )
    session.add(message)
    await session.flush()
    return message


async def mark_escalated(session: AsyncSession, *, conversation_id: uuid.UUID, reason: str) -> None:
    conversation = await session.get(models.Conversation, conversation_id)
    if conversation is None:
        return
    conversation.escalation_reason = reason
    if conversation.escalated_at is None:
        conversation.escalated_at = datetime.now(UTC)


async def active_hold_for_conversation(
    session: AsyncSession, *, conversation_id: uuid.UUID, now: datetime
) -> models.Appointment | None:
    """The slot this conversation is currently holding, if any.

    Read back from the database rather than reported by the agent, so the card
    the patient sees reflects what is actually reserved rather than what the
    model believes it reserved.
    """
    return (
        await session.execute(
            select(models.Appointment)
            .where(
                models.Appointment.conversation_id == conversation_id,
                models.Appointment.status == "held",
                models.Appointment.hold_expires_at > now,
            )
            .order_by(models.Appointment.starts_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def confirmed_for_conversation(
    session: AsyncSession, *, conversation_id: uuid.UUID
) -> list[models.Appointment]:
    return list(
        (
            await session.execute(
                select(models.Appointment)
                .where(
                    models.Appointment.conversation_id == conversation_id,
                    models.Appointment.status == "confirmed",
                )
                .order_by(models.Appointment.starts_at)
            )
        ).scalars()
    )
