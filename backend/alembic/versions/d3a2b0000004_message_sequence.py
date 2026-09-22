"""messages need a strict order, not a timestamp

Every message of one agent turn — the patient's line, the assistant's, and each
tool result — is written inside a single transaction. `now()` returns the
transaction start time, so they all share a `created_at` and ordering by it
falls back to the tie-breaker, which was a random UUID.

The consequence is not cosmetic. Replaying a scrambled transcript hands the
model tool results before the calls that produced them, and providers reject a
tool result whose call has not been seen yet. The conversation is the order.

An identity column is monotonic per insert rather than per transaction, so it
orders correctly no matter how many rows a turn writes.

Revision ID: d3a2b0000004
Revises: c2f1a0000003
Create Date: 2026-09-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3a2b0000004"
down_revision = "c2f1a0000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
    )
    op.create_index("ix_messages_order", "messages", ["conversation_id", "seq"])
    op.drop_index("ix_messages_conversation", table_name="messages")


def downgrade() -> None:
    op.create_index("ix_messages_conversation", "messages", ["conversation_id", "created_at"])
    op.drop_index("ix_messages_order", table_name="messages")
    op.drop_column("messages", "seq")
