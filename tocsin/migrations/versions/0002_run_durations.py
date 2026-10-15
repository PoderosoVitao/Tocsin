"""run durations

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14 19:08:27.324120
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("max_duration_seconds", sa.Integer(), nullable=True))
    op.add_column("checks", sa.Column("last_start_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("checks", "last_start_at")
    op.drop_column("checks", "max_duration_seconds")
