"""failure summaries

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18 10:05:31.642180
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column("summary_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_table(
        "summaries",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_summaries_run_id_runs", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_summaries"),
    )


def downgrade() -> None:
    op.drop_table("summaries")
    op.drop_column("checks", "summary_enabled")
