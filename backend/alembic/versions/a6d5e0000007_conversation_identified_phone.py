"""the phone a conversation has been given

Three tools take an appointment id and act on it: cancel, and the two halves
of a move. Row level security keeps an id inside its own clinic, but within a
clinic any conversation could cancel or move any appointment. An id is only
unguessable, which is not the same as being checked.

A conversation legitimately learns an id one of two ways: it booked the
appointment itself, which `appointments.conversation_id` already records, or
the patient gave a phone number and the lookup returned it. The second left no
trace. This records it, so the tools can refuse an id the conversation was
never given.

The column is deliberately not called `verified_phone`. Nothing verifies it —
the patient says a number and the lookup answers. It is the handle they
identified themselves with, and what it buys is that an id alone is no longer
enough to act on. Authenticating the patient needs a code sent to the number,
which is out of scope here.

Revision ID: a6d5e0000007
Revises: f5c4d0000006
Create Date: 2026-09-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a6d5e0000007"
down_revision = "f5c4d0000006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("identified_phone", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "identified_phone")
