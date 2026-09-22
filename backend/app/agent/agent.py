"""The conversation loop.

One turn is: hand the model the transcript and the tools, run whatever it asks
for, hand back the results, repeat until it has something to say. The loop is
written here rather than taken from an SDK helper because it has to do three
things a generic runner does not — persist every turn under the clinic's tenant
scope, stop when a conversation has been escalated, and behave identically
whichever vendor is answering.

What the model cannot do is decide anything. It picks which question to ask;
`app.services.booking` and `app.domain.scheduling` answer it. Swapping providers
cannot change a booking outcome because no booking logic lives on that side of
the boundary.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import tools as agent_tools
from app.agent.prompt import build_system_prompt
from app.config import get_settings
from app.db import models
from app.db import repository as repo
from app.domain.intervals import Interval
from app.providers.llm import (
    Completion,
    LLMError,
    LLMProvider,
    Message,
    ToolCall,
    Usage,
    get_provider,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# Said when the model itself is unavailable. A patient should never see a stack
# trace, and should always be told what to do instead.
UNAVAILABLE = (
    "I am having trouble reaching the booking system right now. "
    "Please try again in a moment, or call the practice directly."
)

ESCALATED = (
    "I have passed this to the practice — someone will follow up with you. "
    "Is there anything else I can help you arrange?"
)

# Reasons that genuinely hand the conversation to a person. A patient who is
# being told to go to hospital, or who asked for a human, should not then get
# a bot answering in parallel.
#
# "out_of_scope" is deliberately absent. Declining to discuss insurance is not
# a handover; the patient may still want to book, and locking the composer
# after a question the assistant simply does not answer strands them.
HANDOVER_REASONS = {"urgent_symptoms", "patient_request", "system_error"}


@dataclass
class AgentReply:
    conversation_id: uuid.UUID
    text: str
    escalated: bool = False
    escalation_reason: str | None = None
    tools_used: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    provider: str = ""
    model: str = ""


class Agent:
    """Runs one patient turn to completion.

    Stateless between turns: the transcript lives in the database, so a second
    instance can pick up a conversation the first one started. That is what
    makes this deployable behind more than one container.
    """

    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider or get_provider(settings.llm_provider)

    async def respond(
        self,
        session: AsyncSession,
        clinic_id: uuid.UUID,
        *,
        text: str,
        conversation_id: uuid.UUID | None = None,
        channel: str = "chat",
        now: datetime | None = None,
    ) -> AgentReply:
        now = now or datetime.now(UTC)

        conversation = await repo.get_or_create_conversation(
            session,
            clinic_id,
            conversation_id=conversation_id,
            channel=channel,
            llm_provider=self.provider.name,
            llm_model=self.provider.model,
        )

        await repo.append_message(
            session, clinic_id, conversation_id=conversation.id, role="user", content=text
        )

        # An escalated conversation belongs to a person now. Continuing to book
        # around them is how a patient ends up with two different answers —
        # but only for the reasons that really are a handover.
        if (
            conversation.escalated_at is not None
            and conversation.escalation_reason in HANDOVER_REASONS
        ):
            await repo.append_message(
                session,
                clinic_id,
                conversation_id=conversation.id,
                role="assistant",
                content=ESCALATED,
            )
            return AgentReply(
                conversation_id=conversation.id,
                text=ESCALATED,
                escalated=True,
                escalation_reason=conversation.escalation_reason,
                provider=self.provider.name,
                model=self.provider.model,
            )

        system = await self._system_prompt(session, clinic_id, now=now)
        turn_context = await self._context(
            session, clinic_id, conversation_id=conversation.id, now=now
        )
        transcript = await self._transcript(session, conversation_id=conversation.id)

        tool_context = agent_tools.ToolContext(
            session=session,
            clinic_id=clinic_id,
            conversation_id=conversation.id,
            timezone=(await repo.load_policy(session, clinic_id)).timezone,
            now=now,
        )

        used: list[str] = []
        usage = Usage()

        for _ in range(settings.agent_max_tool_rounds):
            try:
                completion = await self.provider.complete(
                    system=system,
                    messages=transcript,
                    tools=agent_tools.DEFINITIONS,
                    max_tokens=1024,
                    context=turn_context,
                )
            except LLMError as exc:
                logger.warning(
                    "llm call failed", extra={"provider": exc.provider, "error": str(exc)}
                )
                await repo.append_message(
                    session,
                    clinic_id,
                    conversation_id=conversation.id,
                    role="assistant",
                    content=UNAVAILABLE,
                )
                return AgentReply(
                    conversation_id=conversation.id,
                    text=UNAVAILABLE,
                    provider=self.provider.name,
                    model=self.provider.model,
                    usage=usage,
                    tools_used=used,
                )

            usage = usage + completion.usage

            if completion.stop_reason == "refusal":
                return await self._finish(
                    session,
                    clinic_id,
                    conversation,
                    text=self._refusal_text(completion),
                    used=used,
                    usage=usage,
                )

            if not completion.wants_tools:
                return await self._finish(
                    session,
                    clinic_id,
                    conversation,
                    text=completion.text or ESCALATED,
                    used=used,
                    usage=usage,
                )

            transcript.append(
                Message(
                    role="assistant",
                    content=completion.text,
                    tool_calls=completion.tool_calls,
                )
            )
            await repo.append_message(
                session,
                clinic_id,
                conversation_id=conversation.id,
                role="assistant",
                content=completion.text,
                tool_calls=[
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in completion.tool_calls
                ],
            )

            for call in completion.tool_calls:
                used.append(call.name)
                result = await self._run_tool(call, tool_context)
                transcript.append(Message(role="tool", content=result, tool_call_id=call.id))
                await repo.append_message(
                    session,
                    clinic_id,
                    conversation_id=conversation.id,
                    role="tool",
                    content=result,
                    tool_calls=[{"id": call.id, "name": call.name}],
                )

            await session.refresh(conversation)
            if conversation.escalation_reason in HANDOVER_REASONS and (
                conversation.escalated_at is not None
            ):
                # The escalate tool fired. Let the model phrase the handover in
                # its next turn rather than cutting the patient off mid-sentence.
                continue

        # Out of rounds. Something is looping; a person should look at it.
        await repo.mark_escalated(session, conversation_id=conversation.id, reason="system_error")
        return await self._finish(
            session,
            clinic_id,
            conversation,
            text=(
                "I am having difficulty completing that. I have passed it to the "
                "practice so someone can help you directly."
            ),
            used=used,
            usage=usage,
            escalated=True,
        )

    async def _run_tool(self, call: ToolCall, context: agent_tools.ToolContext) -> str:
        try:
            return await agent_tools.execute(call.name, call.arguments, context)
        except Exception as exc:  # noqa: BLE001
            # A tool that raises is a bug, not a conversation outcome. Tell the
            # model plainly so it can offer something else instead of stalling.
            logger.exception("tool failed", extra={"tool": call.name})
            return f"That did not work: {type(exc).__name__}. Do not retry it."

    async def _finish(
        self,
        session: AsyncSession,
        clinic_id: uuid.UUID,
        conversation: models.Conversation,
        *,
        text: str,
        used: list[str],
        usage: Usage,
        escalated: bool = False,
    ) -> AgentReply:
        await repo.append_message(
            session, clinic_id, conversation_id=conversation.id, role="assistant", content=text
        )
        await session.refresh(conversation)
        handed_over = (
            conversation.escalated_at is not None
            and conversation.escalation_reason in HANDOVER_REASONS
        )
        return AgentReply(
            conversation_id=conversation.id,
            text=text,
            escalated=escalated or handed_over,
            escalation_reason=conversation.escalation_reason,
            tools_used=used,
            usage=usage,
            provider=self.provider.name,
            model=self.provider.model,
        )

    def _refusal_text(self, completion: Completion) -> str:
        logger.info("provider declined", extra={"reason": completion.refusal_reason})
        return (
            "I am not able to help with that one. I can book, move or cancel an "
            "appointment, or put you through to the practice."
        )

    async def _system_prompt(
        self, session: AsyncSession, clinic_id: uuid.UUID, *, now: datetime
    ) -> str:
        clinic = await session.get(models.Clinic, clinic_id)
        services = await repo.load_services(session, clinic_id)
        schedules = await repo.load_schedules(
            session, clinic_id, window=Interval(now, now + timedelta(days=1))
        )
        practitioners = [
            (s.practitioner.slug, s.practitioner.name, sorted(s.practitioner.service_codes))
            for s in schedules
        ]
        # Deliberately contains nothing that changes between turns. The
        # current time is passed separately as context, because a timestamp
        # here would change the cached prefix every minute and the only symptom
        # would be a larger bill.
        return build_system_prompt(
            clinic_name=clinic.name if clinic else "the practice",
            contact_phone=clinic.contact_phone if clinic else None,
            services=services,
            practitioners=practitioners,
        )

    async def _context(
        self,
        session: AsyncSession,
        clinic_id: uuid.UUID,
        *,
        conversation_id: uuid.UUID,
        now: datetime,
    ) -> str:
        """Per-turn facts the model needs but must not cache.

        Booking state is included because the assistant cannot see the
        confirmation card. Without it, a patient who has just completed the
        form is told to complete the form — the assistant has no tool that
        books, so it has no record that anything did.

        Read from the database rather than announced by the client, so it
        reflects what was actually booked.
        """
        policy = await repo.load_policy(session, clinic_id)
        local = now.astimezone(policy.tz)
        lines = [f"The current date and time at the practice is {local:%A %d %B %Y, %-I:%M %p}."]

        services = await repo.load_services(session, clinic_id)

        practitioners = {
            s.practitioner.slug: s.practitioner.name
            for s in await repo.load_schedules(
                session, clinic_id, window=Interval(now, now + timedelta(days=1))
            )
        }
        slugs = await repo.practitioner_slugs_by_id(session, clinic_id)

        def describe(appointment) -> str:
            service = services.get(appointment.service_code)
            name = service.name if service else appointment.service_code
            who = practitioners.get(slugs.get(appointment.practitioner_id, ""), "")
            when = appointment.starts_at.astimezone(policy.tz)
            with_who = f" with {who}" if who else ""
            # The id is included so a move can name the appointment it
            # replaces. Without it the assistant had to ask the patient for
            # their phone number to look up a booking it had just made.
            return (
                f"{name}{with_who} on {when:%A %d %B at %-I:%M %p} "
                f"(appointment_id={appointment.id})"
            )

        confirmed = await repo.confirmed_for_conversation(session, conversation_id=conversation_id)
        if confirmed:
            booked = "; ".join(describe(a) for a in confirmed)
            lines.append(
                f"This patient has already completed the form and these appointments are "
                f"booked: {booked}. Do not ask them to fill in the form again. Treat these "
                f"as settled unless they ask to change one."
            )

        hold = await repo.active_hold_for_conversation(
            session, conversation_id=conversation_id, now=now
        )
        if hold is not None:
            minutes = max(0, int((hold.hold_expires_at - now).total_seconds() // 60))
            lines.append(
                f"A slot is held and showing on the patient's screen as a confirmation "
                f"form: {describe(hold)}, for about {minutes} more minutes. It is not "
                f"booked until they submit that form."
            )
        else:
            # Stated rather than omitted. The transcript still contains the
            # assistant's own earlier "held, ask them to confirm", and silence
            # does not override a sentence the model can still read. An
            # explicit negative does.
            lines.append(
                "No slot is currently held, and no confirmation form is on the "
                "patient's screen. Any hold you placed earlier has either become "
                "one of the booked appointments above or expired."
            )

        return "\n".join(lines)

    async def _transcript(
        self, session: AsyncSession, *, conversation_id: uuid.UUID
    ) -> list[Message]:
        """Rebuild the normalised transcript from storage.

        Tool results are replayed with their original call ids so the provider
        can match them up; a turn that lost its pairing would be rejected.
        """
        out: list[Message] = []
        for row in await repo.load_transcript(session, conversation_id=conversation_id):
            if row.role == "tool":
                call = (row.tool_calls or [{}])[0]
                out.append(Message(role="tool", content=row.content, tool_call_id=call.get("id")))
            elif row.role == "assistant":
                calls = tuple(
                    ToolCall(
                        id=c["id"],
                        name=c["name"],
                        arguments=c.get("arguments") or {},
                    )
                    for c in (row.tool_calls or [])
                    if c.get("id") and c.get("name")
                )
                out.append(Message(role="assistant", content=row.content, tool_calls=calls))
            else:
                out.append(Message(role="user", content=row.content))
        return out
