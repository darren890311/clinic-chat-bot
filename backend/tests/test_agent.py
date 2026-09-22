"""Agent behaviour, driven by a scripted provider against a real database.

The model is replaced with a script. That is deliberate: what is under test is
the loop, the guardrails and the persistence — not whether a particular model
happens to choose the right tool today. A test that calls a real model tests
the vendor, costs money, and fails for reasons unrelated to this code.

The scenarios are the ones with consequences: a booking that completes, a
request the assistant must refuse, symptoms it must not assess, a loop that
must terminate, and a provider swap that must change nothing.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent import Agent
from app.db import models
from app.providers.llm import Completion, LLMError, ToolCall, Usage

APP_URL = os.environ.get("TEST_APP_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not APP_URL, reason="set TEST_APP_DATABASE_URL to run database tests"
)

CLINIC = uuid.UUID("55555555-5555-5555-5555-555555555555")
SENIOR = uuid.UUID("aaaaaaaa-5555-5555-5555-555555555555")
CLINIC_TZ = ZoneInfo("America/New_York")

WINDOWS = [{"weekday": d, "start": "09:00", "end": "18:00"} for d in range(5)]
SERVICES = [("A", "Routine Cleaning", 60), ("C", "Root Canal Treatment", 150)]

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)  # Monday, 06:00 clinic time


def monday(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 5, hour, minute, tzinfo=CLINIC_TZ).astimezone(UTC)


class ScriptedProvider:
    """Returns prepared completions in order and records what it was asked.

    Also captures the tool definitions and system prompt it was handed, so a
    test can assert on the agent's side of the contract rather than only on the
    replies it produces.
    """

    name = "scripted"
    model = "script-1"

    def __init__(self, script: list[Completion | Exception]) -> None:
        self.script = list(script)
        self.calls = 0
        self.seen_tools: list[str] = []
        self.seen_system: str = ""
        self.seen_transcripts: list[list[Any]] = []
        # Every system prompt and context it was handed, in order, so a test
        # can check the cached half really is stable.
        self.systems: list[str] = []
        self.contexts: list[str | None] = []

    async def complete(
        self, *, system, messages, tools, max_tokens=1024, context=None
    ) -> Completion:
        self.calls += 1
        self.seen_system = system
        self.seen_tools = [t.name for t in tools]
        self.seen_transcripts.append(list(messages))
        self.systems.append(system)
        self.contexts.append(context)
        if not self.script:
            return Completion(text="Anything else?")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def says(text_: str) -> Completion:
    return Completion(text=text_, usage=Usage(10, 5))


def calls(name: str, **arguments: Any) -> Completion:
    return Completion(
        text="",
        tool_calls=(ToolCall(id=f"c{uuid.uuid4().hex[:6]}", name=name, arguments=arguments),),
        stop_reason="tool_use",
        usage=Usage(10, 5),
    )


def _async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1).replace("sslmode=", "ssl=")


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(_async_url(APP_URL), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(text("SELECT set_config('app.clinic_id', :c, true)"), {"c": str(CLINIC)})
        s.add(
            models.Clinic(
                id=CLINIC,
                slug=f"agent-{CLINIC.hex[:8]}",
                name="Test Dental",
                timezone="America/New_York",
                scheduling_policy={},
                contact_phone="+1 555 0100",
            )
        )
        for code, name, minutes in SERVICES:
            s.add(
                models.Service(
                    clinic_id=CLINIC,
                    code=code,
                    name=name,
                    duration_minutes=minutes,
                    description="",
                )
            )
        s.add(
            models.Practitioner(
                id=SENIOR,
                clinic_id=CLINIC,
                slug="dr-hale",
                name="Dr. Hale",
                title="Senior Dentist",
                seniority="senior",
                service_codes=["A", "C"],
                working_windows=WINDOWS,
            )
        )
        await s.flush()
        try:
            yield s
        finally:
            await s.rollback()
    await engine.dispose()


async def _conversation(s, conversation_id) -> models.Conversation:
    await s.refresh(await s.get(models.Conversation, conversation_id))
    return await s.get(models.Conversation, conversation_id)


# --- a booking that completes ----------------------------------------------


async def test_the_agent_can_hold_a_slot_but_not_book_it(session) -> None:
    """The last step is deliberately out of the model's reach.

    Speech is misheard and models are agreeable. The assistant offers times and
    reserves one; the appointment is made by a patient acting on a card, via
    `POST /api/appointments`. There is no tool here that could do it.
    """
    provider = ScriptedProvider(
        [
            calls(
                "find_availability", service_code="A", practitioner_slug=None, earliest=None, days=7
            ),
            calls(
                "hold_slot",
                service_code="A",
                practitioner_slug="dr-hale",
                starts_at=monday(9).isoformat(),
            ),
            says("Held. Please complete the form on your screen."),
        ]
    )
    reply = await Agent(provider).respond(session, CLINIC, text="I need a cleaning", now=NOW)

    assert reply.tools_used == ["find_availability", "hold_slot"]
    assert "confirm_booking" not in provider.seen_tools

    status = (
        await session.execute(
            text("SELECT status FROM appointments WHERE clinic_id = :c"), {"c": CLINIC}
        )
    ).scalar_one()
    assert status == "held", "the agent must not be able to produce a confirmed appointment"


async def test_no_tool_can_confirm_a_booking(session) -> None:
    """Structural, not a matter of prompting.

    If a confirm tool is ever added back, this fails — which is the point. The
    guarantee is that the capability is absent, not that the model was asked
    not to use it.
    """
    provider = ScriptedProvider([says("Hello")])
    await Agent(provider).respond(session, CLINIC, text="Book me in", now=NOW)

    for name in provider.seen_tools:
        assert "confirm" not in name, f"{name} would let the model book without the patient"


async def test_the_transcript_is_persisted_and_replayed(session) -> None:
    """A second turn must see the first. The transcript lives in the database
    so another container can continue a conversation this one started."""
    provider = ScriptedProvider([says("Hello, how can I help?")])
    agent = Agent(provider)
    first = await agent.respond(session, CLINIC, text="Hi", now=NOW)

    provider.script = [says("Of course.")]
    await agent.respond(
        session, CLINIC, text="I need a cleaning", conversation_id=first.conversation_id, now=NOW
    )

    replayed = provider.seen_transcripts[-1]
    assert [m.role for m in replayed][:3] == ["user", "assistant", "user"]
    assert replayed[0].content == "Hi"
    assert replayed[2].content == "I need a cleaning"


# --- scope ------------------------------------------------------------------


async def test_the_model_is_only_ever_offered_appointment_tools(session) -> None:
    """The structural half of the scope guard.

    The prompt asks the assistant not to give medical advice. This is why it
    cannot: there is no tool that would let it look anything up, and no tool
    that touches anything but appointments.
    """
    provider = ScriptedProvider([says("I can help with appointments.")])
    await Agent(provider).respond(session, CLINIC, text="What is my diagnosis?", now=NOW)

    assert sorted(provider.seen_tools) == [
        "cancel_appointment",
        "escalate",
        "find_availability",
        "find_my_appointments",
        "hold_slot",
        "list_services",
    ]


async def test_the_brief_tells_the_model_what_it_must_not_do(session) -> None:
    provider = ScriptedProvider([says("ok")])
    await Agent(provider).respond(session, CLINIC, text="Hello", now=NOW)

    prompt = provider.seen_system
    assert "do not give clinical or medical advice" in prompt.lower()
    assert "+1 555 0100" in prompt  # the number to call in an emergency
    assert "Routine Cleaning" in prompt and "Root Canal Treatment" in prompt
    assert "Dr. Hale" in prompt


async def test_an_out_of_scope_request_escalates_and_is_recorded(session) -> None:
    provider = ScriptedProvider(
        [
            calls("escalate", reason="out_of_scope", summary="Asked about insurance billing"),
            says("I cannot help with billing, but I can pass you to the practice."),
        ]
    )
    reply = await Agent(provider).respond(
        session, CLINIC, text="How much does my insurance cover?", now=NOW
    )

    assert reply.escalated is True
    assert reply.escalation_reason == "out_of_scope"

    actions = (
        (
            await session.execute(
                text("SELECT action FROM audit_log WHERE clinic_id = :c"), {"c": CLINIC}
            )
        )
        .scalars()
        .all()
    )
    assert "conversation.escalated" in actions


async def test_urgent_symptoms_stop_the_booking_flow(session) -> None:
    """The tool result tells the model to stop, rather than leaving it to judge.

    Assessing how serious a symptom is would be practising dentistry. The
    assistant's only correct move is to hand over.
    """
    provider = ScriptedProvider(
        [
            calls("escalate", reason="urgent_symptoms", summary="Severe pain and facial swelling"),
            says("Please call the practice now on +1 555 0100."),
        ]
    )
    reply = await Agent(provider).respond(
        session, CLINIC, text="My face is swollen and it hurts badly", now=NOW
    )

    assert reply.escalated is True
    assert reply.escalation_reason == "urgent_symptoms"

    # The model was told, in the tool result, not to keep booking.
    tool_result = next(
        m for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool"
    )
    assert "emergency department" in tool_result.content
    assert "Do not continue booking" in tool_result.content


async def test_an_escalated_conversation_does_not_go_back_to_the_model(session) -> None:
    """Once a person owns the conversation, a bot answering in parallel is how
    a patient ends up with two different answers."""
    provider = ScriptedProvider(
        [
            calls("escalate", reason="patient_request", summary="Asked for a person"),
            says("Passing you over."),
        ]
    )
    agent = Agent(provider)
    first = await agent.respond(session, CLINIC, text="I want to speak to someone", now=NOW)
    assert first.escalated

    before = provider.calls
    second = await agent.respond(
        session,
        CLINIC,
        text="Actually, book me a cleaning",
        conversation_id=first.conversation_id,
        now=NOW,
    )

    assert provider.calls == before  # the model was not consulted
    assert second.escalated is True
    assert (
        await session.execute(
            text("SELECT count(*) FROM appointments WHERE clinic_id = :c"), {"c": CLINIC}
        )
    ).scalar_one() == 0


# --- failure modes -----------------------------------------------------------


async def test_a_runaway_tool_loop_terminates_and_hands_over(session) -> None:
    """A model that keeps calling tools must not keep costing money forever."""
    provider = ScriptedProvider([calls("list_services") for _ in range(50)])
    reply = await Agent(provider).respond(session, CLINIC, text="What do you offer?", now=NOW)

    assert reply.escalated is True
    assert reply.escalation_reason == "system_error"
    assert provider.calls <= 10  # bounded by agent_max_tool_rounds


async def test_a_provider_outage_produces_an_answer_not_a_stack_trace(session) -> None:
    provider = ScriptedProvider([LLMError("scripted", "upstream down", retryable=True)])
    reply = await Agent(provider).respond(session, CLINIC, text="Hello", now=NOW)

    assert "call the practice directly" in reply.text
    assert reply.escalated is False


async def test_a_failing_tool_is_reported_to_the_model_not_raised(session) -> None:
    """A booking conversation should survive a bug in one tool."""
    provider = ScriptedProvider(
        [
            calls(
                "hold_slot", service_code="A", practitioner_slug="dr-hale", starts_at="not-a-date"
            ),
            says("Sorry, let me try a different time."),
        ]
    )
    reply = await Agent(provider).respond(session, CLINIC, text="Book 9am", now=NOW)

    tool_result = next(
        m for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool"
    )
    assert "not a valid date" in tool_result.content
    assert reply.text == "Sorry, let me try a different time."


async def test_a_slot_taken_mid_conversation_is_explained_not_crashed(session) -> None:
    provider = ScriptedProvider(
        [
            calls(
                "hold_slot",
                service_code="A",
                practitioner_slug="dr-hale",
                starts_at=monday(9).isoformat(),
            ),
            calls(
                "hold_slot",
                service_code="A",
                practitioner_slug="dr-hale",
                starts_at=monday(9).isoformat(),
            ),
            says("That one has gone. How about later?"),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="Book 9am", now=NOW)

    results = [m for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool"]
    assert "not available" in results[-1].content


async def test_a_provider_refusal_is_answered_politely(session) -> None:
    provider = ScriptedProvider(
        [Completion(text="", stop_reason="refusal", refusal_reason="declined")]
    )
    reply = await Agent(provider).respond(session, CLINIC, text="something odd", now=NOW)
    assert "not able to help with that one" in reply.text


# --- the model-agnostic claim, end to end ------------------------------------


async def test_swapping_the_provider_changes_nothing_about_the_booking(session) -> None:
    """Two providers, identical scripts, identical outcome.

    This is the claim the brief asks for. The adapters differ; the tools, the
    guardrails and the scheduling engine do not, so the appointment that comes
    out the other end is the same one.
    """
    outcomes = []
    for name in ("vendor-a", "vendor-b"):
        provider = ScriptedProvider(
            [
                calls(
                    "find_availability",
                    service_code="C",
                    practitioner_slug=None,
                    earliest=None,
                    days=7,
                ),
                calls(
                    "hold_slot",
                    service_code="C",
                    practitioner_slug="dr-hale",
                    starts_at=monday(9).isoformat(),
                ),
                says("Monday at 9am. Confirm?"),
            ]
        )
        provider.name = name
        reply = await Agent(provider).respond(session, CLINIC, text="I need a root canal", now=NOW)

        hold = (
            await session.execute(
                text("SELECT id, starts_at, ends_at FROM appointments WHERE status = 'held'")
            )
        ).one()
        outcomes.append((reply.tools_used, hold.starts_at, hold.ends_at))

        # Clear the hold so the second run starts from the same state.
        await session.execute(text("UPDATE appointments SET status = 'cancelled'"))

    first, second = outcomes
    assert first == second
    assert first[0] == ["find_availability", "hold_slot"]
    assert first[2] - first[1] == timedelta(
        minutes=150
    )  # the service duration, not the model's idea


# --- prompt caching ----------------------------------------------------------


async def test_the_cached_prefix_is_byte_identical_between_turns(session) -> None:
    """The guard on prompt caching.

    Providers cache by prefix. One changing character in the system prompt —
    a timestamp, a request id, a reordered dict — discards the cached work for
    everything after it, and the only symptom is a larger bill and a slower
    reply. Nothing fails, so nothing tells you.

    The current time is therefore passed as `context`, which sits after the
    cache boundary. This test is what stops it drifting back into `system`.
    """
    provider = ScriptedProvider([says("Hello"), says("Hello again")])
    agent = Agent(provider)

    first = await agent.respond(session, CLINIC, text="Hi", now=NOW)
    await agent.respond(
        session,
        CLINIC,
        text="Still there?",
        conversation_id=first.conversation_id,
        now=NOW + timedelta(hours=3, minutes=17),
    )

    assert provider.systems[0] == provider.systems[1], (
        "the system prompt changed between turns; the cached prefix is discarded"
    )
    # The time did move, and it moved in the part that is not cached.
    assert provider.contexts[0] != provider.contexts[1]
    assert "current date and time" in provider.contexts[0]
    # And it is nowhere in the cached half.
    assert "current date and time" not in provider.systems[0]


async def test_availability_returns_the_whole_day_not_the_first_few(session) -> None:
    """A truncated list made the assistant deny times that were free.

    Asked for 11:45, it received six slots ending at 10:15, concluded the time
    was unavailable, and told the patient so. The list is now complete for the
    range, so a question about any particular time can be answered from it.
    """
    provider = ScriptedProvider(
        [
            calls(
                "find_availability",
                service_code="A",
                practitioner_slug="dr-hale",
                earliest=None,
                days=1,
            ),
            says("Here are the times."),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="What is free today?", now=NOW)

    result = next(m for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool")
    # 09:00 to 17:00 on a quarter-hour grid is far more than a handful.
    assert result.content.count(":") > 20
    assert "5:00 PM" in result.content


async def test_the_times_offered_are_clinic_local_with_no_offset(session) -> None:
    """The model once read "11:45 AM" from the list, wrote "11:45:00+00:00",
    and asked to hold 07:45 clinic time — which the engine correctly refused,
    so the assistant told the patient a free slot was taken.

    Mixing clinic-local display times with a UTC example in the same tool
    result is what invited that. The instruction now matches the display.
    """
    provider = ScriptedProvider(
        [
            calls(
                "find_availability",
                service_code="A",
                practitioner_slug="dr-hale",
                earliest=None,
                days=1,
            ),
            says("ok"),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="anything today?", now=NOW)

    result = next(m for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool")
    assert "no timezone offset" in result.content
    assert "+00:00" not in result.content
    assert "Z\n" not in result.content
