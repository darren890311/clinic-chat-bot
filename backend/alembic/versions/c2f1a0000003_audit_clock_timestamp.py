"""audit entries need wall-clock times, not transaction times

`now()` in Postgres returns the transaction start time, so every audit row
written while booking one appointment — the hold, the confirmation, the
cancellation — shares a timestamp and their order is undefined. An audit trail
whose entries cannot be ordered is a weak one.

`clock_timestamp()` reads the actual clock per statement. Only audit_log
changes: everywhere else the transaction time is the correct semantics, because
an appointment's `created_at` should not depend on where in the transaction the
insert happened to land.

Revision ID: c2f1a0000003
Revises: b1c0de000002
Create Date: 2026-09-22
"""

from __future__ import annotations

from alembic import op

revision = "c2f1a0000003"
down_revision = "b1c0de000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE audit_log ALTER COLUMN created_at SET DEFAULT clock_timestamp()")


def downgrade() -> None:
    op.execute("ALTER TABLE audit_log ALTER COLUMN created_at SET DEFAULT now()")
