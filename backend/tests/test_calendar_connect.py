"""Tests for the calendar connection flow.

The interesting cases are the ones an attacker would try: reaching the flow
without the admin token, forging a state, replaying an old one, and pointing a
callback at the wrong provider.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

KEY = Fernet.generate_key().decode()
ADMIN = "test-admin-token"


@pytest.fixture(scope="module")
def client():
    os.environ["TOKEN_ENCRYPTION_KEY"] = KEY
    os.environ["ADMIN_TOKEN"] = ADMIN
    os.environ["GOOGLE_CLIENT_ID"] = "google-client-id"
    os.environ["GOOGLE_CLIENT_SECRET"] = "google-client-secret"

    from app.config import get_settings

    get_settings.cache_clear()
    import app.services.crypto as crypto

    crypto._cipher.cache_clear()

    import app.api.calendar_routes as cal

    cal.settings = get_settings()
    from app.main import app

    with TestClient(app) as c:
        yield c


def _state(**overrides) -> str:
    from app.services.crypto import sign_state

    payload = {
        "clinic_id": "11111111-1111-1111-1111-111111111111",
        "practitioner": "dr-hale",
        "provider": "google",
        "nonce": "n",
    }
    payload.update(overrides)
    return sign_state(payload)


# --- the guard -------------------------------------------------------------


def test_starting_a_connection_without_the_admin_token_is_refused(client) -> None:
    response = client.get(
        "/api/calendar/google/connect", params={"practitioner": "dr-hale"}, follow_redirects=False
    )
    assert response.status_code == 401


def test_a_wrong_admin_token_is_refused(client) -> None:
    response = client.get(
        "/api/calendar/google/connect",
        params={"practitioner": "dr-hale", "token": "not-the-token"},
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_disconnecting_also_requires_the_admin_token(client) -> None:
    assert client.delete("/api/calendar/google/dr-hale").status_code == 401


def test_an_unknown_provider_is_rejected(client) -> None:
    response = client.get(
        "/api/calendar/dropbox/connect",
        params={"practitioner": "dr-hale", "token": ADMIN},
        follow_redirects=False,
    )
    assert response.status_code == 404


# --- the state parameter ---------------------------------------------------


def test_a_forged_state_is_rejected(client) -> None:
    """Signed with a different key, which is what an attacker would have."""
    forged = (
        Fernet(Fernet.generate_key())
        .encrypt(
            json.dumps({"clinic_id": "x", "practitioner": "dr-hale", "provider": "google"}).encode()
        )
        .decode()
    )
    response = client.get("/api/calendar/google/callback", params={"code": "abc", "state": forged})
    assert response.status_code == 400
    assert "invalid or has expired" in response.text


def test_a_stale_state_is_rejected(client) -> None:
    """A consent link captured yesterday must not still work."""
    old = (
        Fernet(KEY.encode())
        .encrypt_at_time(
            json.dumps(
                {
                    "clinic_id": "11111111-1111-1111-1111-111111111111",
                    "practitioner": "dr-hale",
                    "provider": "google",
                }
            ).encode(),
            int(time.time()) - 3600,
        )
        .decode()
    )
    response = client.get("/api/calendar/google/callback", params={"code": "abc", "state": old})
    assert response.status_code == 400
    # Assert the reason, not just the status. Without the expiry check the state
    # decrypts, the flow proceeds to the token exchange, and that fails for its
    # own reasons — also with a 400. The message is what distinguishes them.
    assert "invalid or has expired" in response.text


def test_a_state_issued_for_another_provider_is_rejected(client) -> None:
    """Otherwise a Google consent could be redeemed against the Outlook adapter."""
    response = client.get(
        "/api/calendar/microsoft/callback",
        params={"code": "abc", "state": _state(provider="google")},
    )
    assert response.status_code == 400
    assert "different provider" in response.text


def test_a_callback_without_a_code_is_rejected(client) -> None:
    response = client.get("/api/calendar/google/callback", params={"state": _state()})
    assert response.status_code == 400


def test_a_provider_error_is_shown_rather_than_swallowed(client) -> None:
    response = client.get("/api/calendar/google/callback", params={"error": "access_denied"})
    assert response.status_code == 400
    assert "access_denied" in response.text


# --- the consent redirect --------------------------------------------------


def test_the_consent_url_asks_for_offline_access_and_the_narrow_scope(client) -> None:
    """Without offline access Google issues no refresh token and the connection
    silently stops working after an hour."""
    from app.providers.calendar.google import authorization_url

    url = authorization_url(state="s", redirect_uri="https://example.test/cb")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "calendar.events" in url
    # calendar.readonly would also expose every event's title and attendees.
    assert "calendar.readonly" not in url


def test_the_microsoft_consent_url_asks_for_offline_access(client) -> None:
    from app.providers.calendar.microsoft import authorization_url

    url = authorization_url(state="s", redirect_uri="https://example.test/cb")
    assert "offline_access" in url
    assert "Calendars.ReadWrite" in url
