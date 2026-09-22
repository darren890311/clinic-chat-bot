"""GPT, through the official OpenAI SDK.

The second adapter exists to prove the first one is not load-bearing. Same
tools, same system prompt, same conversation, same guardrails — only the
translation differs, and the differences are instructive:

* Tool calls are a field on the assistant message rather than blocks inside it.
* Tool results are their own role, not user-message content.
* Arguments arrive as a JSON *string* that the adapter must parse.
* The system prompt is the first message rather than a separate parameter.

None of that reaches the agent.
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

STOP_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
    "function_call": "tool_use",
}


class OpenAIProvider:
    name = "openai"

    def __init__(self, *, model: str | None = None, client: Any = None) -> None:
        settings = get_settings()
        self.model = model or settings.openai_model
        self._client = client
        self._api_key = settings.openai_api_key

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LLMError(self.name, "the openai package is not installed") from exc
        if not self._api_key:
            raise LLMError(self.name, "OPENAI_API_KEY is not set")
        self._client = AsyncOpenAI(api_key=self._api_key, timeout=30.0)
        return self._client

    def _tools(self, tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "strict": True,
                    "parameters": tool.strict_schema(),
                },
            }
            for tool in tools
        ]

    def _messages(
        self, system: str, messages: list[Message], context: str | None = None
    ) -> list[dict[str, Any]]:
        # OpenAI caches automatically on the longest matching token prefix, so
        # there is no marker to place — only an order to respect. The stable
        # instructions come first and the volatile context is appended, leaving
        # everything before it a common prefix across turns.
        content = f"{system}\n\n{context}" if context else system
        out: list[dict[str, Any]] = [{"role": "system", "content": content}]

        for message in messages:
            if message.role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        # There is no is_error flag here, so a failure has to be
                        # legible in the text itself.
                        "content": (
                            f"ERROR: {message.content}" if message.is_error else message.content
                        ),
                    }
                )
                continue

            if message.role == "assistant" and message.tool_calls:
                out.append(
                    {
                        "role": "assistant",
                        "content": message.content or None,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments),
                                },
                            }
                            for call in message.tool_calls
                        ],
                    }
                )
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
        context: str | None = None,
    ) -> Completion:
        client = self._ensure_client()

        request: dict[str, Any] = {
            "model": self.model,
            "max_completion_tokens": max_tokens,
            "messages": self._messages(system, messages, context),
        }
        if tools:
            request["tools"] = self._tools(tools)
            request["tool_choice"] = "auto"

        try:
            response = await client.chat.completions.create(**request)
        except Exception as exc:  # noqa: BLE001 - normalised below
            raise self._as_llm_error(exc) from exc

        return self._to_completion(response)

    def _to_completion(self, response: Any) -> Completion:
        choice = response.choices[0]
        message = choice.message

        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise LLMError(self.name, f"tool arguments were not valid JSON: {exc}") from exc
            calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))

        stop = STOP_REASONS.get(choice.finish_reason or "stop", "end_turn")
        refusal = getattr(message, "refusal", None)
        if refusal:
            stop = "refusal"

        usage = getattr(response, "usage", None)
        return Completion(
            text=(message.content or "").strip(),
            tool_calls=tuple(calls),
            stop_reason=stop,
            usage=Usage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                cache_read_tokens=getattr(
                    getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0
                )
                or 0,
            ),
            refusal_reason=refusal,
        )

    def _as_llm_error(self, exc: Exception) -> LLMError:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        retryable = status in (408, 409, 429) or (status is not None and status >= 500)
        if status is None and "connection" in str(exc).lower():
            retryable = True
        return LLMError(self.name, str(exc)[:300], retryable=retryable)


register("openai", OpenAIProvider)
