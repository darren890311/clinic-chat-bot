"""record the tokens that cost the most per token

Cache writes bill at 1.25 times the input rate, so leaving them out of the
totals understates the bill by more than their share of the count suggests.
The provider already reported them and the port already carried them; only
the recording stopped short.

Revision ID: d9a8b000000a
Revises: c8f7a0000009
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d9a8b000000a"
down_revision = "c8f7a0000009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("conversations", "cache_write_tokens")
