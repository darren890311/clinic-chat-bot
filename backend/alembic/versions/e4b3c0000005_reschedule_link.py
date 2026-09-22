"""an appointment can replace another

Moving an appointment is two operations — book the new time, release the old
one — and doing them as two separate requests leaves a patient holding either
both or neither. Booking first and cancelling after risks a double booking if
they walk away; cancelling first risks losing the slot and getting nothing.

Recording the link on the hold makes it one operation instead: when the
confirmation lands, the replaced appointment is cancelled in the same
transaction, so the exchange either happens or does not.

Revision ID: e4b3c0000005
Revises: d3a2b0000004
Create Date: 2026-09-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "e4b3c0000005"
down_revision = "d3a2b0000004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column(
            "replaces_appointment_id",
            UUID(as_uuid=True),
            sa.ForeignKey("appointments.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("appointments", "replaces_appointment_id")
