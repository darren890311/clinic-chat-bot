from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db import repository as repo
from app.db.session import tenant_session, unscoped_session
from app.domain.intervals import Interval
from app.domain.scheduling import compute_availability
from app.providers.calendar import registered_providers

router = APIRouter(prefix="/api")
settings = get_settings()


async def current_clinic_id(
    clinic: str = Query(default=None, description="clinic slug; defaults to the configured clinic"),
) -> uuid.UUID:
    slug = clinic or settings.default_clinic_slug
    async with unscoped_session() as session:
        clinic_id = await repo.resolve_clinic_id(session, slug)
    if clinic_id is None:
        raise HTTPException(status_code=404, detail=f"Unknown clinic {slug!r}")
    return clinic_id


class ServiceOut(BaseModel):
    code: str
    name: str
    duration_minutes: int
    description: str


class SlotOut(BaseModel):
    start: datetime
    end: datetime
    practitioner_slug: str
    practitioner_name: str


class AvailabilityOut(BaseModel):
    service: ServiceOut
    searched_from: datetime
    searched_to: datetime
    slots: list[SlotOut] = Field(default_factory=list)


@router.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "environment": settings.environment,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "calendar_providers": registered_providers(),
    }


@router.get("/services", response_model=list[ServiceOut])
async def list_services(clinic_id: uuid.UUID = Depends(current_clinic_id)) -> list[ServiceOut]:
    async with tenant_session(clinic_id) as session:
        services = await repo.load_services(session, clinic_id)
    return [
        ServiceOut(
            code=s.code,
            name=s.name,
            duration_minutes=s.duration_minutes,
            description=s.description,
        )
        for s in sorted(services.values(), key=lambda s: s.code)
    ]


@router.get("/availability", response_model=AvailabilityOut)
async def availability(
    service: str,
    days: int = Query(default=14, ge=1, le=90),
    start: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    clinic_id: uuid.UUID = Depends(current_clinic_id),
) -> AvailabilityOut:
    now = datetime.now(UTC)
    search_from = start or now
    window = Interval(search_from, search_from + timedelta(days=days))

    async with tenant_session(clinic_id) as session:
        services = await repo.load_services(session, clinic_id)
        if service.upper() not in services:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown service {service!r}. Offered: {sorted(services)}",
            )
        svc = services[service.upper()]
        policy = await repo.load_policy(session, clinic_id)
        schedules = await repo.load_schedules(
            session, clinic_id, window=window, service_code=svc.code
        )

    names = {s.practitioner.slug: s.practitioner.name for s in schedules}
    slots = compute_availability(
        service=svc,
        schedules=schedules,
        search=window,
        policy=policy,
        now=now,
        limit=limit,
    )
    return AvailabilityOut(
        service=ServiceOut(
            code=svc.code,
            name=svc.name,
            duration_minutes=svc.duration_minutes,
            description=svc.description,
        ),
        searched_from=window.start,
        searched_to=window.end,
        slots=[
            SlotOut(
                start=s.start,
                end=s.end,
                practitioner_slug=s.practitioner_slug,
                practitioner_name=names.get(s.practitioner_slug, s.practitioner_slug),
            )
            for s in slots
        ],
    )
