"""A calendar that is always free.

Used for local development and for the automated tests, and as the fallback when
a practitioner has not connected any external calendar yet. Their bookings in our
own database still block their time; only external commitments are invisible.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.intervals import Interval
from app.providers.calendar.base import (
    CalendarCredentials,
    ExternalEvent,
    register,
)


class NullCalendarProvider:
    name = "null"

    async def get_busy(self, credentials: CalendarCredentials, window: Interval) -> list[Interval]:
        return []

    async def create_event(
        self,
        credentials: CalendarCredentials,
        *,
        start: datetime,
        end: datetime,
        summary: str,
        description: str,
        idempotency_key: str,
    ) -> ExternalEvent:
        return ExternalEvent(provider=self.name, event_id=f"null-{idempotency_key}")

    async def delete_event(self, credentials: CalendarCredentials, event_id: str) -> None:
        return None


register(NullCalendarProvider())
