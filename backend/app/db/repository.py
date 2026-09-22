"""Loading domain objects out of the database.

Everything here returns the plain dataclasses the scheduling engine expects, so
the engine never sees a SQLAlchemy object and stays trivially testable.
"""

from __future__ import annotations

import uuid
from datetime import time

from sqlalchemy import select, text
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
) -> list[PractitionerSchedule]:
    """Practitioners plus every block of time they are already committed for.

    Internal bookings only. External calendar busy is layered on by the booking
    service, which fetches it live per request; see `merge_external_busy`.
    """
    stmt = select(models.Practitioner).where(models.Practitioner.is_active.is_(True))
    if service_code:
        stmt = stmt.where(models.Practitioner.service_codes.any(service_code))
    practitioners = list((await session.execute(stmt)).scalars())

    appointments = list(
        (
            await session.execute(
                select(models.Appointment).where(
                    models.Appointment.status.in_(OCCUPYING),
                    models.Appointment.ends_at > window.start,
                    models.Appointment.starts_at < window.end,
                )
            )
        ).scalars()
    )

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
        text(
            "UPDATE appointments SET status = 'expired'"
            " WHERE status = 'held' AND hold_expires_at < now()"
        )
    )
    return result.rowcount or 0
