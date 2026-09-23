"""Database schema.

Two invariants are enforced by Postgres rather than by application code, because
application code is the thing most likely to have a bug in it:

1. No practitioner can hold two overlapping bookings  -> EXCLUDE constraint.
2. No request can read another clinic's rows           -> row level security.

Holds and confirmed appointments live in the same table specifically so that one
exclusion constraint covers both. A hold that is still inside its TTL blocks the
slot exactly as a confirmed appointment would.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _clinic_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False, index=True
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Clinic(Base):
    """The tenant. Every other table hangs off this and is filtered by RLS."""

    __tablename__ = "clinics"

    id: Mapped[uuid.UUID] = _pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/New_York")
    # SchedulingPolicy overrides: granularity, buffer, lead time, horizon.
    scheduling_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    practitioners: Mapped[list[Practitioner]] = relationship(back_populates="clinic")


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (UniqueConstraint("clinic_id", "code", name="uq_services_clinic_code"),)

    id: Mapped[uuid.UUID] = _pk()
    clinic_id: Mapped[uuid.UUID] = _clinic_fk()
    code: Mapped[str] = mapped_column(String(8), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)


class Practitioner(Base):
    __tablename__ = "practitioners"
    __table_args__ = (UniqueConstraint("clinic_id", "slug", name="uq_practitioners_clinic_slug"),)

    id: Mapped[uuid.UUID] = _pk()
    clinic_id: Mapped[uuid.UUID] = _clinic_fk()
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    seniority: Mapped[str] = mapped_column(String(16), nullable=False)
    service_codes: Mapped[list[str]] = mapped_column(ARRAY(String(8)), nullable=False)
    # [{"weekday": 0, "start": "09:00", "end": "18:00"}, ...] in clinic-local time.
    working_windows: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    clinic: Mapped[Clinic] = relationship(back_populates="practitioners")


class CalendarAccount(Base):
    """An OAuth connection to one practitioner's external calendar.

    Refresh tokens are encrypted at rest with a key held outside the database,
    so a database dump alone does not grant calendar access.
    """

    __tablename__ = "calendar_accounts"
    __table_args__ = (
        UniqueConstraint("practitioner_id", "provider", name="uq_calendar_practitioner_provider"),
        CheckConstraint("provider IN ('google','microsoft')", name="ck_calendar_provider"),
    )

    id: Mapped[uuid.UUID] = _pk()
    clinic_id: Mapped[uuid.UUID] = _clinic_fk()
    practitioner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("practitioners.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    account_email: Mapped[str] = mapped_column(String(320), nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(320), nullable=False, default="primary")
    encrypted_refresh_token: Mapped[bytes] = mapped_column(nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(200)), nullable=False)
    # Set when a refresh fails so the UI can prompt a reconnect instead of failing silently.
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class Patient(Base):
    """Deliberately minimal: contact details only, never clinical information.

    This is what bounds a leak. Nothing authenticates a patient, so the worst
    thing anyone can learn from this table is that a named person has a dental
    appointment. Service codes on an appointment are the only hint of clinical
    intent, and they are not diagnoses.
    """

    __tablename__ = "patients"
    __table_args__ = (Index("ix_patients_clinic_phone", "clinic_id", "phone"),)

    id: Mapped[uuid.UUID] = _pk()
    clinic_id: Mapped[uuid.UUID] = _clinic_fk()
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = _created_at()


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_appointments_positive_duration"),
        CheckConstraint(
            "status IN ('held','confirmed','cancelled','expired','completed','no_show',"
            "'superseded')",
            name="ck_appointments_status",
        ),
        CheckConstraint(
            "(status <> 'held') OR (hold_expires_at IS NOT NULL)",
            name="ck_appointments_hold_has_expiry",
        ),
        UniqueConstraint("clinic_id", "idempotency_key", name="uq_appointments_idempotency"),
        Index("ix_appointments_lookup", "clinic_id", "practitioner_id", "starts_at"),
        # The exclusion constraint itself is added in the migration; SQLAlchemy's
        # ExcludeConstraint cannot express the partial WHERE clause we need here.
    )

    id: Mapped[uuid.UUID] = _pk()
    clinic_id: Mapped[uuid.UUID] = _clinic_fk()
    practitioner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("practitioners.id", ondelete="RESTRICT"), nullable=False
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="SET NULL")
    )
    service_code: Mapped[str] = mapped_column(String(8), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="held")
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Written after the booking lands in our database, so a calendar API outage
    # degrades to "not yet mirrored" rather than losing the appointment.
    google_event_id: Mapped[str | None] = mapped_column(String(320))
    microsoft_event_id: Mapped[str | None] = mapped_column(String(320))
    mirror_error: Mapped[str | None] = mapped_column(Text)
    # Set on a hold that is a rescheduling of an existing appointment. The
    # replaced one is cancelled when this is confirmed, in the same
    # transaction, so a patient never ends up holding both or neither.
    replaces_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _pk()
    clinic_id: Mapped[uuid.UUID] = _clinic_fk()
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # chat | voice
    llm_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    llm_model: Mapped[str] = mapped_column(String(64), nullable=False)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalation_reason: Mapped[str | None] = mapped_column(String(64))
    # The number the patient identified themselves with, set when a lookup on
    # it returned something. Not verified — nothing sends a code to it — but it
    # is what lets the tools refuse an appointment id this conversation was
    # never given. See migration a6d5e0000007.
    identified_phone: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = _created_at()


class Message(Base):
    """One turn of a conversation.

    Ordered by `seq`, not by `created_at`. A whole agent turn is written in one
    transaction, so every row in it shares a transaction timestamp; the identity
    column is what actually preserves the order the conversation happened in.
    """

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_order", "conversation_id", "seq"),)

    id: Mapped[uuid.UUID] = _pk()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)
    clinic_id: Mapped[uuid.UUID] = _clinic_fk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class AuditLog(Base):
    """Append-only record of every booking mutation and every calendar write."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_clinic_created", "clinic_id", "created_at"),)

    id: Mapped[uuid.UUID] = _pk()
    clinic_id: Mapped[uuid.UUID] = _clinic_fk()
    actor: Mapped[str] = mapped_column(String(64), nullable=False)  # bot | staff:<id> | system
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()


TENANT_TABLES: tuple[str, ...] = (
    "services",
    "practitioners",
    "calendar_accounts",
    "patients",
    "appointments",
    "conversations",
    "messages",
    "audit_log",
)
