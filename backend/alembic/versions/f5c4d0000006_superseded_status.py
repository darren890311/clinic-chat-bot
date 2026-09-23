"""a replaced appointment steps aside while its replacement is held

Moving an appointment to an adjacent time was impossible. The engine could be
told to ignore the appointment being given up, but the exclusion constraint
cannot: to Postgres, 09:00-10:00 and 09:30-10:30 simply overlap, and the hold
was rejected after the engine had already approved it.

A receptionist moving an appointment does not cancel and rebook — she changes
the time. The equivalent here is for the old appointment to step aside the
moment the new slot is held, in the same transaction, and to step back if the
hold expires unconfirmed.

`superseded` is that state. It is absent from the exclusion constraint's WHERE
clause, so it no longer occupies the practitioner's time, and it is distinct
from `cancelled` so the hold sweep knows it is restorable.

Revision ID: f5c4d0000006
Revises: e4b3c0000005
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op

revision = "f5c4d0000006"
down_revision = "e4b3c0000005"
branch_labels = None
depends_on = None

STATUSES = "'held','confirmed','cancelled','expired','completed','no_show','superseded'"


def upgrade() -> None:
    op.execute("ALTER TABLE appointments DROP CONSTRAINT ck_appointments_status")
    op.execute(
        f"ALTER TABLE appointments ADD CONSTRAINT ck_appointments_status "
        f"CHECK (status IN ({STATUSES}))"
    )


def downgrade() -> None:
    op.execute("UPDATE appointments SET status = 'cancelled' WHERE status = 'superseded'")
    op.execute("ALTER TABLE appointments DROP CONSTRAINT ck_appointments_status")
    op.execute(
        "ALTER TABLE appointments ADD CONSTRAINT ck_appointments_status "
        "CHECK (status IN ('held','confirmed','cancelled','expired','completed','no_show'))"
    )
