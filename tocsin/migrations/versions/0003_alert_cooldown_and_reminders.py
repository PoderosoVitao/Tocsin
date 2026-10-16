"""alert cooldown and reminders

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16 10:28:09.136530
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column("cooldown_seconds", sa.Integer(), server_default=sa.text("300"), nullable=False),
    )
    op.add_column("checks", sa.Column("reminder_seconds", sa.Integer(), nullable=True))
    op.add_column("checks", sa.Column("last_alert_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("checks", sa.Column("last_alerted_state", sa.String(length=16), nullable=True))
    op.add_column(
        "events",
        sa.Column("suppressed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("events", "suppressed")
    op.drop_column("checks", "last_alerted_state")
    op.drop_column("checks", "last_alert_at")
    op.drop_column("checks", "reminder_seconds")
    op.drop_column("checks", "cooldown_seconds")
