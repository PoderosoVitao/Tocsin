"""evaluation reports

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-02 10:12:44.510213
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("check_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("suite", sa.String(length=200), nullable=False),
        sa.Column(
            "labels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("report_url", sa.String(length=2000), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["check_id"],
            ["checks.id"],
            name=op.f("fk_eval_reports_check_id_checks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_eval_reports_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_reports")),
    )
    op.create_index("ix_eval_reports_check_id_id", "eval_reports", ["check_id", "id"], unique=False)
    op.create_table(
        "eval_scenarios",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("runs", sa.Integer(), nullable=False),
        sa.Column("passes", sa.Integer(), nullable=False),
        sa.Column("latency_ms", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["eval_reports.id"],
            name=op.f("fk_eval_scenarios_report_id_eval_reports"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_scenarios")),
        sa.UniqueConstraint("report_id", "name", name=op.f("uq_eval_scenarios_report_id_name")),
    )


def downgrade() -> None:
    op.drop_table("eval_scenarios")
    op.drop_index("ix_eval_reports_check_id_id", table_name="eval_reports")
    op.drop_table("eval_reports")
