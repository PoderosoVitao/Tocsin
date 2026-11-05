"""evaluation rules and verdicts

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05 09:41:07.318842
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column(
            "eval_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "eval_reports",
        sa.Column("verdict", sa.String(length=16), server_default="ok", nullable=False),
    )
    op.add_column(
        "eval_reports",
        sa.Column(
            "findings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("eval_reports", "findings")
    op.drop_column("eval_reports", "verdict")
    op.drop_column("checks", "eval_rules")
