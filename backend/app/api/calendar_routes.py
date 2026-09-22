"""Connecting a practitioner's calendar.

Three endpoints and one redirect, guarded by a shared admin token. There is no
staff login in this deployment, and an unauthenticated endpoint that begins an
OAuth flow is an invitation: anyone who found the URL could attach a calendar of
their choosing to a practitioner of their choosing.

The `state` parameter is sealed with the same key that protects stored tokens,
which gives it authentication and a ten-minute expiry in one step. An unsigned
state is the classic way this flow is abused — a forged one lets an attacker
have a practitioner's genuine authorisation bound to the wrong record.
"""

from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.config import get_settings
from app.db import models
from app.db import repository as repo
from app.db.session import tenant_session
from app.providers.calendar import google, microsoft
from app.providers.calendar.oauth import OAuthError, exchange_authorization_code
from app.services.crypto import TokenEncryptionUnavailable, encrypt, sign_state, verify_state

router = APIRouter(prefix="/api/calendar")
settings = get_settings()

PROVIDERS = {
    "google": {
        "module": google,
        "token_url": google.ENDPOINTS.token_url,
        "client_id": lambda s: s.google_client_id,
        "client_secret": lambda s: s.google_client_secret,
        "scopes": google.ENDPOINTS.scopes,
    },
    "microsoft": {
        "module": microsoft,
        "token_url": None,  # tenant-dependent, resolved per request
        "client_id": lambda s: s.microsoft_client_id,
        "client_secret": lambda s: s.microsoft_client_secret,
        "scopes": microsoft.SCOPES,
    },
}


def require_admin(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    token: str | None = Query(default=None, description="alternative to the header, for browsers"),
) -> None:
    """Guard the connection flow.

    A browser following a link cannot set a header, so the token is also
    accepted as a query parameter. That is weaker — query strings end up in
    logs and history — and is the reason this is a stopgap until staff login
    exists rather than the final design.
    """
    expected = settings.admin_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_TOKEN is not configured; calendar connection is disabled.",
        )
    supplied = x_admin_token or token or ""
    # Constant-time: a length-leaking comparison is a free hint to an attacker.
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Not authorised.")


def _redirect_uri(provider: str) -> str:
    return f"{settings.oauth_redirect_base.rstrip('/')}/api/calendar/{provider}/callback"


def _provider_or_404(provider: str) -> dict:
    if provider not in PROVIDERS:
        raise HTTPException(
            status_code=404, detail=f"Unknown provider {provider!r}. Try google or microsoft."
        )
    return PROVIDERS[provider]


async def current_clinic_id(
    clinic: str | None = Query(default=None),
) -> uuid.UUID:
    from app.db.session import unscoped_session

    slug = clinic or settings.default_clinic_slug
    async with unscoped_session() as session:
        clinic_id = await repo.resolve_clinic_id(session, slug)
    if clinic_id is None:
        raise HTTPException(status_code=404, detail=f"Unknown clinic {slug!r}")
    return clinic_id


class ConnectionOut(BaseModel):
    practitioner_slug: str
    practitioner_name: str
    provider: str
    account_email: str
    connected: bool
    needs_reconnect: bool


@router.get("/status", response_model=list[ConnectionOut])
async def connection_status(
    clinic_id: uuid.UUID = Depends(current_clinic_id),
) -> list[ConnectionOut]:
    """Which practitioners have which calendars connected.

    Readable without the admin token: it exposes no credentials, and the clinic
    needs to see at a glance whose diary the bot can actually check.
    """
    from sqlalchemy import select

    async with tenant_session(clinic_id) as session:
        practitioners = list((await session.execute(select(models.Practitioner))).scalars())
        accounts = list((await session.execute(select(models.CalendarAccount))).scalars())

    by_practitioner = {p.id: p for p in practitioners}
    return [
        ConnectionOut(
            practitioner_slug=by_practitioner[a.practitioner_id].slug,
            practitioner_name=by_practitioner[a.practitioner_id].name,
            provider=a.provider,
            account_email=a.account_email,
            connected=a.invalidated_at is None,
            needs_reconnect=a.invalidated_at is not None,
        )
        for a in accounts
        if a.practitioner_id in by_practitioner
    ]


@router.get("/{provider}/connect", dependencies=[Depends(require_admin)])
async def begin_connection(
    provider: str,
    practitioner: str = Query(description="practitioner slug"),
    clinic_id: uuid.UUID = Depends(current_clinic_id),
) -> RedirectResponse:
    """Send the practitioner to the provider's consent screen."""
    config = _provider_or_404(provider)

    async with tenant_session(clinic_id) as session:
        row = await repo.get_practitioner(session, slug=practitioner)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No practitioner {practitioner!r}")

    if not config["client_id"](settings):
        raise HTTPException(
            status_code=503,
            detail=f"{provider} is not configured; set its client id and secret.",
        )

    state = sign_state(
        {
            "clinic_id": str(clinic_id),
            "practitioner": practitioner,
            "provider": provider,
            # Binds this redirect to this attempt, so a captured consent URL
            # cannot be replayed to attach a different account.
            "nonce": secrets.token_urlsafe(16),
        }
    )
    url = config["module"].authorization_url(state=state, redirect_uri=_redirect_uri(provider))
    return RedirectResponse(url, status_code=302)


@router.get("/{provider}/callback", response_class=HTMLResponse)
async def finish_connection(
    provider: str,
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    """Exchange the one-time code and store the refresh token, encrypted.

    No admin token here: the caller is the provider redirecting a browser back,
    and it cannot carry our header. The sealed state is what authenticates this
    request — it proves we started the flow and says for whom.
    """
    config = _provider_or_404(provider)

    if error:
        return _page(f"The provider reported an error: {error}", ok=False)
    if not code or not state:
        return _page("That callback was missing its code or state.", ok=False)

    try:
        claims = verify_state(state)
    except TokenEncryptionUnavailable:
        return _page("That authorisation link was invalid or has expired.", ok=False)

    if claims.get("provider") != provider:
        return _page("That authorisation was for a different provider.", ok=False)

    clinic_id = uuid.UUID(claims["clinic_id"])
    token_url = config["token_url"] or microsoft._endpoints().token_url

    try:
        payload = await exchange_authorization_code(
            provider=provider,
            code=code,
            token_url=token_url,
            client_id=config["client_id"](settings),
            client_secret=config["client_secret"](settings),
            redirect_uri=_redirect_uri(provider),
        )
    except OAuthError as exc:
        return _page(str(exc), ok=False)

    email = _account_email(payload) or claims["practitioner"]

    async with tenant_session(clinic_id) as session:
        practitioner = await repo.get_practitioner(session, slug=claims["practitioner"])
        if practitioner is None:
            return _page("That practitioner no longer exists.", ok=False)

        existing = [
            a
            for a in await repo.load_calendar_accounts(session, practitioner_id=practitioner.id)
            if a.provider == provider
        ]
        encrypted = encrypt(payload["refresh_token"])

        if existing:
            existing[0].encrypted_refresh_token = encrypted
            existing[0].account_email = email
            existing[0].invalidated_at = None
        else:
            session.add(
                models.CalendarAccount(
                    clinic_id=clinic_id,
                    practitioner_id=practitioner.id,
                    provider=provider,
                    account_email=email,
                    calendar_id="primary",
                    encrypted_refresh_token=encrypted,
                    scopes=list(config["scopes"]),
                )
            )

        await repo.record_audit(
            session,
            clinic_id,
            actor="staff",
            action="calendar.connected",
            entity_type="calendar_account",
            entity_id=practitioner.id,
            # The token itself is never written to the audit trail.
            detail={"provider": provider, "account": email},
        )

    return _page(f"{practitioner.name}'s {provider} calendar is connected.", ok=True)


@router.delete("/{provider}/{practitioner}", dependencies=[Depends(require_admin)])
async def disconnect(
    provider: str,
    practitioner: str,
    clinic_id: uuid.UUID = Depends(current_clinic_id),
) -> dict[str, str]:
    """Forget a practitioner's calendar.

    The stored token is deleted rather than marked inactive: a credential we no
    longer use is a credential we should not still be holding.
    """
    _provider_or_404(provider)

    async with tenant_session(clinic_id) as session:
        row = await repo.get_practitioner(session, slug=practitioner)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No practitioner {practitioner!r}")

        accounts = [
            a
            for a in await repo.load_calendar_accounts(session, practitioner_id=row.id)
            if a.provider == provider
        ]
        for account in accounts:
            await session.delete(account)

        await repo.record_audit(
            session,
            clinic_id,
            actor="staff",
            action="calendar.disconnected",
            entity_type="calendar_account",
            entity_id=row.id,
            detail={"provider": provider},
        )

    return {"status": "disconnected", "practitioner": practitioner, "provider": provider}


def _account_email(payload: dict) -> str | None:
    """Pull the account address out of the id token, without verifying it.

    This is a display label only; nothing is authorised on the strength of it.
    The tokens that matter were issued directly by the provider over TLS.
    """
    import base64
    import json

    id_token = payload.get("id_token")
    if not id_token or id_token.count(".") != 2:
        return None
    try:
        body = id_token.split(".")[1]
        body += "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body))
    except Exception:  # noqa: BLE001 - a label is not worth an exception
        return None
    return claims.get("email") or claims.get("preferred_username")


def _page(message: str, *, ok: bool) -> HTMLResponse:
    """A plain confirmation page. The practitioner lands here from the provider."""
    colour = "#0f6d6d" if ok else "#a11"
    title = "Calendar connected" if ok else "Connection failed"
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title></head>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem">
<h1 style="color:{colour};font-size:1.25rem">{title}</h1>
<p style="color:#444">{message}</p>
<p style="color:#888;font-size:.9rem">You can close this window.</p>
</body></html>""",
        status_code=200 if ok else 400,
    )
