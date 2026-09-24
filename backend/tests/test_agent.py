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


async def test_every_practitioner_free_at_a_time_is_named_with_it(session) -> None:
    """The tool result decides whether an honest answer is easy to give.

    Grouped by practitioner, "who is free at 2 PM?" could only be answered by
    cross-referencing two lists of thirty quarter-hour starts. The assistant
    did not, and offered "2 PM with Dr. Okafor" while Dr. Hale was free at 2 PM
    one line above — true, and read by the patient as "Dr. Hale is busy".

    Grouped by time, both names sit against the time, and there is nothing to
    cross-reference.
    """
    session.add(
        models.Practitioner(
            id=uuid.uuid4(),
            clinic_id=CLINIC,
            slug="dr-okafor",
            name="Dr. Okafor",
            title="Senior Dentist",
            seniority="senior",
            service_codes=["A", "C"],
            working_windows=WINDOWS,
        )
    )
    await session.flush()

    provider = ScriptedProvider(
        [
            calls(
                "find_availability",
                service_code="C",
                practitioner_slug=None,
                earliest=None,
                days=2,
            ),
            says("Root canal is 2.5 hours. 9 AM tomorrow with Dr. Hale or Dr. Okafor?"),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="I need a root canal", now=NOW)

    result = next(
        m for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool"
    ).content

    # The 9 AM start is listed once, under both practitioners, so answering
    # "who can see me at 9?" is a lookup rather than a cross-reference.
    grouped = [ln.strip() for ln in result.splitlines() if ln.startswith("    ")]
    assert any(ln.startswith("dr-hale, dr-okafor: ") and "9:00 AM" in ln for ln in grouped)

    # And no group that is one practitioner's whole day, which is the shape
    # that produced the misleading offer.
    assert not any(ln.startswith("dr-hale: ") for ln in grouped)
    assert not any(ln.startswith("dr-okafor: ") for ln in grouped)


async def test_one_practitioner_is_said_once_rather_than_against_every_time(
    session,
) -> None:
    """Naming the same person beside forty start times is noise, not honesty.

    When a search only turns up one practitioner — because the patient asked
    for them, or because nobody else performs the treatment — the result says
    so once and lists bare times.
    """
    provider = ScriptedProvider(
        [
            calls(
                "find_availability",
                service_code="A",
                practitioner_slug=None,
                earliest=None,
                days=2,
            ),
            says("Tomorrow at 9?"),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="I need a cleaning", now=NOW)

    result = next(
        m for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool"
    ).content

    assert "All of these are with Dr. Hale (dr-hale)" in result
    assert "dr-hale:" not in result


async def test_the_brief_forbids_attributing_a_shared_time_to_one_practitioner(
    session,
) -> None:
    """The data shape makes the honest answer easy; this asks for it.

    Both are needed. The tool result can only offer the model the truth — it
    cannot stop it from picking one name out of two and sounding certain.
    """
    provider = ScriptedProvider([says("ok")])
    await Agent(provider).respond(session, CLINIC, text="Hello", now=NOW)

    prompt = provider.seen_system.lower()
    assert "more than one practitioner is free at a time you offer" in prompt
    assert "keep them with the practitioner they already have" in prompt


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
        "correct_my_details",
        "escalate",
        "find_availability",
        "find_my_appointments",
        "hold_slot",
        "list_services",
    ]


async def test_the_brief_pins_the_reply_language(session) -> None:
    """The brief for this practice says English only, and the model drifted.

    Probing it in Chinese, one run answered in English and the next answered in
    Chinese. Either might be the better product; only one of them is the
    specification, and a demonstration that changes language depending on the
    weather is not a demonstration.
    """
    provider = ScriptedProvider([says("ok")])
    await Agent(provider).respond(session, CLINIC, text="hello", now=NOW)

    prompt = provider.seen_system.lower()
    assert "always reply in english" in prompt
    assert "whatever language the patient writes in" in prompt


async def test_the_brief_tells_the_model_what_it_must_not_do(session) -> None:
    provider = ScriptedProvider([says("ok")])
    await Agent(provider).respond(session, CLINIC, text="Hello", now=NOW)

    prompt = provider.seen_system
    assert "do not give clinical or medical advice" in prompt.lower()
    assert "+1 555 0100" in prompt  # the number to call in an emergency
    assert "Routine Cleaning" in prompt and "Root Canal Treatment" in prompt
    assert "Dr. Hale" in prompt


async def test_an_out_of_scope_question_is_recorded_but_does_not_end_the_conversation(
    session,
) -> None:
    """Declining to discuss insurance is not a handover.

    Locking the composer after a question the assistant simply does not answer
    strands a patient who still wants an appointment. The escalation is
    recorded so the practice can see what was asked; the conversation carries
    on.
    """
    provider = ScriptedProvider(
        [
            calls("escalate", reason="out_of_scope", summary="Asked about insurance billing"),
            says("I cannot help with billing, but I can still book you in."),
        ]
    )
    reply = await Agent(provider).respond(
        session, CLINIC, text="How much does my insurance cover?", now=NOW
    )

    assert reply.escalated is False, "an unanswerable question is not a handover"

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

    # And the patient can carry straight on.
    provider.script = [says("Thursday at nine, then?")]
    follow_up = await Agent(provider).respond(
        session,
        CLINIC,
        text="Fine, just book me a cleaning",
        conversation_id=reply.conversation_id,
        now=NOW,
    )
    assert follow_up.text == "Thursday at nine, then?"


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


async def _someone_elses_appointment(
    session, *, name: str = "Someone Else", phone: str = "0900111222", hour: int = 14
) -> models.Appointment:
    """A confirmed booking made in a conversation that is not ours."""
    patient = models.Patient(id=uuid.uuid4(), clinic_id=CLINIC, full_name=name, phone=phone)
    appointment = models.Appointment(
        id=uuid.uuid4(),
        clinic_id=CLINIC,
        practitioner_id=SENIOR,
        patient_id=patient.id,
        service_code="A",
        starts_at=monday(hour),
        ends_at=monday(hour + 1),
        status="confirmed",
        conversation_id=uuid.uuid4(),
    )
    # Flushed first: there is no ORM relationship between the two, so nothing
    # orders the inserts and the appointment's patient_id goes in unbacked.
    session.add(patient)
    await session.flush()
    session.add(appointment)
    await session.flush()
    return appointment


async def test_a_conversation_cannot_cancel_an_appointment_it_was_never_given(
    session,
) -> None:
    """An unguessable id is not a checked id.

    Row level security keeps an appointment inside its own clinic. Within one,
    nothing stopped a conversation cancelling a booking it had no connection
    to. The realistic way that happens is not an attacker — it is a model
    mis-copying or inventing a UUID.
    """
    other = await _someone_elses_appointment(session)

    provider = ScriptedProvider(
        [
            calls("cancel_appointment", appointment_id=str(other.id)),
            says("I could not find that booking."),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="Cancel my appointment", now=NOW)

    result = next(
        m for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool"
    ).content
    assert "ask for the name and phone number" in result

    await session.refresh(other)
    assert other.status == "confirmed", "the booking must survive a call it did not authorise"


async def test_the_refusal_does_not_reveal_that_the_appointment_exists(session) -> None:
    """A distinct "you may not touch that" is an existence oracle.

    Someone probing with ids would learn which ones are real. A missing
    appointment and one belonging to another conversation get the same answer.
    """
    other = await _someone_elses_appointment(session)

    provider = ScriptedProvider(
        [
            calls("cancel_appointment", appointment_id=str(other.id)),
            calls("cancel_appointment", appointment_id=str(uuid.uuid4())),
            says("I could not find that booking."),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="Cancel it", now=NOW)

    results = [
        m.content for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool"
    ]
    assert len(results) == 2
    assert results[0] == results[1]


async def test_the_phone_number_the_patient_gives_brings_it_into_scope(session) -> None:
    """The second honest route to an id: the patient identifies the booking.

    A patient who closed the page and came back has no conversation holding
    their appointment. Giving the number the booking was made under is how
    they get it back — and it is exactly what a model inventing an id cannot
    do.
    """
    other = await _someone_elses_appointment(session)

    provider = ScriptedProvider(
        [
            calls("find_my_appointments", phone="0900111222", full_name="Someone Else"),
            calls("cancel_appointment", appointment_id=str(other.id)),
            says("Cancelled."),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="I need to cancel", now=NOW)

    await session.refresh(other)
    assert other.status == "cancelled"


async def test_a_move_cannot_be_aimed_at_an_appointment_out_of_scope(session) -> None:
    """Both halves of a move take an id, so both are checked.

    Only cancelling would leave the same booking reachable by rescheduling it
    into the past, or onto a slot the patient never asked for.
    """
    other = await _someone_elses_appointment(session)

    provider = ScriptedProvider(
        [
            calls(
                "find_availability",
                service_code="A",
                practitioner_slug=None,
                earliest=None,
                days=7,
                moving_appointment_id=str(other.id),
            ),
            calls(
                "hold_slot",
                service_code="A",
                practitioner_slug="dr-hale",
                starts_at=monday(9).isoformat(),
                replaces_appointment_id=str(other.id),
            ),
            says("I could not find that booking."),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="Move my appointment", now=NOW)

    results = [
        m.content for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool"
    ]
    assert all("ask for the name and phone number" in r for r in results)

    await session.refresh(other)
    assert other.status == "confirmed"

    held = await session.execute(
        text("SELECT count(*) FROM appointments WHERE clinic_id = :c AND status = 'held'"),
        {"c": CLINIC},
    )
    assert held.scalar_one() == 0, "the replacement must not have been held either"


async def test_a_number_shared_by_two_people_does_not_hand_over_both(session) -> None:
    """Families share a mobile. The number alone was a key to both records.

    A parent books for themselves and for a child on one phone. Asked to
    cancel, the assistant used to list everything under the number — the other
    person's appointment included, visible and cancellable to whoever rang.
    """
    darren = await _someone_elses_appointment(session, name="Darren Chen", phone="0915", hour=10)
    kevin = await _someone_elses_appointment(session, name="Kevin Chen", phone="0915", hour=14)

    provider = ScriptedProvider(
        [
            calls("find_my_appointments", phone="0915", full_name="Darren"),
            says("You have one, today at 10."),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="What have I got booked?", now=NOW)

    result = next(
        m for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool"
    ).content
    assert str(darren.id) in result
    assert str(kevin.id) not in result, "the other person on this number must not appear"


async def test_the_other_persons_appointment_cannot_be_cancelled(session) -> None:
    """Listing it and acting on it are two gates, and both have to hold.

    A model that had somehow acquired the id — a stale lookup, a mis-copy —
    must still be refused, because the conversation identified itself as
    somebody else.
    """
    kevin = await _someone_elses_appointment(session, name="Kevin Chen", phone="0915", hour=14)
    await _someone_elses_appointment(session, name="Darren Chen", phone="0915", hour=10)

    provider = ScriptedProvider(
        [
            calls("find_my_appointments", phone="0915", full_name="Darren"),
            calls("cancel_appointment", appointment_id=str(kevin.id)),
            says("I could not find that booking."),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="Cancel it", now=NOW)

    await session.refresh(kevin)
    assert kevin.status == "confirmed"


async def test_a_partial_name_still_finds_the_patient(session) -> None:
    """Forgiving in one direction only.

    Nobody remembers whether they gave a surname six months ago, so every word
    the caller gives must appear in the stored name rather than the reverse.
    "Darren" finds "Darren Chen"; "Darren Smith" finds nothing.
    """
    appointment = await _someone_elses_appointment(session, name="Darren Chen", phone="0915")

    provider = ScriptedProvider(
        [
            calls("find_my_appointments", phone="0915", full_name="darren"),
            says("Found it."),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="What have I got?", now=NOW)
    assert (
        str(appointment.id)
        in next(
            m for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool"
        ).content
    )


async def test_a_failed_lookup_does_not_say_which_half_was_wrong(session) -> None:
    """ "That number exists but the name is wrong" is an existence oracle.

    It is exactly what somebody working through numbers wants to hear, and it
    would turn a failed guess into a confirmed hit.
    """
    await _someone_elses_appointment(session, name="Darren Chen", phone="0915")

    provider = ScriptedProvider(
        [
            calls("find_my_appointments", phone="0915", full_name="Kevin"),
            calls("find_my_appointments", phone="0000", full_name="Kevin"),
            says("Nothing found."),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="Find my booking", now=NOW)

    results = [
        m.content for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool"
    ]
    assert results[0] == results[1], "a real number with a wrong name must look like no number"


async def test_the_brief_forbids_reading_a_name_out(session) -> None:
    """The name is the only thing being checked, so the assistant must not supply it.

    A receptionist does not read the name on the file back to whoever is
    asking; she asks them for it. Handing it over would leave the check
    answering its own question.
    """
    provider = ScriptedProvider([says("ok")])
    await Agent(provider).respond(session, CLINIC, text="Hello", now=NOW)

    prompt = provider.seen_system.lower()
    assert "never read a name out" in prompt
    assert "ask for **both** the phone number" in prompt


async def test_tokens_are_recorded_even_when_the_turn_fails(session) -> None:
    """Recorded as they are spent, not when the turn succeeds.

    A turn has several exits: a refusal, a provider failure, the loop guard. All
    of them are paid for. Writing the total at the end would miss whichever one
    the code forgot, and the practice would be billed for turns nobody counted.

    Here the provider answers once and then fails, so the turn ends on the
    failure path with one call's worth of tokens already spent.
    """
    provider = ScriptedProvider(
        [
            calls("list_services"),
            LLMError("scripted", "upstream is down"),
        ]
    )
    reply = await Agent(provider).respond(session, CLINIC, text="hello", now=NOW)

    conversation = await _conversation(session, reply.conversation_id)
    assert conversation.input_tokens > 0
    assert conversation.output_tokens > 0


async def test_an_untouched_conversation_has_recorded_nothing(session) -> None:
    provider = ScriptedProvider([says("Hello.")])
    reply = await Agent(provider).respond(session, CLINIC, text="hi", now=NOW)
    conversation = await _conversation(session, reply.conversation_id)
    # One call happened, so it is not zero; the point is that it is that call
    # and not a double count of the cumulative figure on the reply.
    assert conversation.input_tokens == reply.usage.input_tokens
    assert conversation.output_tokens == reply.usage.output_tokens


async def test_a_lapsed_hold_is_corrected_at_the_end_of_the_conversation(
    session,
) -> None:
    """Where the correction sits is the whole of this fix.

    Said in the per-turn context, which precedes the transcript, it was
    ignored: the transcript contains the assistant's own "a confirmation form
    is on your screen", and that is the most recent and most concrete thing it
    can read. A patient whose hold had lapsed was told twice more to fill in a
    form that had gone from their screen minutes earlier.

    So it is appended last, where a correction has to go, and only when there
    is something to correct.
    """
    hold = models.Appointment(
        id=uuid.uuid4(),
        clinic_id=CLINIC,
        practitioner_id=SENIOR,
        service_code="A",
        starts_at=monday(9),
        ends_at=monday(10),
        status="held",
        hold_expires_at=NOW - timedelta(minutes=3),
    )
    session.add(hold)
    await session.flush()

    provider = ScriptedProvider([says("That hold lapsed. Shall I take it again?")])
    reply = await Agent(provider).respond(session, CLINIC, text="is it booked?", now=NOW)

    hold.conversation_id = reply.conversation_id
    await session.flush()

    provider = ScriptedProvider([says("ok")])
    await Agent(provider).respond(
        session,
        CLINIC,
        text="hello?",
        conversation_id=reply.conversation_id,
        now=NOW,
    )

    last = provider.seen_transcripts[-1][-1]
    assert "expired 3 minutes ago" in last.content
    assert "out of date" in last.content, "it has to disown what it said before"

    # And not persisted. A third turn is what shows it: if the note were
    # written to the transcript, this turn would carry both the stored copy
    # and the fresh one, and the model would read about the same lapse twice.
    provider = ScriptedProvider([says("ok")])
    await Agent(provider).respond(
        session,
        CLINIC,
        text="right",
        conversation_id=reply.conversation_id,
        now=NOW,
    )
    notes = [
        m
        for m in provider.seen_transcripts[-1]
        if isinstance(getattr(m, "content", None), str) and "[Practice system]" in m.content
    ]
    assert len(notes) == 1, f"one fresh note, never a stored one; got {len(notes)}"


async def test_no_correction_when_there_is_nothing_to_correct(session) -> None:
    """A conversation that never held anything gets no system note."""
    provider = ScriptedProvider([says("Hello.")])
    await Agent(provider).respond(session, CLINIC, text="hi", now=NOW)

    assert not any(
        "[Practice system]" in m.content
        for m in provider.seen_transcripts[-1]
        if isinstance(getattr(m, "content", None), str)
    )


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


async def test_the_context_states_plainly_that_nothing_is_held(session) -> None:
    """An explicit negative, because silence loses to the transcript.

    The assistant's own earlier tool result says "Held — ask them to confirm",
    and that sentence stays in the conversation forever. Omitting the hold from
    the context did not override it: the model kept describing a reservation
    that no longer existed and offered to cancel the booking it had just made
    as a duplicate of itself.
    """
    provider = ScriptedProvider([says("ok")])
    await Agent(provider).respond(session, CLINIC, text="Hello", now=NOW)

    context = provider.contexts[0]
    assert "No slot is currently held" in context
    assert "no confirmation form is on the" in context


async def test_a_completed_booking_is_reported_as_settled(session) -> None:
    """The assistant cannot see the card, so the server has to tell it."""
    from app.db import repository as repo
    from app.services import booking

    conversation = await repo.get_or_create_conversation(
        session,
        CLINIC,
        conversation_id=None,
        channel="chat",
        llm_provider="scripted",
        llm_model="script-1",
    )
    hold = await booking.create_hold(
        session,
        CLINIC,
        service_code="A",
        practitioner_slug="dr-hale",
        starts_at=monday(9),
        conversation_id=conversation.id,
        now=NOW,
    )
    await booking.confirm_appointment(
        session,
        CLINIC,
        hold_id=hold.id,
        patient=booking.PatientDetails(full_name="Alex Tran", phone="+886912345678"),
        now=NOW,
    )

    provider = ScriptedProvider([says("ok")])
    await Agent(provider).respond(
        session, CLINIC, text="Is that sorted?", conversation_id=conversation.id, now=NOW
    )
    context = provider.contexts[0]
    assert "already completed the form" in context
    assert "Do not ask them to fill in the form again" in context


async def test_the_brief_sends_ordinary_pain_to_a_booking_not_a_hospital(session) -> None:
    """The single most common reason to ring a dentist must not be refused.

    An earlier version listed "severe pain" as an emergency, so a patient who
    said their tooth hurt badly was told to go to an emergency department and
    the conversation was locked. That is wrong twice over: it is what the
    practice exists to treat, and it makes the assistant useless at its own
    job.
    """
    provider = ScriptedProvider([says("ok")])
    await Agent(provider).respond(session, CLINIC, text="My tooth hurts", now=NOW)

    prompt = provider.seen_system.lower()
    assert "severe pain" in prompt and "ordinary dental work" in prompt
    assert "search from today rather than next week" in prompt
    # Offering the earliest slot without saying why reads as reading out the
    # next free time. A patient in pain should be told they were moved up.
    assert "say why" in prompt


async def test_the_tool_result_carries_the_urgent_instruction_not_the_brief(
    session,
) -> None:
    """Where an instruction sits decides whether it survives.

    The brief already said to tell a patient with a knocked-out tooth to ring
    the practice. Measured against the real model it produced that sentence
    once in five attempts, and a longer, firmer wording produced it none in
    five. The instruction was not being ignored so much as buried: by the time
    the model writes its reply, the nearest thing in its context is a list of
    times, and a list of times is what it reports.

    So it is said in the tool result instead, which is the last thing read
    before the reply is written. The phone number is also on screen at all
    times, which is the half that does not depend on the model.
    """
    provider = ScriptedProvider(
        [
            calls(
                "find_availability",
                service_code="A",
                practitioner_slug=None,
                earliest=None,
                days=1,
                urgent_symptom="his front tooth came out completely",
            ),
            says("Ring the practice now. The earliest is 9 AM."),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="his tooth came out", now=NOW)

    result = next(
        m for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool"
    ).content

    # Before any time is mentioned, and carrying the number to ring.
    assert result.startswith("URGENT")
    assert "+1 555 0100" in result
    assert result.index("ring the practice") < result.index("Start times")


async def test_the_reply_carries_the_urgent_flag_whatever_the_model_said(
    session,
) -> None:
    """The banner is driven by the classification, not by the wording.

    Measured against the real model over six attempts, it set the flag six
    times and included the instruction to ring in five. So the screen shows the
    number on the strength of the flag. Here the model says nothing about
    ringing at all and the flag still reaches the client.
    """
    provider = ScriptedProvider(
        [
            calls(
                "find_availability",
                service_code="A",
                practitioner_slug=None,
                earliest=None,
                days=1,
                urgent_symptom="his tooth came out",
            ),
            says("The earliest today is 9:00 AM. Shall I hold it?"),
        ]
    )
    reply = await Agent(provider).respond(session, CLINIC, text="his tooth came out", now=NOW)

    assert "ring" not in reply.text.lower(), "the model said nothing; that is the point"
    assert reply.urgent_symptom is True


async def test_an_ordinary_booking_raises_no_alarm(session) -> None:
    """A banner that appears for a cleaning is a banner nobody reads."""
    provider = ScriptedProvider(
        [
            calls(
                "find_availability",
                service_code="A",
                practitioner_slug=None,
                earliest=None,
                days=7,
            ),
            says("Thursday at 9 ?"),
        ]
    )
    reply = await Agent(provider).respond(session, CLINIC, text="a cleaning", now=NOW)
    assert reply.urgent_symptom is False


async def test_availability_without_the_urgent_flag_says_nothing_about_ringing(
    session,
) -> None:
    """Ordinary bookings must not carry an emergency instruction."""
    provider = ScriptedProvider(
        [
            calls(
                "find_availability",
                service_code="A",
                practitioner_slug=None,
                earliest=None,
                days=1,
            ),
            says("Here are some times."),
        ]
    )
    await Agent(provider).respond(session, CLINIC, text="a cleaning please", now=NOW)

    result = next(
        m for m in provider.seen_transcripts[-1] if getattr(m, "role", None) == "tool"
    ).content
    assert "URGENT" not in result


async def test_a_knocked_out_tooth_is_not_sent_to_a_hospital(session) -> None:
    """Re-implantation is time-critical and it is dental work, not A&E work.

    The routing question originally asked whether the injury followed an
    accident, which swept up every sports knock and escalated it away from the
    one place that could treat it.
    """
    provider = ScriptedProvider([says("ok")])
    await Agent(provider).respond(session, CLINIC, text="I lost a tooth", now=NOW)

    prompt = provider.seen_system
    assert "knocked-out or pushed-out adult tooth" in prompt
    assert "belongs at the dentist, not at a hospital" in prompt
    assert "do not escalate instead of booking" in prompt


async def test_the_hospital_criteria_stay_narrow(session) -> None:
    """What genuinely needs a hospital, and nothing wider."""
    provider = ScriptedProvider([says("ok")])
    await Agent(provider).respond(session, CLINIC, text="Hello", now=NOW)

    prompt = provider.seen_system
    for sign in ("spreading towards your eye or neck", "bleeding that will", "blow to the head"):
        assert sign in prompt
    # The phrase that used to catch every sports injury.
    assert "injury after an accident" not in prompt
