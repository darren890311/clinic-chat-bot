"""Google Calendar.

Talks to the REST API over httpx rather than through google-api-python-client,
which is synchronous and would block the event loop on every availability query.
The three calls we make are simple enough that the SDK buys nothing, and writing
them out means the OAuth scopes and the request shapes are visible here rather
than buried in a client library's defaults.

Scope is `calendar.events` only: enough to read free/busy and manage the events
we create, and not enough to read anyone's event contents.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import httpx

from app.config import get_settings
from app.domain.intervals import Interval
from app.providers.calendar.base import (
    CalendarCredentials,
    CalendarError,
    ExternalEvent,
    register,
)
from app.providers.calendar.oauth import OAuthEndpoints, OAuthError, token_store

API = "https://www.googleapis.com/calendar/v3"

ENDPOINTS = OAuthEndpoints(
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    # Two scopes, both minimal, and the pair is deliberate.
    #
    # `calendar.events` manages our own appointments. It does not cover the
    # freeBusy endpoint — that returns 403 "insufficient authentication
    # scopes" — and the obvious fix, `calendar.readonly`, would hand us every
    # event's title, attendees and description for a practitioner's whole
    # diary.
    #
    # `calendar.freebusy` returns opaque busy intervals and nothing else. So
    # the guarantee the CalendarProvider port makes in code — that a booking
    # assistant has no way to learn who a dentist is meeting — is the same
    # guarantee Google enforces on the token, and a practitioner reading the
    # consent screen can see that for themselves.
    scopes=(
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
    ),
)


def authorization_url(*, state: str, redirect_uri: str) -> str:
    """Consent screen URL.

    `access_type=offline` with `prompt=consent` is what makes Google return a
    refresh token. Without both, the connection works for an hour and then
    stops, which is a confusing failure to debug weeks later.
    """
    settings = get_settings()
    params = httpx.QueryParams(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(ENDPOINTS.scopes),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return f"{ENDPOINTS.authorize_url}?{params}"


def _event_id(idempotency_key: str) -> str:
    """A deterministic event id, so a retry updates rather than duplicates.

    Google requires base32hex: characters a-v and 0-9, 5 to 1024 long. A UUID's
    hex digits are a subset of that, and the prefix keeps it obviously ours.
    """
    return "clinic" + uuid.UUID(idempotency_key).hex


class GoogleCalendarProvider:
    name = "google"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # Injectable so tests can drive it with a mock transport.
        self._client = client

    async def _request(
        self, credentials: CalendarCredentials, method: str, path: str, **kwargs
    ) -> httpx.Response:
        settings = get_settings()
        owned = self._client is None
        http = self._client or httpx.AsyncClient(timeout=20.0)
        try:
            token = await token_store.access_token(
                provider=self.name,
                refresh_token=credentials.refresh_token,
                token_url=ENDPOINTS.token_url,
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret,
                client=http,
            )
            return await http.request(
                method,
                f"{API}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
        except OAuthError as exc:
            raise CalendarError(self.name, str(exc), retryable=not exc.reconnect_required) from exc
        except httpx.HTTPError as exc:
            raise CalendarError(self.name, f"request failed: {exc}") from exc
        finally:
            if owned:
                await http.aclose()

    async def get_busy(self, credentials: CalendarCredentials, window: Interval) -> list[Interval]:
        """Opaque busy blocks. The response carries no titles or attendees."""
        response = await self._request(
            credentials,
            "POST",
            "/freeBusy",
            json={
                "timeMin": window.start.isoformat(),
                "timeMax": window.end.isoformat(),
                "items": [{"id": credentials.calendar_id}],
            },
        )
        if response.status_code >= 400:
            raise CalendarError(
                self.name, f"freeBusy failed ({response.status_code}): {response.text[:200]}"
            )

        calendar = response.json().get("calendars", {}).get(credentials.calendar_id, {})
        if calendar.get("errors"):
            # A calendar we cannot read is not a calendar with nothing in it.
            raise CalendarError(self.name, f"freeBusy error: {calendar['errors']}")

        return [
            Interval(
                datetime.fromisoformat(block["start"].replace("Z", "+00:00")),
                datetime.fromisoformat(block["end"].replace("Z", "+00:00")),
            )
            for block in calendar.get("busy", [])
        ]

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
        event_id = _event_id(idempotency_key)
        response = await self._request(
            credentials,
            "POST",
            f"/calendars/{credentials.calendar_id}/events",
            json={
                "id": event_id,
                "summary": summary,
                "description": description,
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()},
                # The patient is not a Google Calendar user and should not be
                # invited; this is the practitioner's copy of the booking.
                "attendees": [],
                "reminders": {"useDefault": True},
            },
        )

        # A client-supplied id already in use means we wrote this event on a
        # previous attempt whose response we never saw. That is success.
        if response.status_code == 409:
            return ExternalEvent(provider=self.name, event_id=event_id)

        if response.status_code >= 400:
            raise CalendarError(
                self.name, f"event creation failed ({response.status_code}): {response.text[:200]}"
            )

        payload = response.json()
        return ExternalEvent(
            provider=self.name,
            event_id=payload.get("id", event_id),
            html_link=payload.get("htmlLink"),
        )

    async def delete_event(self, credentials: CalendarCredentials, event_id: str) -> None:
        response = await self._request(
            credentials, "DELETE", f"/calendars/{credentials.calendar_id}/events/{event_id}"
        )
        # 404 and 410 mean it is already gone, which is what we wanted.
        if response.status_code not in (200, 204, 404, 410):
            raise CalendarError(
                self.name, f"event deletion failed ({response.status_code}): {response.text[:200]}"
            )


register(GoogleCalendarProvider())
