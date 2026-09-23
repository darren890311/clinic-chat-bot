"""what each conversation cost

Token counts came back from the provider on every turn and were shown in the
corner of the screen, then discarded. Asked afterwards what a month of testing
had cost, the only available answer was to count messages and multiply by a
figure measured once, which gives a range rather than a number.

A practice will ask the same question with more at stake. Totals are kept on
the conversation and added to as the tokens are spent, rather than written at
the end: a turn has several exits, and one that forgets to record is a turn
that was paid for and not counted.

Revision ID: c8f7a0000009
Revises: b7e6f0000008
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c8f7a0000009"
down_revision = "b7e6f0000008"
branch_labels = None
depends_on = None

COLUMNS = ("input_tokens", "output_tokens", "cached_tokens")


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column(
            "conversations",
            sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for name in COLUMNS:
        op.drop_column("conversations", name)
