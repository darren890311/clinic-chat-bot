from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db import repository as repo
from app.db.session import tenant_session, unscoped_session
from app.domain import errors
from app.domain.intervals import Interval
from app.domain.scheduling import compute_availability
from app.providers.calendar import CalendarError, registered_providers
from app.services import booking

router = APIRouter(prefix="/api")
settings = get_settings()


async def current_clinic_id(
    clinic: str = Query(default=None, description="clinic slug; defaults to the configured clinic"),
) -> uuid.UUID:
    slug = clinic or settings.default_clinic_slug
    async with unscoped_session() as session:
        clinic_id = await repo.resolve_clinic_id(session, slug)
    if clinic_id is None:
        raise HTTPException(status_code=404, detail=f"Unknown clinic {slug!r}")
    return clinic_id


class ServiceOut(BaseModel):
    code: str
    name: str
    duration_minutes: int
    description: str


class SlotOut(BaseModel):
    start: datetime
    end: datetime
    practitioner_slug: str
    practitioner_name: str


class AvailabilityOut(BaseModel):
    service: ServiceOut
    searched_from: datetime
    searched_to: datetime
    slots: list[SlotOut] = Field(default_factory=list)


@router.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "environment": settings.environment,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "calendar_providers": registered_providers(),
    }


@router.get("/services", response_model=list[ServiceOut])
async def list_services(clinic_id: uuid.UUID = Depends(current_clinic_id)) -> list[ServiceOut]:
    async with tenant_session(clinic_id) as session:
        services = await repo.load_services(session, clinic_id)
    return [
        ServiceOut(
            code=s.code,
            name=s.name,
            duration_minutes=s.duration_minutes,
            description=s.description,
        )
        for s in sorted(services.values(), key=lambda s: s.code)
    ]


@router.get("/availability", response_model=AvailabilityOut)
async def availability(
    service: str,
    days: int = Query(default=14, ge=1, le=90),
    start: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    clinic_id: uuid.UUID = Depends(current_clinic_id),
) -> AvailabilityOut:
    now = datetime.now(UTC)
    search_from = start or now
    window = Interval(search_from, search_from + timedelta(days=days))

    async with tenant_session(clinic_id) as session:
        services = await repo.load_services(session, clinic_id)
        if service.upper() not in services:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown service {service!r}. Offered: {sorted(services)}",
            )
        svc = services[service.upper()]
        policy = await repo.load_policy(session, clinic_id)
        schedules = await repo.load_schedules(
            session, clinic_id, window=window, service_code=svc.code
        )

    names = {s.practitioner.slug: s.practitioner.name for s in schedules}
    slots = compute_availability(
        service=svc,
        schedules=schedules,
        search=window,
        policy=policy,
        now=now,
        limit=limit,
    )
    return AvailabilityOut(
        service=ServiceOut(
            code=svc.code,
            name=svc.name,
            duration_minutes=svc.duration_minutes,
            description=svc.description,
        ),
        searched_from=window.start,
        searched_to=window.end,
        slots=[
            SlotOut(
                start=s.start,
                end=s.end,
                practitioner_slug=s.practitioner_slug,
                practitioner_name=names.get(s.practitioner_slug, s.practitioner_slug),
            )
            for s in slots
        ],
    )


# --- booking ---------------------------------------------------------------


class HoldRequest(BaseModel):
    service_code: str = Field(description="A, B, C, D or E")
    practitioner_slug: str
    starts_at: datetime
    conversation_id: uuid.UUID | None = None


class HoldOut(BaseModel):
    hold_id: uuid.UUID
    service_code: str
    practitioner_slug: str
    practitioner_name: str
    starts_at: datetime
    ends_at: datetime
    expires_at: datetime


class PatientIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=3, max_length=32)
    email: str | None = Field(default=None, max_length=320)
    notes: str | None = Field(default=None, max_length=2000)


class ConfirmRequest(BaseModel):
    hold_id: uuid.UUID
    patient: PatientIn


class AppointmentOut(BaseModel):
    appointment_id: uuid.UUID
    status: str
    service_code: str
    practitioner_slug: str
    practitioner_name: str
    starts_at: datetime
    ends_at: datetime
    mirrored_to: list[str] = Field(default_factory=list)
    mirror_error: str | None = None


def _booking_http_error(exc: errors.BookingError) -> HTTPException:
    """Domain errors carry their own status and a message safe to read aloud."""
    return HTTPException(status_code=exc.status_code, detail=exc.message)


async def _appointment_out(session, appointment) -> AppointmentOut:
    practitioner = await repo.get_practitioner_by_id(session, appointment.practitioner_id)
    mirrored = [
        name
        for name, event_id in (
            ("google", appointment.google_event_id),
            ("microsoft", appointment.microsoft_event_id),
        )
        if event_id
    ]
    return AppointmentOut(
        appointment_id=appointment.id,
        status=appointment.status,
        service_code=appointment.service_code,
        practitioner_slug=practitioner.slug if practitioner else "",
        practitioner_name=practitioner.name if practitioner else "",
        starts_at=appointment.starts_at,
        ends_at=appointment.ends_at,
        mirrored_to=mirrored,
        mirror_error=appointment.mirror_error,
    )


@router.post("/holds", response_model=HoldOut, status_code=201)
async def create_hold(
    body: HoldRequest,
    clinic_id: uuid.UUID = Depends(current_clinic_id),
) -> HoldOut:
    """Reserve a slot briefly while the patient decides.

    Separate from confirming because a patient on a call needs time to agree,
    and the slot has to be theirs while they take it.
    """
    async with tenant_session(clinic_id) as session:
        try:
            hold = await booking.create_hold(
                session,
                clinic_id,
                service_code=body.service_code,
                practitioner_slug=body.practitioner_slug,
                starts_at=body.starts_at,
                conversation_id=body.conversation_id,
            )
        except errors.BookingError as exc:
            raise _booking_http_error(exc) from exc
        except CalendarError as exc:
            # We could not read a practitioner's calendar, so we do not know the
            # slot is free. Refuse rather than book over something unseen.
            raise HTTPException(
                status_code=503,
                detail="I cannot check the calendar right now. Please try again shortly.",
            ) from exc

        practitioner = await repo.get_practitioner_by_id(session, hold.practitioner_id)
        return HoldOut(
            hold_id=hold.id,
            service_code=hold.service_code,
            practitioner_slug=body.practitioner_slug,
            practitioner_name=practitioner.name if practitioner else body.practitioner_slug,
            starts_at=hold.starts_at,
            ends_at=hold.ends_at,
            expires_at=hold.hold_expires_at,
        )


@router.post("/appointments", response_model=AppointmentOut, status_code=201)
async def confirm_appointment(
    body: ConfirmRequest,
    clinic_id: uuid.UUID = Depends(current_clinic_id),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> AppointmentOut:
    """Turn a hold into a booking.

    Send the same `Idempotency-Key` when retrying: a dropped response must not
    produce a second appointment.
    """
    async with tenant_session(clinic_id) as session:
        try:
            appointment = await booking.confirm_appointment(
                session,
                clinic_id,
                hold_id=body.hold_id,
                patient=booking.PatientDetails(
                    full_name=body.patient.full_name,
                    phone=body.patient.phone,
                    email=body.patient.email,
                    notes=body.patient.notes,
                ),
                idempotency_key=idempotency_key,
            )
        except errors.BookingError as exc:
            raise _booking_http_error(exc) from exc
        except CalendarError as exc:
            raise HTTPException(
                status_code=503,
                detail="I cannot confirm against the calendar right now. Please try again shortly.",
            ) from exc

        return await _appointment_out(session, appointment)


@router.delete("/appointments/{appointment_id}", response_model=AppointmentOut)
async def cancel_appointment(
    appointment_id: uuid.UUID,
    reason: str | None = Query(default=None, max_length=500),
    clinic_id: uuid.UUID = Depends(current_clinic_id),
) -> AppointmentOut:
    async with tenant_session(clinic_id) as session:
        try:
            appointment = await booking.cancel_appointment(
                session, clinic_id, appointment_id=appointment_id, reason=reason
            )
        except errors.BookingError as exc:
            raise _booking_http_error(exc) from exc

        return await _appointment_out(session, appointment)
