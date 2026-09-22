"""The tools the assistant may call.

This list is the scope guard that actually holds. The system prompt asks the
model not to give medical advice; this file means it has no way to look anything
up, quote anything, or change anything outside of appointments — there is no
tool for it. A prompt can be argued with. A missing capability cannot.

Every tool is a thin wrapper over `app.services.booking`, which is a thin
wrapper over the scheduling engine. The model chooses *what* to ask; it never
computes an answer.

**There is deliberately no tool that confirms a booking.** The assistant can
offer times and hold one, and that is where its authority ends. Confirming is a
direct call to `POST /api/appointments` from a card the patient has read and
acted on, with the name and phone number they typed.

Speech is misheard and models are agreeable; an appointment is something a
person arranges their day around. Making the last step unreachable from here is
the difference between a guardrail the model is asked to respect and one it
cannot cross.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository as repo
from app.domain import errors
from app.domain.intervals import Interval
from app.providers.calendar import CalendarError
from app.providers.llm import ToolDefinition
from app.services import booking

# How far ahead a patient may ask about in one query.
MAX_SEARCH_DAYS = 60


@dataclass
class ToolContext:
    """Everything a tool needs that the model must not be able to choose.

    The clinic and the conversation come from the session, never from the
    model's arguments. Otherwise a patient could ask the assistant to look at
    another practice's diary and the assistant would oblige.
    """

    session: AsyncSession
    clinic_id: uuid.UUID
    conversation_id: uuid.UUID
    timezone: str
    now: datetime

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


DEFINITIONS: list[ToolDefinition] = [
    ToolDefinition(
        name="list_services",
        description=(
            "List the treatments this practice offers, with how long each takes "
            "and which practitioners perform it."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    ),
    ToolDefinition(
        name="find_availability",
        description=(
            "Find appointment times for a treatment. Returns the earliest "
            "options, already checked against the practitioners' own calendars. "
            "Use this before offering any time to a patient."
        ),
        parameters={
            "type": "object",
            "properties": {
                "service_code": {
                    "type": "string",
                    "description": "The service code, for example A or C.",
                },
                "practitioner_slug": {
                    "type": ["string", "null"],
                    "description": "Restrict to one practitioner. Null means any.",
                },
                "earliest": {
                    "type": ["string", "null"],
                    "description": (
                        "ISO-8601 date or datetime to search from. Null means as soon as possible."
                    ),
                },
                "days": {
                    "type": "integer",
                    "description": "How many days ahead to search. 14 is a sensible default.",
                },
            },
            "required": ["service_code", "practitioner_slug", "earliest", "days"],
        },
    ),
    ToolDefinition(
        name="hold_slot",
        description=(
            "Reserve a time for a few minutes while the patient decides. Does "
            "not book it. Call this once the patient has chosen a time, then "
            "show them the details to confirm."
        ),
        parameters={
            "type": "object",
            "properties": {
                "service_code": {"type": "string"},
                "practitioner_slug": {"type": "string"},
                "starts_at": {
                    "type": "string",
                    "description": "ISO-8601 start time, exactly as returned by find_availability.",
                },
            },
            "required": ["service_code", "practitioner_slug", "starts_at"],
        },
    ),
    ToolDefinition(
        name="find_my_appointments",
        description="Look up a patient's upcoming appointments by phone number.",
        parameters={
            "type": "object",
            "properties": {"phone": {"type": "string"}},
            "required": ["phone"],
        },
    ),
    ToolDefinition(
        name="cancel_appointment",
        description="Cancel an appointment the patient has identified.",
        parameters={
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string"},
                "reason": {"type": ["string", "null"]},
            },
            "required": ["appointment_id", "reason"],
        },
    ),
    ToolDefinition(
        name="escalate",
        description=(
            "Hand the conversation to the practice. Use for urgent symptoms, "
            "for anything you are not allowed to answer, and whenever a patient "
            "asks for a person."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": ["urgent_symptoms", "out_of_scope", "patient_request", "system_error"],
                },
                "summary": {
                    "type": "string",
                    "description": "One sentence for the staff member picking this up.",
                },
            },
            "required": ["reason", "summary"],
        },
    ),
]

NAMES = {tool.name for tool in DEFINITIONS}


async def execute(name: str, arguments: dict[str, Any], context: ToolContext) -> str:
    """Run one tool and return what the model should read.

    Returns prose rather than JSON. The model reads it and speaks to a patient;
    a sentence it can quote is more useful than a structure it has to describe,
    and it keeps invented field names out of the conversation.

    Domain errors are returned as text rather than raised. A slot being taken is
    a normal thing that happens during a booking conversation, and the assistant
    should recover from it by offering another time, not by apologising for a
    system failure.
    """
    if name not in NAMES:
        return f"There is no tool called {name}."

    handler = _HANDLERS[name]
    try:
        return await handler(arguments, context)
    except errors.BookingError as exc:
        return exc.message
    except CalendarError:
        return (
            "I cannot reach the practitioners' calendars at the moment, so I "
            "cannot confirm what is free. Please try again shortly."
        )


# --- handlers --------------------------------------------------------------


async def _list_services(_: dict[str, Any], ctx: ToolContext) -> str:
    services = await repo.load_services(ctx.session, ctx.clinic_id)
    schedules = await repo.load_schedules(
        ctx.session, ctx.clinic_id, window=Interval(ctx.now, ctx.now + timedelta(days=1))
    )
    by_service: dict[str, list[str]] = {}
    for schedule in schedules:
        for code in schedule.practitioner.service_codes:
            by_service.setdefault(code, []).append(schedule.practitioner.name)

    lines = []
    for code, service in sorted(services.items()):
        who = ", ".join(sorted(by_service.get(code, []))) or "nobody currently"
        lines.append(
            f"{code}: {service.name}, {service.duration_minutes} minutes. Performed by {who}."
        )
    return "\n".join(lines)


async def _find_availability(args: dict[str, Any], ctx: ToolContext) -> str:
    days = min(max(int(args.get("days") or 14), 1), MAX_SEARCH_DAYS)
    earliest = _parse_when(args.get("earliest"), ctx) or ctx.now
    if earliest < ctx.now:
        earliest = ctx.now

    service, slots = await booking.find_availability(
        ctx.session,
        ctx.clinic_id,
        service_code=args["service_code"],
        search=Interval(earliest, earliest + timedelta(days=days)),
        practitioner_slug=args.get("practitioner_slug") or None,
        limit=6,
        now=ctx.now,
    )

    if not slots:
        return (
            f"No {service.name} appointments are available in the {days} days "
            f"from {earliest.astimezone(ctx.tz):%A %d %B}. Try a longer window "
            "or a different practitioner."
        )

    names = {s.practitioner.slug: s.practitioner.name for s in await _schedules(ctx)}
    lines = [
        f"{_when(slot.start, ctx)} with {names.get(slot.practitioner_slug, slot.practitioner_slug)}"
        f" ({slot.practitioner_slug}), starts_at={slot.start.isoformat()}"
        for slot in slots
    ]
    return f"{service.name} ({service.duration_minutes} minutes). Available:\n" + "\n".join(lines)


async def _hold_slot(args: dict[str, Any], ctx: ToolContext) -> str:
    starts_at = _parse_when(args["starts_at"], ctx)
    if starts_at is None:
        return "That start time was not a valid date and time."

    hold = await booking.create_hold(
        ctx.session,
        ctx.clinic_id,
        service_code=args["service_code"],
        practitioner_slug=args["practitioner_slug"],
        starts_at=starts_at,
        conversation_id=ctx.conversation_id,
        now=ctx.now,
    )
    held_for = int((hold.hold_expires_at - ctx.now).total_seconds() // 60)
    return (
        f"Held. hold_id={hold.id}. {_when(hold.starts_at, ctx)} until "
        f"{_when(hold.ends_at, ctx)}. Reserved for about {held_for} minutes — "
        "show the patient these details and ask them to confirm."
    )


async def _find_my_appointments(args: dict[str, Any], ctx: ToolContext) -> str:
    appointments = await repo.upcoming_for_phone(
        ctx.session, ctx.clinic_id, phone=args["phone"], now=ctx.now
    )
    if not appointments:
        return "No upcoming appointments are registered to that number."

    services = await repo.load_services(ctx.session, ctx.clinic_id)
    names = {s.practitioner.slug: s.practitioner.name for s in await _schedules(ctx)}
    ids = await repo.practitioner_slugs_by_id(ctx.session, ctx.clinic_id)

    lines = []
    for appointment in appointments:
        slug = ids.get(appointment.practitioner_id, "")
        service = services.get(appointment.service_code)
        lines.append(
            f"{_when(appointment.starts_at, ctx)}: "
            f"{service.name if service else appointment.service_code} with "
            f"{names.get(slug, slug)}, appointment_id={appointment.id}"
        )
    return "\n".join(lines)


async def _cancel_appointment(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        appointment_id = uuid.UUID(str(args["appointment_id"]))
    except ValueError:
        return "That appointment reference is not valid."

    appointment = await booking.cancel_appointment(
        ctx.session,
        ctx.clinic_id,
        appointment_id=appointment_id,
        reason=args.get("reason") or None,
    )
    return f"Cancelled the appointment on {_when(appointment.starts_at, ctx)}."


async def _escalate(args: dict[str, Any], ctx: ToolContext) -> str:
    """Mark the conversation for a human.

    Recorded rather than merely spoken, so the practice can see what the
    assistant could not handle. A bot that quietly declines things nobody ever
    reviews is a bot that hides its own gaps.
    """
    reason = args.get("reason", "patient_request")
    await repo.mark_escalated(
        ctx.session,
        conversation_id=ctx.conversation_id,
        reason=reason,
    )
    await repo.record_audit(
        ctx.session,
        ctx.clinic_id,
        actor="bot",
        action="conversation.escalated",
        entity_type="conversation",
        entity_id=ctx.conversation_id,
        detail={"reason": reason, "summary": args.get("summary", "")},
    )
    if reason == "urgent_symptoms":
        return (
            "Flagged as urgent. Tell the patient to call the practice now, and "
            "to go to an emergency department if they cannot reach anyone or "
            "the symptoms are severe. Do not continue booking."
        )
    return "Flagged for a staff member. Tell the patient someone will follow up."


_HANDLERS = {
    "list_services": _list_services,
    "find_availability": _find_availability,
    "hold_slot": _hold_slot,
    "find_my_appointments": _find_my_appointments,
    "cancel_appointment": _cancel_appointment,
    "escalate": _escalate,
}


# --- formatting ------------------------------------------------------------


async def _schedules(ctx: ToolContext):
    return await repo.load_schedules(
        ctx.session, ctx.clinic_id, window=Interval(ctx.now, ctx.now + timedelta(days=1))
    )


def _when(moment: datetime, ctx: ToolContext) -> str:
    """Clinic-local and human. The model should never do timezone arithmetic."""
    return f"{moment.astimezone(ctx.tz):%A %d %B at %-I:%M %p}"


def _parse_when(value: Any, ctx: ToolContext) -> datetime | None:
    """Accept what a model plausibly produces: a date, or a time with or without a zone.

    A bare date or a naive datetime is read as clinic-local, because that is
    what the patient meant. Guessing UTC would silently shift every request by
    the offset.
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{text}T00:00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ctx.tz)
    return parsed.astimezone(UTC)
