"""Adapter tests for the language model port.

The claim these defend is "the app is model-agnostic". That is not proven by a
provider interface existing; it is proven by both adapters turning the same
neutral conversation into their own dialect and turning their own answers back
into the same neutral result. The last test in this file is that equivalence,
asserted directly.

Providers are driven with stub clients rather than HTTP, because what is under
test is the translation, not the vendors' transport.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.providers.llm import Message, ToolCall, ToolDefinition
from app.providers.llm.anthropic_provider import AnthropicProvider
from app.providers.llm.base import LLMError
from app.providers.llm.openai_provider import OpenAIProvider

TOOLS = [
    ToolDefinition(
        name="find_availability",
        description="Find appointment times.",
        parameters={
            "type": "object",
            "properties": {
                "service_code": {"type": "string"},
                "days": {"type": "integer"},
            },
            "required": ["service_code", "days"],
        },
    )
]

# One conversation that has already been through a tool round, expressed in the
# neutral types. Both adapters must be able to replay it.
TRANSCRIPT = [
    Message(role="user", content="I need a cleaning next week"),
    Message(
        role="assistant",
        content="Let me look.",
        tool_calls=(
            ToolCall(
                id="call_1", name="find_availability", arguments={"service_code": "A", "days": 7}
            ),
        ),
    ),
    Message(role="tool", content="Tuesday at 9am with Dr. Hale", tool_call_id="call_1"),
]


class StubAnthropic:
    """Enough of the Anthropic client surface to capture one request."""

    def __init__(self, response: Any) -> None:
        self.captured: dict[str, Any] = {}
        self._response = response
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.captured = kwargs
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class StubOpenAI:
    def __init__(self, response: Any) -> None:
        self.captured: dict[str, Any] = {}
        self._response = response
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.captured = kwargs
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def anthropic_response(*, blocks: list[Any], stop_reason: str = "end_turn", **extra: Any) -> Any:
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=120, output_tokens=45),
        **extra,
    )


def openai_response(
    *, content: str | None = None, tool_calls: list[Any] | None = None, finish_reason: str = "stop"
) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, tool_calls=tool_calls, refusal=None),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=45),
    )


# --- Anthropic -------------------------------------------------------------


async def test_anthropic_sends_tool_results_as_blocks_in_one_user_message() -> None:
    """Splitting results across messages teaches the model to stop calling tools
    in parallel, which costs a round trip on every later turn."""
    stub = StubAnthropic(
        anthropic_response(blocks=[SimpleNamespace(type="text", text="Tuesday works")])
    )
    provider = AnthropicProvider(model="claude-opus-5", client=stub)

    transcript = [
        *TRANSCRIPT,
        Message(role="tool", content="Also Wednesday at 2pm", tool_call_id="call_2"),
    ]
    await provider.complete(system="S", messages=transcript, tools=TOOLS)

    sent = stub.captured["messages"]
    tool_result_messages = [
        m
        for m in sent
        if isinstance(m["content"], list) and m["content"][0]["type"] == "tool_result"
    ]
    assert len(tool_result_messages) == 1
    assert len(tool_result_messages[0]["content"]) == 2


async def test_anthropic_puts_tool_calls_inside_the_assistant_message() -> None:
    stub = StubAnthropic(anthropic_response(blocks=[SimpleNamespace(type="text", text="ok")]))
    await AnthropicProvider(model="claude-opus-5", client=stub).complete(
        system="S", messages=TRANSCRIPT, tools=TOOLS
    )

    assistant = next(m for m in stub.captured["messages"] if m["role"] == "assistant")
    kinds = [block["type"] for block in assistant["content"]]
    assert kinds == ["text", "tool_use"]
    assert assistant["content"][1]["id"] == "call_1"


async def test_anthropic_marks_tools_strict_with_a_closed_schema() -> None:
    """Strict schemas are what let the tool handlers skip defensive parsing."""
    stub = StubAnthropic(anthropic_response(blocks=[SimpleNamespace(type="text", text="ok")]))
    await AnthropicProvider(model="claude-opus-5", client=stub).complete(
        system="S", messages=TRANSCRIPT, tools=TOOLS
    )

    tool = stub.captured["tools"][0]
    assert tool["strict"] is True
    assert tool["input_schema"]["additionalProperties"] is False
    assert set(tool["input_schema"]["required"]) == {"service_code", "days"}


async def test_anthropic_asks_for_a_refusal_fallback_on_opus_5() -> None:
    stub = StubAnthropic(anthropic_response(blocks=[SimpleNamespace(type="text", text="ok")]))
    await AnthropicProvider(model="claude-opus-5", client=stub).complete(
        system="S", messages=TRANSCRIPT, tools=TOOLS
    )
    assert stub.captured["fallbacks"] == "default"


async def test_anthropic_reports_a_refusal_rather_than_an_empty_answer() -> None:
    stub = StubAnthropic(
        anthropic_response(
            blocks=[],
            stop_reason="refusal",
            stop_details=SimpleNamespace(type="refusal", category="other", explanation="declined"),
        )
    )
    completion = await AnthropicProvider(model="claude-opus-5", client=stub).complete(
        system="S", messages=TRANSCRIPT, tools=TOOLS
    )
    assert completion.stop_reason == "refusal"
    assert completion.refusal_reason == "declined"


async def test_anthropic_separates_retryable_failures_from_permanent_ones() -> None:
    for status, retryable in ((429, True), (503, True), (400, False)):
        error = RuntimeError("boom")
        error.status_code = status
        stub = StubAnthropic(error)
        with pytest.raises(LLMError) as exc:
            await AnthropicProvider(model="claude-opus-5", client=stub).complete(
                system="S", messages=TRANSCRIPT, tools=TOOLS
            )
        assert exc.value.retryable is retryable


# --- OpenAI ----------------------------------------------------------------


async def test_openai_puts_the_system_prompt_in_the_message_list() -> None:
    stub = StubOpenAI(openai_response(content="Tuesday works"))
    await OpenAIProvider(model="gpt-5", client=stub).complete(
        system="You are the assistant", messages=TRANSCRIPT, tools=TOOLS
    )
    assert stub.captured["messages"][0] == {
        "role": "system",
        "content": "You are the assistant",
    }


async def test_openai_gives_tool_results_their_own_role() -> None:
    stub = StubOpenAI(openai_response(content="ok"))
    await OpenAIProvider(model="gpt-5", client=stub).complete(
        system="S", messages=TRANSCRIPT, tools=TOOLS
    )
    result = next(m for m in stub.captured["messages"] if m["role"] == "tool")
    assert result["tool_call_id"] == "call_1"
    assert result["content"] == "Tuesday at 9am with Dr. Hale"


async def test_openai_serialises_tool_arguments_as_a_json_string() -> None:
    stub = StubOpenAI(openai_response(content="ok"))
    await OpenAIProvider(model="gpt-5", client=stub).complete(
        system="S", messages=TRANSCRIPT, tools=TOOLS
    )
    assistant = next(m for m in stub.captured["messages"] if m["role"] == "assistant")
    arguments = assistant["tool_calls"][0]["function"]["arguments"]
    assert isinstance(arguments, str)
    assert json.loads(arguments) == {"service_code": "A", "days": 7}


async def test_openai_parses_tool_arguments_back_into_data() -> None:
    stub = StubOpenAI(
        openai_response(
            finish_reason="tool_calls",
            tool_calls=[
                SimpleNamespace(
                    id="call_9",
                    function=SimpleNamespace(
                        name="find_availability",
                        arguments='{"service_code": "C", "days": 14}',
                    ),
                )
            ],
        )
    )
    completion = await OpenAIProvider(model="gpt-5", client=stub).complete(
        system="S", messages=TRANSCRIPT, tools=TOOLS
    )
    assert completion.stop_reason == "tool_use"
    assert completion.tool_calls[0].arguments == {"service_code": "C", "days": 14}


async def test_openai_rejects_unparseable_tool_arguments() -> None:
    stub = StubOpenAI(
        openai_response(
            finish_reason="tool_calls",
            tool_calls=[
                SimpleNamespace(
                    id="call_9",
                    function=SimpleNamespace(name="find_availability", arguments="{not json"),
                )
            ],
        )
    )
    with pytest.raises(LLMError):
        await OpenAIProvider(model="gpt-5", client=stub).complete(
            system="S", messages=TRANSCRIPT, tools=TOOLS
        )


# --- the equivalence that matters ------------------------------------------


async def test_both_providers_produce_the_same_normalised_result() -> None:
    """The model-agnostic claim, asserted rather than described.

    Two vendors, two wire formats, one `Completion`. Everything above the port
    — the agent loop, the tools, the guardrails, the booking transaction — sees
    only the right-hand side of this comparison, which is why swapping vendors
    cannot change what gets booked.
    """
    claude = StubAnthropic(
        anthropic_response(
            blocks=[
                SimpleNamespace(type="text", text="Let me check."),
                SimpleNamespace(
                    type="tool_use",
                    id="call_1",
                    name="find_availability",
                    input={"service_code": "A", "days": 7},
                ),
            ],
            stop_reason="tool_use",
        )
    )
    gpt = StubOpenAI(
        openai_response(
            content="Let me check.",
            finish_reason="tool_calls",
            tool_calls=[
                SimpleNamespace(
                    id="call_1",
                    function=SimpleNamespace(
                        name="find_availability",
                        arguments='{"service_code": "A", "days": 7}',
                    ),
                )
            ],
        )
    )

    from_claude = await AnthropicProvider(model="claude-opus-5", client=claude).complete(
        system="S", messages=TRANSCRIPT, tools=TOOLS
    )
    from_gpt = await OpenAIProvider(model="gpt-5", client=gpt).complete(
        system="S", messages=TRANSCRIPT, tools=TOOLS
    )

    assert from_claude == from_gpt
    assert from_claude.tool_calls[0].arguments == {"service_code": "A", "days": 7}
    assert from_claude.stop_reason == "tool_use"

    # And the wire formats they were built from really are different, so the
    # equality above is doing work rather than comparing two identical paths.
    assert "system" in claude.captured and "system" not in gpt.captured
    assert claude.captured["tools"][0]["input_schema"]
    assert gpt.captured["tools"][0]["function"]["parameters"]
