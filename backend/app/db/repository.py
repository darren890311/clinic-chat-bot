"""Loading domain objects out of the database.

Everything here returns the plain dataclasses the scheduling engine expects, so
the engine never sees a SQLAlchemy object and stays trivially testable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.domain.catalog import Practitioner, Seniority, Service
from app.domain.intervals import Interval
from app.domain.scheduling import PractitionerSchedule, SchedulingPolicy, WorkingWindow
from app.providers.llm import Usage

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


async def lock_practitioner(
    session: AsyncSession, clinic_id: uuid.UUID, *, slug: str
) -> uuid.UUID | None:
    """Take the practitioner's row for the rest of this transaction.

    Holds for one practitioner then queue instead of colliding. Without it,
    two simultaneous requests for the same slot each swept expired holds and
    then inserted, touching the same rows in whatever order they arrived in,
    and Postgres broke the tie by killing one with a deadlock.

    A deadlock is worse than losing a race. Losing a race aborts a savepoint
    and the conversation carries on to offer another time; a deadlock aborts
    the whole transaction, so there is nothing left to carry on with. Queuing
    turns it back into an ordinary lost race.

    Holds are short and the row is never held for long, so the queue is a few
    milliseconds even when a popular slot is contested.
    """
    return (
        await session.execute(
            select(models.Practitioner.id)
            .where(
                models.Practitioner.clinic_id == clinic_id,
                models.Practitioner.slug == slug,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


async def expire_stale_holds(session: AsyncSession) -> int:
    """Release holds whose TTL has passed.

    There is no background worker by design, so this runs at the start of every
    booking transaction. A hold that has expired must stop blocking its slot
    before the exclusion constraint is consulted, or a patient who hesitated
    would lock the slot until someone noticed.
    """
    stale = list(
        (
            await session.execute(
                select(models.Appointment).where(
                    models.Appointment.status == "held",
                    models.Appointment.hold_expires_at < func.now(),
                )
            )
        ).scalars()
    )
    if not stale:
        return 0

    for hold in stale:
        hold.status = "expired"

    # Flushed before any restore. SQLAlchemy batches UPDATEs and does not
    # guarantee the order between them; with both pending it issued the restore
    # first, while the hold still occupied the slot, and the exclusion
    # constraint rejected the pair. The slot has to be released before anything
    # can move back into it.
    await session.flush()

    # A move that was never confirmed has to leave the patient where they
    # started. The appointment that stepped aside when the hold was made comes
    # back, unless someone else has since taken its slot — in which case the
    # exclusion constraint refuses and it stays superseded for staff to see.
    for hold in stale:
        if not hold.replaces_appointment_id:
            continue
        replaced = await session.get(models.Appointment, hold.replaces_appointment_id)
        if replaced is None or replaced.status != "superseded":
            continue
        replaced.status = "confirmed"
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            replaced.status = "superseded"
            session.add(
                models.AuditLog(
                    clinic_id=replaced.clinic_id,
                    actor="system",
                    action="appointment.restore_failed",
                    entity_type="appointment",
                    entity_id=replaced.id,
                    detail={"reason": "slot taken while the move was pending"},
                )
            )

    await session.flush()
    return len(stale)


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


def normalise_name(name: str) -> str:
    """Casefolded, with runs of whitespace collapsed.

    Only for comparison. What the practice displays and calls out in a waiting
    room is always the name as the patient typed it.
    """
    return " ".join(name.casefold().split())


def name_matches(given: str, stored: str) -> bool:
    """Whether `given` identifies the person whose record reads `stored`.

    Forgiving in one direction only. Every word the caller gave must appear in
    the stored name, so "Darren" finds "Darren Chen" and so does "Darren
    Chen" — a patient should not have to remember whether they gave a surname
    six months ago. "Kevin" finds neither, which is the entire point.
    """
    given_words = set(normalise_name(given).split())
    return bool(given_words) and given_words <= set(normalise_name(stored).split())


async def upsert_patient(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    full_name: str,
    phone: str,
    email: str | None,
) -> models.Patient:
    """Match on phone *and* name, otherwise create.

    Phone alone was the key here, and the name was overwritten on every match.
    Families share a number — a parent booking for a child, a couple on one
    mobile — so booking for somebody else silently renamed every appointment
    the first person had. The practice then calls out the wrong name in the
    waiting room for a patient whose own record no longer carries their name.

    Matching on both means two people on one number are two records, which is
    what they are. An existing name is never rewritten: correcting a typo is
    `correct_my_details`, which is scoped to the conversation that made the
    booking, and it should not be reachable by anyone who happens to know the
    number.

    The comparison is exact rather than the forgiving match used for lookup.
    "Darren" and "Darren Chen" become two records here, which is redundant but
    never wrong, where merging them would mean deciding which name survives —
    the decision that caused this.
    """
    candidates = (
        (
            await session.execute(
                select(models.Patient).where(
                    models.Patient.clinic_id == clinic_id, models.Patient.phone == phone
                )
            )
        )
        .scalars()
        .all()
    )

    wanted = normalise_name(full_name)
    for existing in candidates:
        if normalise_name(existing.full_name) == wanted:
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


async def upcoming_for_patient(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    phone: str,
    full_name: str,
    now: datetime,
    limit: int = 10,
) -> list[models.Appointment]:
    """A patient's future bookings, found by the number and name they gave.

    The number alone returned everything booked under it, which on a shared
    family mobile is somebody else's appointments — visible, and cancellable,
    to whoever rang. Reception asks for a name; so does this.

    It is still not authentication. A name is not a secret either, and someone
    who knows both can still see and cancel. What it does is stop one person's
    number being a key to another person's record, and reduce a lookup on a
    guessed number from a list of everybody to nothing at all.

    Row level security keeps the lookup inside this clinic, so a number that
    exists at another practice finds nothing here.
    """
    rows = (
        (
            await session.execute(
                select(models.Appointment, models.Patient)
                .join(models.Patient, models.Appointment.patient_id == models.Patient.id)
                .where(
                    models.Patient.phone == phone,
                    models.Appointment.status == "confirmed",
                    models.Appointment.starts_at >= now,
                )
                .order_by(models.Appointment.starts_at)
            )
        )
        .tuples()
        .all()
    )
    # Filtered here rather than in SQL: the match is on words, and expressing
    # that as a LIKE invites a name containing a wildcard to widen it.
    matched = [
        appointment for appointment, patient in rows if name_matches(full_name, patient.full_name)
    ]
    return matched[:limit]


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


async def add_usage(session: AsyncSession, *, conversation_id: uuid.UUID, usage: Usage) -> None:
    """Add one model call's tokens to the conversation's running total.

    Called the moment a completion comes back, before anything decides what to
    do with it. Recording at the end instead would miss the turns that end in a
    refusal, a provider failure, or the loop guard, and those are paid for like
    any other.
    """
    await session.execute(
        update(models.Conversation)
        .where(models.Conversation.id == conversation_id)
        .values(
            input_tokens=models.Conversation.input_tokens + usage.input_tokens,
            output_tokens=models.Conversation.output_tokens + usage.output_tokens,
            cached_tokens=models.Conversation.cached_tokens + usage.cache_read_tokens,
        )
    )


async def usage_since(session: AsyncSession, *, since: datetime) -> dict[str, int]:
    """Token totals for this clinic's conversations started since `since`."""
    row = (
        await session.execute(
            select(
                func.count(models.Conversation.id),
                func.coalesce(func.sum(models.Conversation.input_tokens), 0),
                func.coalesce(func.sum(models.Conversation.output_tokens), 0),
                func.coalesce(func.sum(models.Conversation.cached_tokens), 0),
            ).where(models.Conversation.created_at >= since)
        )
    ).one()
    return {
        "conversations": row[0],
        "input_tokens": row[1],
        "output_tokens": row[2],
        "cached_tokens": row[3],
    }


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


async def lapsed_hold_for_conversation(
    session: AsyncSession, *, conversation_id: uuid.UUID, now: datetime
) -> models.Appointment | None:
    """The most recent hold this conversation placed and then let go.

    Told only that nothing is held, a model reads its own transcript saying
    "Held: Crown Fitting with Dr. Hale" and believes that instead. A hedge
    makes it worse: "either booked or expired" offers two possibilities and
    names neither, so the concrete sentence wins. This is what lets the turn
    say which one happened, and to what.
    """
    return (
        await session.execute(
            select(models.Appointment)
            .where(
                models.Appointment.conversation_id == conversation_id,
                models.Appointment.status.in_(("held", "expired")),
                models.Appointment.hold_expires_at <= now,
            )
            .order_by(models.Appointment.hold_expires_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


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


async def update_patient_details(
    session: AsyncSession,
    clinic_id: uuid.UUID,
    *,
    patient_id: uuid.UUID,
    full_name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
) -> models.Patient | None:
    """Correct what a patient typed about themselves.

    Contact details only — this table holds nothing clinical, so there is
    nothing here that needs a practitioner's judgement to change. A mistyped
    name is exactly the sort of thing a receptionist fixes without ceremony.
    """
    patient = await session.get(models.Patient, patient_id)
    if patient is None:
        return None
    if full_name:
        patient.full_name = full_name
    if phone:
        patient.phone = phone
    if email is not None:
        patient.email = email or None
    await session.flush()
    return patient
