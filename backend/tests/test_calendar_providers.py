"""Adapter tests, driven by a mock transport rather than the network.

What is worth testing here is not that httpx can make a request. It is that we
send the right shapes, parse the right fields, ask for no more data than we
need, and treat each provider's idea of "this already exists" as success rather
than as an error.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from app.domain.intervals import Interval
from app.providers.calendar import CalendarError
from app.providers.calendar.base import CalendarCredentials
from app.providers.calendar.google import GoogleCalendarProvider
from app.providers.calendar.microsoft import MicrosoftGraphProvider
from app.providers.calendar.oauth import OAuthError, token_store

WINDOW = Interval(datetime(2026, 10, 5, 13, tzinfo=UTC), datetime(2026, 10, 5, 22, tzinfo=UTC))
APPOINTMENT_ID = "0191f4c2-1b2d-7a3e-9f10-abcdef123456"


def _creds(provider: str) -> CalendarCredentials:
    return CalendarCredentials(
        provider=provider,
        account_email="dr.hale@example.com",
        calendar_id="primary",
        refresh_token=f"refresh-{provider}-{uuid.uuid4()}",
    )


class Recorder:
    """Collects the requests an adapter makes, and replies from a script."""

    def __init__(self, routes: dict[str, tuple[int, dict]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.token_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        if "oauth2" in request.url.host or "login.microsoftonline.com" in request.url.host:
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": "at-123", "expires_in": 3600})

        for fragment, (status, body) in self.routes.items():
            if fragment in str(request.url):
                return httpx.Response(status, json=body)

        return httpx.Response(404, json={"error": f"unrouted: {request.url}"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def body(self, index: int) -> dict:
        return json.loads(self.requests[index].content)

    def to(self, fragment: str) -> list[httpx.Request]:
        return [r for r in self.requests if fragment in str(r.url)]


@pytest.fixture(autouse=True)
def _clear_token_cache():
    token_store._tokens.clear()
    token_store._locks.clear()
    yield
    token_store._tokens.clear()


# --- Google ----------------------------------------------------------------


async def test_google_parses_free_busy_blocks() -> None:
    rec = Recorder(
        {
            "/freeBusy": (
                200,
                {
                    "calendars": {
                        "primary": {
                            "busy": [
                                {"start": "2026-10-05T16:00:00Z", "end": "2026-10-05T17:00:00Z"},
                                {"start": "2026-10-05T19:30:00Z", "end": "2026-10-05T20:00:00Z"},
                            ]
                        }
                    }
                },
            )
        }
    )
    async with rec.client() as http:
        busy = await GoogleCalendarProvider(http).get_busy(_creds("google"), WINDOW)

    assert busy == [
        Interval(datetime(2026, 10, 5, 16, tzinfo=UTC), datetime(2026, 10, 5, 17, tzinfo=UTC)),
        Interval(datetime(2026, 10, 5, 19, 30, tzinfo=UTC), datetime(2026, 10, 5, 20, tzinfo=UTC)),
    ]
    sent = rec.body(1)
    assert sent["items"] == [{"id": "primary"}]
    assert sent["timeMin"].startswith("2026-10-05T13:00")


async def test_google_treats_a_per_calendar_error_as_a_failure_not_an_empty_day() -> None:
    """A calendar we cannot read is not a calendar with nothing in it."""
    rec = Recorder(
        {"/freeBusy": (200, {"calendars": {"primary": {"errors": [{"reason": "notFound"}]}}})}
    )
    async with rec.client() as http:
        with pytest.raises(CalendarError):
            await GoogleCalendarProvider(http).get_busy(_creds("google"), WINDOW)


async def test_google_sends_a_deterministic_event_id() -> None:
    rec = Recorder({"/events": (200, {"id": "clinic-x", "htmlLink": "https://cal/x"})})
    async with rec.client() as http:
        await GoogleCalendarProvider(http).create_event(
            _creds("google"),
            start=WINDOW.start,
            end=WINDOW.end,
            summary="Routine Cleaning — Alex",
            description="Booked by the assistant",
            idempotency_key=APPOINTMENT_ID,
        )

    body = rec.body(1)
    assert body["id"] == "clinic" + uuid.UUID(APPOINTMENT_ID).hex
    # base32hex: only a-v and 0-9 are legal in a Google event id.
    assert all(c in "abcdefghijklmnopqrstuv0123456789" for c in body["id"])
    assert body["attendees"] == []


async def test_google_treats_a_duplicate_id_as_success() -> None:
    """The first attempt wrote the event; its response was lost. Retrying is fine."""
    rec = Recorder({"/events": (409, {"error": {"message": "duplicate"}})})
    async with rec.client() as http:
        event = await GoogleCalendarProvider(http).create_event(
            _creds("google"),
            start=WINDOW.start,
            end=WINDOW.end,
            summary="s",
            description="d",
            idempotency_key=APPOINTMENT_ID,
        )
    assert event.event_id == "clinic" + uuid.UUID(APPOINTMENT_ID).hex


async def test_google_deleting_an_already_deleted_event_is_not_an_error() -> None:
    for status in (404, 410, 204):
        rec = Recorder({"/events/": (status, {})})
        async with rec.client() as http:
            await GoogleCalendarProvider(http).delete_event(_creds("google"), "clinicabc")


async def test_the_access_token_is_fetched_once_for_several_calls() -> None:
    """Every availability query would otherwise begin with a token refresh."""
    rec = Recorder({"/freeBusy": (200, {"calendars": {"primary": {"busy": []}}})})
    credentials = _creds("google")
    async with rec.client() as http:
        provider = GoogleCalendarProvider(http)
        await provider.get_busy(credentials, WINDOW)
        await provider.get_busy(credentials, WINDOW)
        await provider.get_busy(credentials, WINDOW)

    assert rec.token_calls == 1
    assert len(rec.to("/freeBusy")) == 3


# --- Microsoft -------------------------------------------------------------


async def test_microsoft_uses_get_schedule_when_the_account_supports_it() -> None:
    rec = Recorder(
        {
            "/getSchedule": (
                200,
                {
                    "value": [
                        {
                            "scheduleItems": [
                                {
                                    "status": "busy",
                                    "start": {"dateTime": "2026-10-05T16:00:00.0000000"},
                                    "end": {"dateTime": "2026-10-05T17:00:00.0000000"},
                                }
                            ]
                        }
                    ]
                },
            )
        }
    )
    async with rec.client() as http:
        busy = await MicrosoftGraphProvider(http).get_busy(_creds("microsoft"), WINDOW)

    assert busy == [
        Interval(datetime(2026, 10, 5, 16, tzinfo=UTC), datetime(2026, 10, 5, 17, tzinfo=UTC))
    ]
    assert rec.to("/calendarView") == []


async def test_microsoft_falls_back_to_calendar_view_for_personal_accounts() -> None:
    """getSchedule is a work/school feature; a personal account refuses it."""
    rec = Recorder(
        {
            "/getSchedule": (403, {"error": {"code": "ErrorAccessDenied"}}),
            "/calendarView": (
                200,
                {
                    "value": [
                        {
                            "showAs": "busy",
                            "isCancelled": False,
                            "start": {"dateTime": "2026-10-05T16:00:00.0000000"},
                            "end": {"dateTime": "2026-10-05T17:00:00.0000000"},
                        },
                        {
                            "showAs": "free",
                            "isCancelled": False,
                            "start": {"dateTime": "2026-10-05T18:00:00.0000000"},
                            "end": {"dateTime": "2026-10-05T19:00:00.0000000"},
                        },
                        {
                            "showAs": "busy",
                            "isCancelled": True,
                            "start": {"dateTime": "2026-10-05T20:00:00.0000000"},
                            "end": {"dateTime": "2026-10-05T21:00:00.0000000"},
                        },
                    ]
                },
            ),
        }
    )
    async with rec.client() as http:
        busy = await MicrosoftGraphProvider(http).get_busy(_creds("microsoft"), WINDOW)

    # Only the busy, uncancelled event counts.
    assert busy == [
        Interval(datetime(2026, 10, 5, 16, tzinfo=UTC), datetime(2026, 10, 5, 17, tzinfo=UTC))
    ]


async def test_the_calendar_view_fallback_never_asks_for_event_contents() -> None:
    """Titles and attendees are none of this system's business, and asking for
    them would put attacker-controlled text within reach of the model."""
    rec = Recorder(
        {
            "/getSchedule": (404, {}),
            "/calendarView": (200, {"value": []}),
        }
    )
    async with rec.client() as http:
        await MicrosoftGraphProvider(http).get_busy(_creds("microsoft"), WINDOW)

    select = rec.to("/calendarView")[0].url.params["$select"]
    assert select == "start,end,showAs,isCancelled"
    for forbidden in ("subject", "body", "attendees", "organizer"):
        assert forbidden not in select


async def test_microsoft_sends_a_transaction_id_for_idempotency() -> None:
    rec = Recorder({"/me/events": (201, {"id": "AAMk123", "webLink": "https://outlook/x"})})
    async with rec.client() as http:
        event = await MicrosoftGraphProvider(http).create_event(
            _creds("microsoft"),
            start=WINDOW.start,
            end=WINDOW.end,
            summary="Routine Cleaning — Alex",
            description="Booked by the assistant",
            idempotency_key=APPOINTMENT_ID,
        )

    body = rec.body(1)
    assert body["transactionId"] == APPOINTMENT_ID
    assert body["showAs"] == "busy"
    assert body["attendees"] == []
    assert event.event_id == "AAMk123"


async def test_microsoft_asks_for_times_in_utc() -> None:
    """Graph returns naive strings in whatever timezone it feels like otherwise."""
    rec = Recorder({"/getSchedule": (200, {"value": []})})
    async with rec.client() as http:
        await MicrosoftGraphProvider(http).get_busy(_creds("microsoft"), WINDOW)

    assert rec.to("/getSchedule")[0].headers["Prefer"] == 'outlook.timezone="UTC"'


# --- OAuth -----------------------------------------------------------------


async def test_a_revoked_token_is_reported_as_needing_a_reconnect() -> None:
    """Retrying a revoked grant never helps; the practitioner must authorise again."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OAuthError) as exc:
            await token_store.access_token(
                provider="google",
                refresh_token="revoked",
                token_url="https://oauth2.googleapis.com/token",
                client_id="id",
                client_secret="secret",
                client=http,
            )
    assert exc.value.reconnect_required is True


async def test_a_transient_token_failure_is_reported_as_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "backend_error"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OAuthError) as exc:
            await token_store.access_token(
                provider="google",
                refresh_token="fine",
                token_url="https://oauth2.googleapis.com/token",
                client_id="id",
                client_secret="secret",
                client=http,
            )
    assert exc.value.reconnect_required is False


async def test_a_failed_refresh_surfaces_as_a_calendar_error() -> None:
    """The booking service catches CalendarError; it must not see OAuthError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(CalendarError) as exc:
            await GoogleCalendarProvider(http).get_busy(_creds("google"), WINDOW)
    # Not retryable: reconnecting is the only remedy.
    assert exc.value.retryable is False
