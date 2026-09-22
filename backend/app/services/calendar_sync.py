"""Reading and writing practitioners' external calendars.

Two directions, with deliberately different failure behaviour:

* **Reading busy time** happens before we offer a slot and again before we
  confirm it. If a provider is unreachable we fail the booking rather than
  book over a commitment we could not see.
* **Writing the event** happens after the appointment already exists in our
  database. If a provider is unreachable the appointment still stands and the
  failure is recorded on the row; the mirror is a projection, not the source of
  truth.

This module is the only place that knows a practitioner might have more than one
calendar. The scheduling engine sees a flat list of busy intervals.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.db import repository as repo
from app.domain.intervals import Interval, merge
from app.providers.calendar import (
    CalendarCredentials,
    CalendarError,
    get_provider,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MirrorResult:
    google_event_id: str | None = None
    microsoft_event_id: str | None = None
    error: str | None = None


def _credentials(account: models.CalendarAccount, refresh_token: str) -> CalendarCredentials:
    return CalendarCredentials(
        provider=account.provider,
        account_email=account.account_email,
        calendar_id=account.calendar_id,
        refresh_token=refresh_token,
    )


def _decrypt(account: models.CalendarAccount) -> str:
    """Decrypt a stored refresh token.

    Tokens are encrypted at rest with a key held outside the database, so a
    database dump alone does not grant calendar access. The null provider needs
    no token, which is why an empty value is tolerated here rather than raising.
    """
    if not account.encrypted_refresh_token:
        return ""
    from app.services.crypto import decrypt

    return decrypt(account.encrypted_refresh_token)


async def external_busy(
    session: AsyncSession,
    *,
    practitioner_id: uuid.UUID,
    window: Interval,
) -> list[Interval]:
    """Busy intervals from every calendar this practitioner has connected.

    Providers are queried concurrently: a practitioner with both Google and
    Outlook connected should not pay for two round trips in sequence on a call
    where someone is waiting to hear their options.
    """
    accounts = await repo.load_calendar_accounts(session, practitioner_id=practitioner_id)
    if not accounts:
        return []

    async def fetch(account: models.CalendarAccount) -> list[Interval]:
        provider = get_provider(account.provider)
        return await provider.get_busy(_credentials(account, _decrypt(account)), window)

    results = await asyncio.gather(*(fetch(a) for a in accounts), return_exceptions=True)

    busy: list[Interval] = []
    for account, result in zip(accounts, results, strict=True):
        if isinstance(result, BaseException):
            # Fail closed. Offering a slot we could not verify is worse than
            # telling the patient we cannot check availability right now.
            raise CalendarError(
                account.provider,
                f"could not read availability for {account.account_email}: {result}",
            ) from result
        busy.extend(result)

    return merge(busy)


async def mirror_appointment(
    session: AsyncSession,
    *,
    appointment: models.Appointment,
    practitioner: models.Practitioner,
    summary: str,
    description: str,
) -> MirrorResult:
    """Write the appointment into the practitioner's calendars.

    Never raises. The booking is already committed by the time this runs, and a
    calendar outage must not undo it. Failures are returned for the caller to
    persist on the row so staff can see what has not been mirrored.
    """
    accounts = await repo.load_calendar_accounts(session, practitioner_id=practitioner.id)
    if not accounts:
        return MirrorResult()

    ids: dict[str, str] = {}
    errors: list[str] = []

    for account in accounts:
        try:
            provider = get_provider(account.provider)
            event = await provider.create_event(
                _credentials(account, _decrypt(account)),
                start=appointment.starts_at,
                end=appointment.ends_at,
                summary=summary,
                description=description,
                # Same key on a retry means the provider updates rather than
                # duplicates, so a partial failure can be replayed safely.
                idempotency_key=str(appointment.id),
            )
            ids[account.provider] = event.event_id
        except Exception as exc:  # noqa: BLE001 - deliberately swallowed
            logger.warning(
                "calendar mirror failed", extra={"provider": account.provider, "error": str(exc)}
            )
            errors.append(f"{account.provider}: {exc}")

    return MirrorResult(
        google_event_id=ids.get("google"),
        microsoft_event_id=ids.get("microsoft"),
        error="; ".join(errors) or None,
    )


async def withdraw_appointment(session: AsyncSession, *, appointment: models.Appointment) -> None:
    """Remove a cancelled appointment from the external calendars. Never raises."""
    accounts = await repo.load_calendar_accounts(
        session, practitioner_id=appointment.practitioner_id
    )
    event_ids = {
        "google": appointment.google_event_id,
        "microsoft": appointment.microsoft_event_id,
    }
    for account in accounts:
        event_id = event_ids.get(account.provider)
        if not event_id:
            continue
        try:
            provider = get_provider(account.provider)
            await provider.delete_event(_credentials(account, _decrypt(account)), event_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "calendar withdrawal failed",
                extra={"provider": account.provider, "error": str(exc)},
            )
