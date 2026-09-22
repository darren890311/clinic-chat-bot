"""The calendar port.

Adding a third calendar system (Apple, CalDAV, a practice-management suite)
means writing one class that satisfies this protocol and registering it. Nothing
in the scheduling engine, the agent, or the API changes.

Deliberately narrow. We read free/busy and we write events; we never read event
titles, attendees or descriptions. That keeps other people's meeting contents out
of this system entirely, which is both a privacy property and a prompt-injection
defence: there is no untrusted calendar text for a model to be influenced by.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain.intervals import Interval


@dataclass(frozen=True)
class CalendarCredentials:
    provider: str
    account_email: str
    calendar_id: str
    refresh_token: str


@dataclass(frozen=True)
class ExternalEvent:
    provider: str
    event_id: str
    html_link: str | None = None


class CalendarError(RuntimeError):
    """Provider call failed. Callers decide whether to degrade or abort."""

    def __init__(self, provider: str, message: str, *, retryable: bool = True) -> None:
        self.provider = provider
        self.retryable = retryable
        super().__init__(f"[{provider}] {message}")


@runtime_checkable
class CalendarProvider(Protocol):
    name: str

    async def get_busy(self, credentials: CalendarCredentials, window: Interval) -> list[Interval]:
        """Opaque busy blocks. Must not return event titles or attendees."""
        ...

    async def create_event(
        self,
        credentials: CalendarCredentials,
        *,
        start: datetime,
        end: datetime,
        summary: str,
        description: str,
        idempotency_key: str,
    ) -> ExternalEvent: ...

    async def delete_event(self, credentials: CalendarCredentials, event_id: str) -> None: ...


_REGISTRY: dict[str, CalendarProvider] = {}


def register(provider: CalendarProvider) -> CalendarProvider:
    _REGISTRY[provider.name] = provider
    return provider


def get_provider(name: str) -> CalendarProvider:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise CalendarError(
            name, f"no provider registered; available: {sorted(_REGISTRY)}", retryable=False
        ) from None


def registered_providers() -> list[str]:
    return sorted(_REGISTRY)
