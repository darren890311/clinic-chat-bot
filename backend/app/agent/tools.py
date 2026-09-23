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

from app.db import models
from app.db import repository as repo
from app.domain import errors
from app.domain.intervals import Interval
from app.providers.calendar import CalendarError
from app.providers.llm import ToolDefinition
from app.services import booking

# How far ahead a patient may ask about in one query.
MAX_SEARCH_DAYS = 60
# Enough to cover several full days of quarter-hour starts without letting a
# wide search flood the context.
MAX_SLOTS = 200
MAX_DAYS_SHOWN = 8


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
            "Find appointment times for a treatment, already checked against "
            "the practitioners' own calendars. Returns every start time that "
            "works in the range, grouped by day, each one listing every "
            "practitioner free at it — so you can answer both 'when can I come "
            "in?' and 'who can see me at 2?' without searching again. Use this "
            "before offering any time to a patient."
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
                "moving_appointment_id": {
                    "type": ["string", "null"],
                    "description": (
                        "When the patient is moving an existing appointment, its "
                        "id. That appointment is then ignored when working out "
                        "what is free, so they are not blocked by the slot they "
                        "are giving up. Null otherwise."
                    ),
                },
            },
            "required": [
                "service_code",
                "practitioner_slug",
                "earliest",
                "days",
                "moving_appointment_id",
            ],
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
                "replaces_appointment_id": {
                    "type": ["string", "null"],
                    "description": (
                        "When the patient is moving an existing appointment, its "
                        "id. That appointment is cancelled automatically when "
                        "this one is confirmed, so do not cancel it yourself. "
                        "Null for a new booking."
                    ),
                },
            },
            "required": [
                "service_code",
                "practitioner_slug",
                "starts_at",
                "replaces_appointment_id",
            ],
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
        name="correct_my_details",
        description=(
            "Fix the name, phone number or email recorded against this "
            "patient's bookings. Use it when they say something was typed or "
            "heard wrong. Contact details only — nothing clinical."
        ),
        parameters={
            "type": "object",
            "properties": {
                "full_name": {"type": ["string", "null"]},
                "phone": {"type": ["string", "null"]},
                "email": {"type": ["string", "null"]},
            },
            "required": ["full_name", "phone", "email"],
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


async def _in_scope(ctx: ToolContext, appointment_id: uuid.UUID) -> bool:
    """Whether this conversation was ever legitimately given this appointment.

    Three tools act on an appointment id: cancel, and the two halves of a move.
    Row level security keeps an id inside its own clinic; within a clinic,
    nothing stopped one conversation cancelling another's booking. The id is
    unguessable, which is not the same as being checked.

    There are two honest ways to have learned one — this conversation booked
    it, or the patient gave a phone number and the lookup returned it — and
    both are recorded. Anything else is a model that has invented or
    mis-copied a UUID, which is the failure this actually guards against.

    It is not authentication. The phone is whatever the patient said; nothing
    sends a code to it. What it buys is that an id on its own is no longer
    enough to act on.
    """
    appointment = await repo.get_appointment(ctx.session, appointment_id)
    if appointment is None:
        return False
    if appointment.conversation_id == ctx.conversation_id:
        return True

    conversation = await ctx.session.get(models.Conversation, ctx.conversation_id)
    phone = conversation.identified_phone if conversation else None
    if not phone or appointment.patient_id is None:
        return False

    patient = await ctx.session.get(models.Patient, appointment.patient_id)
    return patient is not None and patient.phone == phone


# Said to the model, not to the patient. Deliberately the same answer as a
# genuinely missing appointment: a distinct "you may not touch that" would
# confirm the id exists to anyone probing with one.
OUT_OF_SCOPE = (
    "No such appointment is available in this conversation. If the patient has "
    "a booking, ask for the phone number on it and call find_my_appointments "
    "first."
)


async def _find_availability(args: dict[str, Any], ctx: ToolContext) -> str:
    """Every time that works, grouped by day, rather than the first few.

    Returning a truncated list caused the assistant to tell patients a time was
    unavailable when it was simply past the cut-off: asked for 11:45, it saw
    six slots ending at 10:15 and said no. A day's worth of quarter-hour starts
    is a short line of text, and being able to answer "is 11:45 free?" is worth
    far more than the tokens it costs.
    """
    days = min(max(int(args.get("days") or 14), 1), MAX_SEARCH_DAYS)
    earliest = _parse_when(args.get("earliest"), ctx) or ctx.now
    if earliest < ctx.now:
        earliest = ctx.now

    moving = args.get("moving_appointment_id")
    try:
        moving_id = uuid.UUID(str(moving)) if moving else None
    except ValueError:
        return "That appointment reference is not valid."
    if moving_id is not None and not await _in_scope(ctx, moving_id):
        return OUT_OF_SCOPE

    service, slots = await booking.find_availability(
        ctx.session,
        ctx.clinic_id,
        service_code=args["service_code"],
        search=Interval(earliest, earliest + timedelta(days=days)),
        practitioner_slug=args.get("practitioner_slug") or None,
        limit=MAX_SLOTS,
        moving_appointment_id=moving_id,
        now=ctx.now,
    )

    if not slots:
        return (
            f"No {service.name} appointments are available in the {days} days "
            f"from {earliest.astimezone(ctx.tz):%A %d %B}. Try a longer window "
            "or a different practitioner."
        )

    names = {s.practitioner.slug: s.practitioner.name for s in await _schedules(ctx)}

    # Grouped by time, not by practitioner.
    #
    # Practitioner-major grouping made the question patients actually ask —
    # "who can see me at 2?" — answerable only by cross-referencing two long
    # lists of quarter-hour starts. The assistant did not, and offered "2 PM
    # with Dr. Okafor" from Dr. Okafor's line while Dr. Hale sat free at 2 PM
    # one line above. Every word of it was true and it reads as "Dr. Hale is
    # busy then". A shape that makes the honest answer the easy one beats a
    # prompt asking for the honest answer.
    grouped: dict[str, dict[str, list[str]]] = {}
    for slot in slots:
        local = slot.start.astimezone(ctx.tz)
        day = grouped.setdefault(f"{local:%A %d %B}", {})
        day.setdefault(f"{local:%-I:%M %p}", []).append(slot.practitioner_slug)

    shown = list(grouped.items())[:MAX_DAYS_SHOWN]
    appearing = sorted({slot.practitioner_slug for slot in slots})

    lines = [f"{service.name}, {service.duration_minutes} minutes."]
    if len(appearing) == 1:
        slug = appearing[0]
        lines.append(f"All of these are with {names.get(slug, slug)} ({slug}). Start times:")
        for day, times in shown:
            lines.append(f"- {day}: {', '.join(times.keys())}")
    else:
        lines.append(
            "Practitioners: "
            + ", ".join(f"{names.get(s, s)} = {s}" for s in appearing)
            + ". Below, each day's start times are grouped by who is free at "
            "them. A time appears exactly once, under everyone who can take "
            "it — so if a group names two practitioners, both are free then "
            "and the patient may choose. Say so rather than naming one."
        )
        for day, times in shown:
            # Names once per group of times rather than once per time. Naming
            # them against all forty quarter-hour starts said the same thing
            # and cost roughly twice the tokens.
            by_who: dict[str, list[str]] = {}
            for time, who in times.items():
                by_who.setdefault(", ".join(who), []).append(time)
            lines.append(f"- {day}:")
            lines.extend(f"    {who}: {', '.join(ts)}" for who, ts in by_who.items())

    if len(grouped) > len(shown):
        more = len(grouped) - len(shown)
        lines.append(
            f"{more} further {'day' if more == 1 else 'days'} with free times "
            "beyond these are not listed; search from a later date to see them."
        )

    # Local time, no offset. Offering a UTC example beside clinic-local display
    # times invited the model to staple the two together: it read "11:45 AM",
    # wrote "11:45:00+00:00", and asked to hold 07:45 clinic time.
    lines.append(
        "These are clinic local times. Pass one to hold_slot exactly as "
        "YYYY-MM-DDTHH:MM with no timezone offset, for example "
        f"{slots[0].start.astimezone(ctx.tz):%Y-%m-%dT%H:%M}."
    )
    if len(slots) >= MAX_SLOTS:
        lines.append("There may be more beyond this range; narrow the dates to see further.")
    return "\n".join(lines)


async def _hold_slot(args: dict[str, Any], ctx: ToolContext) -> str:
    starts_at = _parse_when(args["starts_at"], ctx)
    if starts_at is None:
        return "That start time was not a valid date and time."

    replaces = args.get("replaces_appointment_id")
    try:
        replaces_id = uuid.UUID(str(replaces)) if replaces else None
    except ValueError:
        return "That appointment reference is not valid."
    if replaces_id is not None and not await _in_scope(ctx, replaces_id):
        return OUT_OF_SCOPE

    try:
        hold = await booking.create_hold(
            ctx.session,
            ctx.clinic_id,
            service_code=args["service_code"],
            practitioner_slug=args["practitioner_slug"],
            starts_at=starts_at,
            conversation_id=ctx.conversation_id,
            replaces_appointment_id=replaces_id,
            now=ctx.now,
        )
    except errors.SlotUnavailable:
        # Say which time was actually understood. A bare "not available" hid a
        # timezone mistake behind a plausible-sounding business answer, and the
        # assistant repeated it to the patient as fact.
        raise errors.SlotUnavailable(
            f"{_when(starts_at, ctx)} is not available. "
            "Check you sent a clinic local time with no timezone offset, then "
            "call find_availability again if needed."
        ) from None
    held_for = int((hold.hold_expires_at - ctx.now).total_seconds() // 60)
    moving = (
        " The appointment it replaces is cancelled automatically when this one "
        "is confirmed; do not cancel it yourself."
        if replaces_id
        else ""
    )
    return (
        f"Held. hold_id={hold.id}. {_when(hold.starts_at, ctx)} until "
        f"{_when(hold.ends_at, ctx)}. Reserved for about {held_for} minutes — "
        f"tell the patient the form is on their screen.{moving}"
    )


async def _find_my_appointments(args: dict[str, Any], ctx: ToolContext) -> str:
    appointments = await repo.upcoming_for_phone(
        ctx.session, ctx.clinic_id, phone=args["phone"], now=ctx.now
    )
    if not appointments:
        return "No upcoming appointments are registered to that number."

    # The patient has identified themselves with this number, so the ids about
    # to be handed to the model become ones it may act on. Recorded on the
    # conversation rather than inferred later from the transcript: an
    # authorisation decision should not depend on parsing stored tool-call
    # JSON back out again.
    conversation = await ctx.session.get(models.Conversation, ctx.conversation_id)
    if conversation is not None:
        conversation.identified_phone = args["phone"]
        await ctx.session.flush()

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
    if not await _in_scope(ctx, appointment_id):
        return OUT_OF_SCOPE

    appointment = await booking.cancel_appointment(
        ctx.session,
        ctx.clinic_id,
        appointment_id=appointment_id,
        reason=args.get("reason") or None,
    )
    return f"Cancelled the appointment on {_when(appointment.starts_at, ctx)}."


async def _correct_my_details(args: dict[str, Any], ctx: ToolContext) -> str:
    """Correct the contact details on this conversation's bookings.

    Scoped to appointments made in this conversation. Letting the assistant
    edit a patient found by any other means would turn a typo fix into a way
    to overwrite somebody else's record.
    """
    appointments = await repo.confirmed_for_conversation(
        ctx.session, conversation_id=ctx.conversation_id
    )
    patient_ids = {a.patient_id for a in appointments if a.patient_id}
    if not patient_ids:
        return (
            "There are no confirmed bookings in this conversation yet, so there "
            "is nothing to correct. The details are taken from the form."
        )

    updated = None
    for patient_id in patient_ids:
        updated = await repo.update_patient_details(
            ctx.session,
            ctx.clinic_id,
            patient_id=patient_id,
            full_name=args.get("full_name") or None,
            phone=args.get("phone") or None,
            email=args.get("email"),
        )

    if updated is None:
        return "I could not find the record to correct."

    await repo.record_audit(
        ctx.session,
        ctx.clinic_id,
        actor="bot",
        action="patient.details_corrected",
        entity_type="patient",
        entity_id=updated.id,
        detail={k: v for k, v in args.items() if v},
    )
    return (
        f"Updated. The bookings now read: {updated.full_name}, {updated.phone}"
        f"{', ' + updated.email if updated.email else ''}. "
        "Read it back to the patient so they can check it."
    )


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
    "correct_my_details": _correct_my_details,
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
