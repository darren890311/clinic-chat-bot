"""Claude, through the official Anthropic SDK.

Unlike the calendar adapters, which speak REST directly, this one uses the
vendor SDK. The Messages API surface is large — tool blocks, thinking blocks,
streaming, refusal handling — and hand-rolling it would be reimplementing a
maintained client badly. The three calendar endpoints did not justify a
dependency; this does.

Everything provider-specific lives here and nowhere else: adaptive thinking,
the effort setting, and the server-side refusal fallback. The agent above sees
only `Completion`.
"""

from __future__ import annotations

import json
from typing import Any

from app.config import get_settings
from app.providers.llm.base import (
    Completion,
    LLMError,
    Message,
    ToolCall,
    ToolDefinition,
    Usage,
    register,
)

# Requests that decline for safety reasons are routed by category to a model
# that can answer, rather than surfacing an empty turn to a patient.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

STOP_REASONS = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "stop_sequence": "end_turn",
    "pause_turn": "end_turn",
}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, model: str | None = None, client: Any = None) -> None:
        settings = get_settings()
        self.model = model or settings.llm_model
        self._client = client
        self._api_key = settings.anthropic_api_key

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LLMError(self.name, "the anthropic package is not installed") from exc
        if not self._api_key:
            raise LLMError(self.name, "ANTHROPIC_API_KEY is not set")
        self._client = AsyncAnthropic(api_key=self._api_key, timeout=30.0)
        return self._client

    def _tools(self, tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        # `strict` makes the API guarantee the arguments validate against the
        # schema, which removes a whole class of defensive parsing from the
        # tool implementations.
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "strict": True,
                "input_schema": tool.strict_schema(),
            }
            for tool in tools
        ]

    def _messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Normalised transcript -> Anthropic's content-block shape.

        Two things differ from the neutral form. Tool calls are blocks inside an
        assistant message rather than a field beside it, and tool results are
        blocks inside a *user* message rather than a role of their own.

        Consecutive tool results are merged into a single user message on
        purpose: splitting them teaches the model to stop requesting tools in
        parallel, which costs a round trip on every subsequent turn.
        """
        out: list[dict[str, Any]] = []

        for message in messages:
            if message.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }
                if message.is_error:
                    block["is_error"] = True
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
                continue

            if message.role == "assistant" and message.tool_calls:
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                    for call in message.tool_calls
                )
                out.append({"role": "assistant", "content": blocks})
                continue

            out.append({"role": message.role, "content": message.content})

        return out

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolDefinition],
        max_tokens: int = 1024,
    ) -> Completion:
        client = self._ensure_client()

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": self._messages(messages),
            # Booking is a short, latency-sensitive exchange with a patient
            # waiting. Low effort is the right trade here; the hard reasoning
            # lives in the scheduling engine, not in the model.
            "output_config": {"effort": "low"},
        }
        if tools:
            request["tools"] = self._tools(tools)

        if self.model.startswith("claude-opus-5") or self.model.startswith("claude-fable"):
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"

        try:
            if "betas" in request:
                response = await client.beta.messages.create(**request)
            else:
                response = await client.messages.create(**request)
        except Exception as exc:  # noqa: BLE001 - normalised below
            raise self._as_llm_error(exc) from exc

        return self._to_completion(response)

    def _to_completion(self, response: Any) -> Completion:
        text_parts: list[str] = []
        calls: list[ToolCall] = []

        for block in response.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                text_parts.append(block.text)
            elif kind == "tool_use":
                # The SDK may escape strings differently between models, so the
                # input is always taken as parsed data, never string-matched.
                arguments = (
                    block.input if isinstance(block.input, dict) else json.loads(block.input)
                )
                calls.append(ToolCall(id=block.id, name=block.name, arguments=arguments))

        stop = STOP_REASONS.get(response.stop_reason or "end_turn", "end_turn")

        refusal = None
        if stop == "refusal":
            # Populated only on a refusal; guard before reading.
            details = getattr(response, "stop_details", None)
            refusal = getattr(details, "explanation", None) or "the request was declined"

        usage = getattr(response, "usage", None)
        return Completion(
            text="".join(text_parts).strip(),
            tool_calls=tuple(calls),
            stop_reason=stop,
            usage=Usage(
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
            ),
            refusal_reason=refusal,
        )

    def _as_llm_error(self, exc: Exception) -> LLMError:
        """Separate what waiting fixes from what it does not."""
        status = getattr(exc, "status_code", None)
        retryable = status in (408, 409, 429) or (status is not None and status >= 500)
        if status is None and "connection" in str(exc).lower():
            retryable = True
        return LLMError(self.name, str(exc)[:300], retryable=retryable)


register("anthropic", AnthropicProvider)
