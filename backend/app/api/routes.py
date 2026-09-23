from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.agent import Agent
from app.config import get_settings
from app.db import models
from app.db import repository as repo
from app.db.session import tenant_session, unscoped_session
from app.domain import errors
from app.domain.intervals import Interval
from app.domain.scheduling import compute_availability
from app.providers.calendar import CalendarError, registered_providers
from app.providers.llm import registered_providers as llm_providers
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


class ClinicOut(BaseModel):
    """What the client needs to render times the way the practice reads them.

    The timezone is sent explicitly because the browser's own is irrelevant and
    actively misleading: a patient is walking into a building, and the building
    has one clock. Formatting in the viewer's zone produced an assistant saying
    11:45 AM beside a confirmation saying 11:45 PM.
    """

    name: str
    timezone: str
    contact_phone: str | None = None


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
        "llm_providers": llm_providers(),
        "calendar_providers": registered_providers(),
    }


@router.get("/clinic", response_model=ClinicOut)
async def clinic_info(clinic_id: uuid.UUID = Depends(current_clinic_id)) -> ClinicOut:
    async with tenant_session(clinic_id) as session:
        clinic = await session.get(models.Clinic, clinic_id)
    if clinic is None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return ClinicOut(name=clinic.name, timezone=clinic.timezone, contact_phone=clinic.contact_phone)


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


# --- conversation ----------------------------------------------------------


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: uuid.UUID | None = None
    channel: str = Field(default="chat", pattern="^(chat|voice)$")


class PendingHold(BaseModel):
    """A slot reserved but not yet booked.

    Sent so the client can render a confirmation card. The assistant has no
    tool that confirms a booking; the patient acting on this card is what does.
    """

    hold_id: uuid.UUID
    service_code: str
    service_name: str
    practitioner_name: str
    starts_at: datetime
    ends_at: datetime
    expires_at: datetime


class BookedAppointment(BaseModel):
    """A confirmed appointment, as the practice has it recorded.

    The patient's name and phone come from the database rather than from what
    the client remembers typing, so a correction made in conversation shows up
    on the card the patient is being asked to check.
    """

    appointment_id: uuid.UUID
    service_name: str
    practitioner_name: str
    starts_at: datetime
    ends_at: datetime
    patient_name: str | None = None
    patient_phone: str | None = None


class ChatReply(BaseModel):
    conversation_id: uuid.UUID
    reply: str
    escalated: bool = False
    escalation_reason: str | None = None
    tools_used: list[str] = Field(default_factory=list)
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    # Read back from the database after the turn, not reported by the model.
    pending_hold: PendingHold | None = None
    booked: list[BookedAppointment] = Field(default_factory=list)


@router.get("/conversations/{conversation_id}/bookings", response_model=list[BookedAppointment])
async def conversation_bookings(
    conversation_id: uuid.UUID,
    clinic_id: uuid.UUID = Depends(current_clinic_id),
) -> list[BookedAppointment]:
    """What this conversation currently has booked.

    Exists so the client never keeps its own copy. It guessed before: after a
    reschedule it showed the replaced appointment alongside its replacement,
    and after a name correction it kept showing the name that had been
    corrected.
    """
    async with tenant_session(clinic_id) as session:
        services = await repo.load_services(session, clinic_id)
        out = []
        for appointment in await repo.confirmed_for_conversation(
            session, conversation_id=conversation_id
        ):
            practitioner = await repo.get_practitioner_by_id(session, appointment.practitioner_id)
            patient = (
                await session.get(models.Patient, appointment.patient_id)
                if appointment.patient_id
                else None
            )
            service = services.get(appointment.service_code)
            out.append(
                BookedAppointment(
                    appointment_id=appointment.id,
                    service_name=service.name if service else appointment.service_code,
                    practitioner_name=practitioner.name if practitioner else "",
                    starts_at=appointment.starts_at,
                    ends_at=appointment.ends_at,
                    patient_name=patient.full_name if patient else None,
                    patient_phone=patient.phone if patient else None,
                )
            )
    return out


@router.post("/chat", response_model=ChatReply)
async def chat(
    body: ChatRequest,
    clinic_id: uuid.UUID = Depends(current_clinic_id),
) -> ChatReply:
    """One turn of conversation.

    The whole turn runs inside a single tenant-scoped transaction, so a booking
    made mid-conversation and the transcript recording it either both land or
    neither does.

    `provider` and `model` are returned deliberately: being able to watch them
    change while the behaviour does not is the point of the abstraction.
    """
    agent = Agent()
    now = datetime.now(UTC)

    async with tenant_session(clinic_id) as session:
        reply = await agent.respond(
            session,
            clinic_id,
            text=body.message,
            conversation_id=body.conversation_id,
            channel=body.channel,
        )

        services = await repo.load_services(session, clinic_id)
        hold = await repo.active_hold_for_conversation(
            session, conversation_id=reply.conversation_id, now=now
        )
        pending = None
        if hold is not None:
            practitioner = await repo.get_practitioner_by_id(session, hold.practitioner_id)
            service = services.get(hold.service_code)
            pending = PendingHold(
                hold_id=hold.id,
                service_code=hold.service_code,
                service_name=service.name if service else hold.service_code,
                practitioner_name=practitioner.name if practitioner else "",
                starts_at=hold.starts_at,
                ends_at=hold.ends_at,
                expires_at=hold.hold_expires_at,
            )

        booked = []
        for appointment in await repo.confirmed_for_conversation(
            session, conversation_id=reply.conversation_id
        ):
            practitioner = await repo.get_practitioner_by_id(session, appointment.practitioner_id)
            service = services.get(appointment.service_code)
            patient = (
                await session.get(models.Patient, appointment.patient_id)
                if appointment.patient_id
                else None
            )
            booked.append(
                BookedAppointment(
                    appointment_id=appointment.id,
                    service_name=service.name if service else appointment.service_code,
                    practitioner_name=practitioner.name if practitioner else "",
                    starts_at=appointment.starts_at,
                    ends_at=appointment.ends_at,
                    patient_name=patient.full_name if patient else None,
                    patient_phone=patient.phone if patient else None,
                )
            )

    return ChatReply(
        conversation_id=reply.conversation_id,
        reply=reply.text,
        escalated=reply.escalated,
        escalation_reason=reply.escalation_reason,
        tools_used=reply.tools_used,
        provider=reply.provider,
        model=reply.model,
        input_tokens=reply.usage.input_tokens,
        output_tokens=reply.usage.output_tokens,
        cached_tokens=reply.usage.cache_read_tokens,
        pending_hold=pending,
        booked=booked,
    )
