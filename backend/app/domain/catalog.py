"""Service catalog and practitioner competencies.

This module is the canonical definition of what the clinic sells and who can
deliver it. The database seed is generated from here so the two cannot drift.

Competency rule (per the clinic's brief):
  - Routine Cleaning / Dental Examination : all three practitioners
  - Everything else                       : senior practitioners only
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum


class Seniority(StrEnum):
    JUNIOR = "junior"
    SENIOR = "senior"


@dataclass(frozen=True)
class Service:
    code: str
    name: str
    duration_minutes: int
    description: str

    @property
    def duration(self) -> timedelta:
        return timedelta(minutes=self.duration_minutes)


@dataclass(frozen=True)
class Practitioner:
    slug: str
    name: str
    title: str
    seniority: Seniority
    service_codes: frozenset[str]

    def can_perform(self, service_code: str) -> bool:
        return service_code in self.service_codes


SERVICES: dict[str, Service] = {
    s.code: s
    for s in [
        Service("A", "Routine Cleaning", 60, "Scale, polish and fluoride treatment."),
        Service("B", "Dental Examination", 60, "Full check-up including digital X-rays."),
        Service("C", "Root Canal Treatment", 150, "Endodontic therapy on a single tooth."),
        Service("D", "Crown Fitting", 120, "Preparation and placement of a permanent crown."),
        Service("E", "Full Mouth Restoration", 360, "Multi-quadrant restorative work, full day."),
    ]
}

BASIC_SERVICES = frozenset({"A", "B"})
ALL_SERVICES = frozenset(SERVICES)

PRACTITIONERS: dict[str, Practitioner] = {
    p.slug: p
    for p in [
        Practitioner(
            slug="dr-hale",
            name="Dr. Evelyn Hale",
            title="Senior Dentist",
            seniority=Seniority.SENIOR,
            service_codes=ALL_SERVICES,
        ),
        Practitioner(
            slug="dr-okafor",
            name="Dr. Daniel Okafor",
            title="Senior Dentist",
            seniority=Seniority.SENIOR,
            service_codes=ALL_SERVICES,
        ),
        Practitioner(
            slug="dr-ramos",
            name="Dr. Mia Ramos",
            title="Associate Dentist",
            seniority=Seniority.JUNIOR,
            service_codes=BASIC_SERVICES,
        ),
    ]
}


def get_service(code: str) -> Service:
    try:
        return SERVICES[code.strip().upper()]
    except KeyError:
        raise UnknownServiceError(code) from None


def practitioners_for(service_code: str) -> list[Practitioner]:
    """Practitioners qualified to deliver a service, seniors last.

    Ordering matters: we prefer to fill the junior's calendar for basic services
    so senior capacity stays free for work only they can do.
    """
    code = get_service(service_code).code
    qualified = [p for p in PRACTITIONERS.values() if p.can_perform(code)]
    return sorted(qualified, key=lambda p: (p.seniority == Seniority.SENIOR, p.slug))


class UnknownServiceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Unknown service code: {code!r}. Valid codes: {sorted(SERVICES)}")
