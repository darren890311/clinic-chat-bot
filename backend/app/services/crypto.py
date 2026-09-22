"""Encryption for OAuth refresh tokens at rest.

A refresh token grants ongoing access to a practitioner's calendar, so it is
encrypted with a key held outside the database. A database dump, a replica, or
a backup restored somewhere it should not be does not by itself grant calendar
access; the key lives in Secret Manager and is injected into the running
container only.

Fernet is AES-128-CBC with an HMAC, key rotation supported via MultiFernet. It
is chosen over hand-rolled AES because the authenticated construction and the
IV handling are the parts that go wrong.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class TokenEncryptionUnavailable(RuntimeError):
    """No key configured. Connecting a calendar must fail rather than store plaintext."""


@lru_cache
def _cipher() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        raise TokenEncryptionUnavailable(
            "TOKEN_ENCRYPTION_KEY is not set; refusing to store OAuth tokens unencrypted"
        )
    return Fernet(key.encode())


def encrypt(plaintext: str) -> bytes:
    return _cipher().encrypt(plaintext.encode())


def decrypt(ciphertext: bytes) -> str:
    try:
        return _cipher().decrypt(ciphertext).decode()
    except InvalidToken as exc:
        # Usually means the key was rotated without re-encrypting. Surfacing it
        # as a distinct error lets the UI prompt for a reconnect rather than
        # failing the booking with something unreadable.
        raise TokenEncryptionUnavailable(
            "stored token could not be decrypted with the current key"
        ) from exc


def sign_state(payload: dict) -> str:
    """Seal an OAuth `state` value.

    The state parameter is what ties a consent redirect back to the request that
    started it. Unsigned, an attacker can forge one and have a practitioner's
    authorisation attached to a practitioner of the attacker's choosing.

    Fernet gives authentication and an expiry in one step, so a state cannot be
    tampered with and cannot be replayed days later.
    """
    import json

    # Decoded to str: this value travels in a URL query parameter.
    return encrypt(json.dumps(payload, separators=(",", ":"))).decode()


def verify_state(state: str, *, max_age_seconds: int = 600) -> dict:
    """Open a sealed state, rejecting anything forged or stale."""
    import json

    from cryptography.fernet import InvalidToken

    try:
        raw = _cipher().decrypt(state.encode(), ttl=max_age_seconds)
    except InvalidToken as exc:
        raise TokenEncryptionUnavailable("oauth state was invalid or expired") from exc
    return json.loads(raw.decode())
