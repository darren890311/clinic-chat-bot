"""the name a conversation has been given

`identified_phone` recorded the number a lookup answered on. A number is not
enough on a shared family mobile, where it returns somebody else's
appointments, so the lookup now takes a name as well and this records it.

Both are required to reach an appointment the conversation did not book. Still
not authentication — a name is not a secret either — but a number on its own
is no longer a key to another person's record.

Revision ID: b7e6f0000008
Revises: a6d5e0000007
Create Date: 2026-09-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7e6f0000008"
down_revision = "a6d5e0000007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("identified_name", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "identified_name")
