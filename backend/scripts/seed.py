"""Seed a clinic from the canonical catalog in app.domain.catalog.

Idempotent: safe to re-run. Run after `alembic upgrade head`.

    python -m scripts.seed --slug darren-dental --name "Darren Dental"
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select, text

from app.db import models
from app.db.session import SessionFactory
from app.domain.catalog import PRACTITIONERS, SERVICES

# Mon-Fri 09:00-18:00, Sat 09:00-13:00. A six-hour service cannot fit a Saturday,
# which is a real constraint the scheduling engine derives rather than hard-codes.
WEEKDAY = [{"weekday": d, "start": "09:00", "end": "18:00"} for d in range(5)]
SATURDAY = [{"weekday": 5, "start": "09:00", "end": "13:00"}]
WORKING_WINDOWS = WEEKDAY + SATURDAY


async def _scope(session, clinic_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.clinic_id', :id, true)"), {"id": str(clinic_id)}
    )


async def seed(slug: str, name: str, timezone: str, phone: str) -> uuid.UUID:
    async with SessionFactory() as session, session.begin():
        clinic_id = (
            await session.execute(text("SELECT public.resolve_clinic(:s)"), {"s": slug})
        ).scalar_one_or_none()

        if clinic_id is None:
            clinic_id = uuid.uuid4()
            # Scope the transaction to the clinic we are about to create.
            # The policy on `clinics` is WITH CHECK (id = app.clinic_id), so
            # provisioning satisfies it without any exemption: you may create
            # exactly the tenant you have already scoped yourself to.
            await _scope(session, clinic_id)
            await session.execute(
                text(
                    "INSERT INTO clinics"
                    " (id, slug, name, timezone, contact_phone, scheduling_policy)"
                    " VALUES (:id, :slug, :name, :tz, :phone, '{}')"
                ),
                {
                    "id": clinic_id,
                    "slug": slug,
                    "name": name,
                    "tz": timezone,
                    "phone": phone,
                },
            )
            print(f"created clinic {slug} ({clinic_id})")
        else:
            await _scope(session, clinic_id)
            # Reseeding an existing demo clinic moves it, rather than printing
            # that it exists and leaving the old timezone in place. Without
            # this, changing the default here has no effect on any database
            # that has already been seeded — which is every one of them.
            await session.execute(
                text("UPDATE clinics SET timezone = :tz, contact_phone = :phone WHERE id = :id"),
                {"id": clinic_id, "tz": timezone, "phone": phone},
            )
            print(f"clinic {slug} already exists ({clinic_id}); timezone set to {timezone}")

        existing_services = {
            s.code for s in (await session.execute(select(models.Service))).scalars()
        }
        for svc in SERVICES.values():
            if svc.code in existing_services:
                continue
            session.add(
                models.Service(
                    clinic_id=clinic_id,
                    code=svc.code,
                    name=svc.name,
                    duration_minutes=svc.duration_minutes,
                    description=svc.description,
                )
            )
            print(f"  + service {svc.code} {svc.name} ({svc.duration_minutes}m)")

        existing_practitioners = {
            p.slug for p in (await session.execute(select(models.Practitioner))).scalars()
        }
        for prac in PRACTITIONERS.values():
            if prac.slug in existing_practitioners:
                continue
            session.add(
                models.Practitioner(
                    clinic_id=clinic_id,
                    slug=prac.slug,
                    name=prac.name,
                    title=prac.title,
                    seniority=prac.seniority.value,
                    service_codes=sorted(prac.service_codes),
                    working_windows=WORKING_WINDOWS,
                )
            )
            print(f"  + {prac.name} ({prac.seniority}) -> {sorted(prac.service_codes)}")

    return clinic_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", default="darren-dental")
    parser.add_argument("--name", default="Darren Dental")
    # The demo clinic is in Taipei because that is where it is being shown.
    # Daylight saving is not lost by this: the engine's wall-clock projection
    # is asserted in tests/test_scheduling.py against America/New_York, which
    # does observe it, and a test is better evidence than a demonstration.
    parser.add_argument("--timezone", default="Asia/Taipei")
    parser.add_argument("--phone", default="+886 2 2345 6789")
    args = parser.parse_args()
    clinic_id = asyncio.run(seed(args.slug, args.name, args.timezone, args.phone))
    print(f"\nseeded: {args.slug} = {clinic_id}")


if __name__ == "__main__":
    main()
