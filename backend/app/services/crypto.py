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
