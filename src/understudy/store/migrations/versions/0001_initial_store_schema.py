"""Initial schema for runs, playbooks, and scenario_results.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-13 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    # 2. Create runs table
    op.create_table(
        "runs",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("incident_id", sa.Text(), nullable=False),
        sa.Column("scenario_id", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("prod_applied_plan", sa.Text(), nullable=True),
        sa.Column("prod_outcome", sa.Text(), nullable=True),
        sa.Column("escalation_reason", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_runs_scenario_id", "runs", ["scenario_id"], unique=False)
    op.execute("CREATE INDEX ix_runs_started_at_desc ON runs (started_at DESC);")

    # Append-only rules for runs table
    op.execute("CREATE RULE runs_no_update AS ON UPDATE TO runs DO INSTEAD NOTHING;")
    op.execute("CREATE RULE runs_no_delete AS ON DELETE TO runs DO INSTEAD NOTHING;")

    # 3. Create playbooks table
    op.create_table(
        "playbooks",
        sa.Column("playbook_id", sa.Text(), nullable=False),
        sa.Column("failure_class", sa.Text(), nullable=False),
        sa.Column("signature_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_refs", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("successes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("playbook_id"),
    )
    op.execute(
        "CREATE INDEX ix_playbooks_embedding_hnsw ON playbooks "
        "USING hnsw (embedding vector_cosine_ops);"
    )

    # 4. Create scenario_results table
    op.create_table(
        "scenario_results",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("scenario_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("repeat_index", sa.Integer(), nullable=False),
        sa.Column("twin_predicted_success", sa.Boolean(), nullable=True),
        sa.Column("prod_actual_success", sa.Boolean(), nullable=True),
        sa.Column("expected_escalation", sa.Boolean(), nullable=False),
        sa.Column("did_escalate", sa.Boolean(), nullable=False),
        sa.Column("runner_up_plan_id", sa.Text(), nullable=True),
        sa.Column("runner_up_prod_success", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("scenario_results")
    op.drop_table("playbooks")
    op.drop_table("runs")
