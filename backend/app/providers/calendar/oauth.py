"""OAuth plumbing shared by the calendar providers.

Google and Microsoft both issue long-lived refresh tokens and short-lived access
tokens through the same `refresh_token` grant, so the exchange is written once
here rather than twice in the adapters. Hand-rolling it over httpx keeps the
whole path async and means the OAuth flow is code we can explain rather than a
vendor SDK's behaviour we have to trust.

Access tokens are cached in memory for their stated lifetime. Without that,
every availability query on a call would start with a token refresh, and a
patient waiting to hear their options would pay for it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

# Refresh slightly early: a token that expires while a request is in flight is
# indistinguishable from a revoked one, and the retry looks like a bug.
EXPIRY_MARGIN_SECONDS = 60


class OAuthError(RuntimeError):
    """Token exchange failed.

    Separate from CalendarError because the remedy differs: a failed refresh
    usually means the practitioner must reconnect, not that the call should be
    retried.
    """

    def __init__(self, provider: str, message: str, *, reconnect_required: bool = False) -> None:
        self.provider = provider
        self.reconnect_required = reconnect_required
        super().__init__(f"[{provider}] {message}")


@dataclass(frozen=True)
class OAuthEndpoints:
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]


@dataclass
class _CachedToken:
    access_token: str
    expires_at: float


class TokenStore:
    """In-process cache of access tokens, keyed by refresh token.

    Deliberately not shared between instances: Cloud Run may run several, and a
    per-instance cache is correct without any coordination. The worst case is a
    few extra refreshes after a scale-up.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, _CachedToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def access_token(
        self,
        *,
        provider: str,
        refresh_token: str,
        token_url: str,
        client_id: str,
        client_secret: str,
        scopes: tuple[str, ...] = (),
        client: httpx.AsyncClient | None = None,
    ) -> str:
        key = f"{provider}:{refresh_token}"

        cached = self._tokens.get(key)
        if cached and cached.expires_at > time.monotonic():
            return cached.access_token

        # One refresh per token even if several requests notice it expired at
        # the same moment.
        async with self._lock(key):
            cached = self._tokens.get(key)
            if cached and cached.expires_at > time.monotonic():
                return cached.access_token

            token = await self._exchange(
                provider=provider,
                refresh_token=refresh_token,
                token_url=token_url,
                client_id=client_id,
                client_secret=client_secret,
                scopes=scopes,
                client=client,
            )
            self._tokens[key] = token
            return token.access_token

    async def _exchange(
        self,
        *,
        provider: str,
        refresh_token: str,
        token_url: str,
        client_id: str,
        client_secret: str,
        scopes: tuple[str, ...],
        client: httpx.AsyncClient | None,
    ) -> _CachedToken:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scopes:
            # Microsoft requires the scope on refresh; Google ignores it.
            form["scope"] = " ".join(scopes)

        owned = client is None
        http = client or httpx.AsyncClient(timeout=15.0)
        try:
            response = await http.post(token_url, data=form)
        except httpx.HTTPError as exc:
            raise OAuthError(provider, f"token endpoint unreachable: {exc}") from exc
        finally:
            if owned:
                await http.aclose()

        if response.status_code >= 400:
            body = response.text[:300]
            # invalid_grant means the token was revoked or expired. Retrying
            # will never help; the practitioner has to authorise again.
            raise OAuthError(
                provider,
                f"refresh failed ({response.status_code}): {body}",
                reconnect_required="invalid_grant" in body,
            )

        payload = response.json()
        if "access_token" not in payload:
            raise OAuthError(provider, "token response contained no access_token")

        lifetime = int(payload.get("expires_in", 3600))
        return _CachedToken(
            access_token=payload["access_token"],
            expires_at=time.monotonic() + max(lifetime - EXPIRY_MARGIN_SECONDS, 30),
        )

    def forget(self, provider: str, refresh_token: str) -> None:
        self._tokens.pop(f"{provider}:{refresh_token}", None)


# Module-level so adapters share one cache per process.
token_store = TokenStore()


async def exchange_authorization_code(
    *,
    provider: str,
    code: str,
    token_url: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Swap the one-time code from the consent screen for tokens.

    Returns the raw payload because the caller needs the refresh token, and the
    two providers spell the surrounding fields differently.
    """
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }
    owned = client is None
    http = client or httpx.AsyncClient(timeout=15.0)
    try:
        response = await http.post(token_url, data=form)
    except httpx.HTTPError as exc:
        raise OAuthError(provider, f"token endpoint unreachable: {exc}") from exc
    finally:
        if owned:
            await http.aclose()

    if response.status_code >= 400:
        raise OAuthError(
            provider, f"code exchange failed ({response.status_code}): {response.text[:300]}"
        )

    payload = response.json()
    if not payload.get("refresh_token"):
        # Google only issues one on the first consent, and only when asked with
        # access_type=offline and prompt=consent. Without it the connection
        # would work until the access token expired and then quietly stop.
        raise OAuthError(
            provider,
            "authorisation returned no refresh token; the connection would expire within the hour",
        )
    return payload
