"""SQLAlchemy ORM models for Understudy storage schema.

Implements database schema defined in docs/02-architecture.md §2.12:
- runs (append-only, with PostgreSQL rules runs_no_update and runs_no_delete)
- playbooks (pgvector embeddings, vector_cosine_ops HNSW index)
- scenario_results (evaluation execution results with foreign key to runs)
"""

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base declarative class for Understudy store models."""


class RunModel(Base):
    """SQLAlchemy model for append-only incident run records."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    incident_id: Mapped[str] = mapped_column(Text, nullable=False)
    scenario_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    prod_applied_plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    prod_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (Index("ix_runs_started_at_desc", started_at.desc()),)


# Attach append-only rules upon table creation
def _create_runs_append_only_rules(
    target: Any,
    connection: Any,
    **kw: Any,
) -> None:
    _ = (target, kw)
    connection.execute(
        text(
            "CREATE RULE runs_no_update AS ON UPDATE TO runs DO INSTEAD NOTHING;\n"
            "CREATE RULE runs_no_delete AS ON DELETE TO runs DO INSTEAD NOTHING;"
        )
    )


event.listen(
    RunModel.__table__,
    "after_create",
    _create_runs_append_only_rules,
)


class PlaybookModel(Base):
    """SQLAlchemy model for incident playbooks and vector embeddings."""

    __tablename__ = "playbooks"

    playbook_id: Mapped[str] = mapped_column(Text, primary_key=True)
    failure_class: Mapped[str] = mapped_column(Text, nullable=False)
    signature_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_playbooks_embedding_hnsw",
            embedding,
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class ScenarioResultModel(Base):
    """SQLAlchemy model for evaluation scenario execution metrics."""

    __tablename__ = "scenario_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scenario_id: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str] = mapped_column(Text, ForeignKey("runs.run_id"), nullable=False)
    repeat_index: Mapped[int] = mapped_column(Integer, nullable=False)
    twin_predicted_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    prod_actual_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    expected_escalation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    did_escalate: Mapped[bool] = mapped_column(Boolean, nullable=False)
    runner_up_plan_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    runner_up_prod_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
