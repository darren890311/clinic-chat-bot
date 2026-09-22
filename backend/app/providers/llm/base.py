"""The language model port.

Nothing above this module knows which vendor is answering. The agent builds one
set of tool definitions, one conversation, and one system prompt; an adapter
translates them into whatever shape its provider wants and translates the answer
back into the types here.

That is what makes the assistant model-agnostic in a way you can demonstrate
rather than assert: change `LLM_PROVIDER` in the environment and the same
booking conversation runs against a different vendor, with the same tools and
the same guardrails, because none of that logic lives in the adapter.

The normalised shapes below are deliberately the intersection of what the
providers agree on. Anything vendor-specific — Anthropic's adaptive thinking,
its refusal fallbacks, OpenAI's response format — is the adapter's business and
does not appear here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["user", "assistant", "tool"]

# Why a turn ended. Providers spell these differently; adapters map onto these.
StopReason = Literal[
    "end_turn",  # the model finished speaking
    "tool_use",  # the model wants tools run before it continues
    "max_tokens",  # truncated; the answer is incomplete
    "refusal",  # the provider declined to answer
]


@dataclass(frozen=True)
class ToolDefinition:
    """A tool the model may call, described once for every provider.

    `parameters` is JSON Schema. Both providers accept it; both also want
    `additionalProperties: false` and a complete `required` list before they
    will guarantee the arguments validate, so `strict_schema` enforces that
    rather than leaving it to whoever writes the next tool.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def strict_schema(self) -> dict[str, Any]:
        schema = dict(self.parameters)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema["additionalProperties"] = False
        schema.setdefault("required", sorted(schema["properties"]))
        return schema


@dataclass(frozen=True)
class ToolCall:
    """A request from the model to run one tool.

    `id` is the provider's correlation handle. It must come back attached to the
    result or the conversation desynchronises, so it is carried rather than
    regenerated.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    # Set on a tool result, naming the call it answers.
    tool_call_id: str | None = None
    is_error: bool = False


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True)
class Completion:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: StopReason = "end_turn"
    usage: Usage = field(default_factory=Usage)
    # Present when the provider declined. Kept so the agent can say something
    # honest rather than replaying an empty answer.
    refusal_reason: str | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMError(RuntimeError):
    """A provider call failed.

    `retryable` separates a rate limit or a timeout — where waiting helps —
    from a malformed request, where it never will.
    """

    def __init__(self, provider: str, message: str, *, retryable: bool = False) -> None:
        self.provider = provider
        self.retryable = retryable
        super().__init__(f"[{provider}] {message}")


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolDefinition],
        max_tokens: int = 1024,
    ) -> Completion:
        """One turn. The caller owns the loop, the tools and the transcript."""
        ...


_REGISTRY: dict[str, type] = {}


def register(name: str, factory: type) -> type:
    _REGISTRY[name] = factory
    return factory


def get_provider(name: str, **kwargs: Any) -> LLMProvider:
    try:
        return _REGISTRY[name](**kwargs)
    except KeyError:
        raise LLMError(name, f"no provider registered; available: {sorted(_REGISTRY)}") from None


def registered_providers() -> list[str]:
    return sorted(_REGISTRY)
