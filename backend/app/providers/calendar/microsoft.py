"""Outlook, through Microsoft Graph.

Two things differ from Google in ways that matter.

**Reading availability.** Graph's `getSchedule` is the direct equivalent of
Google's `freeBusy`, but it is documented for work and school accounts and is
not reliably available on personal outlook.com accounts. Rather than pick one
and hope, this adapter tries `getSchedule` and falls back to reading a
`calendarView` and deriving busy intervals from it. The fallback asks only for
start, end and `showAs`, so it still never sees event titles or attendees.

**Idempotency.** Graph does not accept a client-chosen event id. It does accept
`transactionId`, which it uses to deduplicate for a few days — enough to cover a
retry whose first response was lost.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from app.config import get_settings
from app.domain.intervals import Interval, merge
from app.providers.calendar.base import (
    CalendarCredentials,
    CalendarError,
    ExternalEvent,
    register,
)
from app.providers.calendar.oauth import OAuthEndpoints, OAuthError, token_store

API = "https://graph.microsoft.com/v1.0"

# offline_access is what makes Graph return a refresh token at all.
SCOPES = (
    "offline_access",
    "https://graph.microsoft.com/Calendars.ReadWrite",
)

# `showAs` values that mean the practitioner is not available.
BUSY_STATES = {"busy", "oof", "workingElsewhere"}


def _endpoints() -> OAuthEndpoints:
    tenant = get_settings().microsoft_tenant_id or "common"
    return OAuthEndpoints(
        authorize_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        token_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        scopes=SCOPES,
    )


def authorization_url(*, state: str, redirect_uri: str) -> str:
    settings = get_settings()
    params = httpx.QueryParams(
        {
            "client_id": settings.microsoft_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "response_mode": "query",
            "scope": " ".join(SCOPES),
            "state": state,
        }
    )
    return f"{_endpoints().authorize_url}?{params}"


def _parse(value: str) -> datetime:
    """Graph returns naive local-to-the-timezone strings; we always ask for UTC."""
    cleaned = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        from datetime import UTC

        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class MicrosoftGraphProvider:
    name = "microsoft"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _request(
        self, credentials: CalendarCredentials, method: str, path: str, **kwargs
    ) -> httpx.Response:
        settings = get_settings()
        endpoints = _endpoints()
        owned = self._client is None
        http = self._client or httpx.AsyncClient(timeout=20.0)
        try:
            token = await token_store.access_token(
                provider=self.name,
                refresh_token=credentials.refresh_token,
                token_url=endpoints.token_url,
                client_id=settings.microsoft_client_id,
                client_secret=settings.microsoft_client_secret,
                scopes=SCOPES,
                client=http,
            )
            headers = {
                "Authorization": f"Bearer {token}",
                # Ask for UTC so we never have to guess what a naive time means.
                "Prefer": 'outlook.timezone="UTC"',
            }
            headers.update(kwargs.pop("headers", {}))
            return await http.request(method, f"{API}{path}", headers=headers, **kwargs)
        except OAuthError as exc:
            raise CalendarError(self.name, str(exc), retryable=not exc.reconnect_required) from exc
        except httpx.HTTPError as exc:
            raise CalendarError(self.name, f"request failed: {exc}") from exc
        finally:
            if owned:
                await http.aclose()

    async def get_busy(self, credentials: CalendarCredentials, window: Interval) -> list[Interval]:
        busy = await self._get_schedule(credentials, window)
        if busy is not None:
            return busy
        return await self._calendar_view(credentials, window)

    async def _get_schedule(
        self, credentials: CalendarCredentials, window: Interval
    ) -> list[Interval] | None:
        """The direct free/busy call. Returns None if this account cannot use it."""
        response = await self._request(
            credentials,
            "POST",
            "/me/calendar/getSchedule",
            json={
                "schedules": [credentials.account_email],
                "startTime": {"dateTime": window.start.isoformat(), "timeZone": "UTC"},
                "endTime": {"dateTime": window.end.isoformat(), "timeZone": "UTC"},
                "availabilityViewInterval": 15,
            },
        )

        # Personal accounts answer with a 4xx here rather than an empty result.
        # Falling back is correct; failing would make the adapter unusable for
        # exactly the accounts a small clinic is most likely to have.
        if response.status_code in (400, 403, 404, 501):
            return None
        if response.status_code >= 400:
            raise CalendarError(
                self.name, f"getSchedule failed ({response.status_code}): {response.text[:200]}"
            )

        schedules = response.json().get("value", [])
        if not schedules:
            return []

        intervals: list[Interval] = []
        for item in schedules[0].get("scheduleItems", []):
            if item.get("status") and item["status"] not in BUSY_STATES:
                continue
            intervals.append(
                Interval(_parse(item["start"]["dateTime"]), _parse(item["end"]["dateTime"]))
            )
        return merge(intervals)

    async def _calendar_view(
        self, credentials: CalendarCredentials, window: Interval
    ) -> list[Interval]:
        """Fallback: derive busy from the event list, asking for as little as possible."""
        params = {
            "startDateTime": window.start.isoformat(),
            "endDateTime": window.end.isoformat(),
            # Only what is needed to know the practitioner is occupied. No
            # subject, no body, no attendees.
            "$select": "start,end,showAs,isCancelled",
            "$top": "200",
        }
        response = await self._request(credentials, "GET", "/me/calendarView", params=params)
        if response.status_code >= 400:
            raise CalendarError(
                self.name, f"calendarView failed ({response.status_code}): {response.text[:200]}"
            )

        intervals: list[Interval] = []
        for event in response.json().get("value", []):
            if event.get("isCancelled"):
                continue
            if event.get("showAs", "busy") not in BUSY_STATES:
                continue
            intervals.append(
                Interval(_parse(event["start"]["dateTime"]), _parse(event["end"]["dateTime"]))
            )
        return merge(intervals)

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
        response = await self._request(
            credentials,
            "POST",
            "/me/events",
            json={
                # Graph deduplicates on this for several days, which covers a
                # retry whose first response was lost.
                "transactionId": idempotency_key,
                "subject": summary,
                "body": {"contentType": "text", "content": description},
                "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
                "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
                "attendees": [],
                "showAs": "busy",
            },
        )
        if response.status_code >= 400:
            raise CalendarError(
                self.name, f"event creation failed ({response.status_code}): {response.text[:200]}"
            )

        payload = response.json()
        return ExternalEvent(
            provider=self.name,
            event_id=payload["id"],
            html_link=payload.get("webLink"),
        )

    async def delete_event(self, credentials: CalendarCredentials, event_id: str) -> None:
        response = await self._request(credentials, "DELETE", f"/me/events/{event_id}")
        if response.status_code not in (200, 204, 404, 410):
            raise CalendarError(
                self.name, f"event deletion failed ({response.status_code}): {response.text[:200]}"
            )


register(MicrosoftGraphProvider())
