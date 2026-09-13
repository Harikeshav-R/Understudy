"""Postgres-backed implementations of RunStore, PlaybookStore, and EvalStore.

Conforms to protocols defined in understudy.store.api:
- PostgresRunStore: append-only run record persistence
- PostgresPlaybookStore: pgvector-backed playbook storage & nearest-neighbor search
- PostgresEvalStore: evaluation scenario results storage
"""

from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from understudy.common.clock import Clock, resolve_clock
from understudy.common.errors import StoreError
from understudy.contracts.enums import FailureClass
from understudy.contracts.plan import RemediationPlan
from understudy.contracts.run import RunRecord
from understudy.store.api import EvalStore, PlaybookSearchResult, PlaybookStore, RunStore
from understudy.store.database import StoreDatabase
from understudy.store.models import PlaybookModel, RunModel, ScenarioResultModel

# Arbitrary fixed key scoping the K5 single-writer advisory lock taken in claim_run().
_RUN_CLAIM_ADVISORY_LOCK_KEY = 72176


def _run_model_from_record(record: RunRecord) -> RunModel:
    outcome_val = record.outcome.value if hasattr(record.outcome, "value") else str(record.outcome)
    return RunModel(
        run_id=record.run_id,
        incident_id=record.incident_id,
        scenario_id=record.scenario_id,
        started_at=record.started_at,
        finished_at=record.finished_at,
        outcome=outcome_val,
        prod_applied_plan=record.prod_applied_plan_id,
        prod_outcome=record.prod_outcome,
        escalation_reason=record.escalation_reason,
        payload=record.model_dump(mode="json"),
    )


class PostgresRunStore(RunStore):
    """PostgreSQL implementation of the append-only RunStore."""

    def __init__(self, db: StoreDatabase | None = None) -> None:
        self._db = db or StoreDatabase()

    async def record_run(self, record: RunRecord) -> None:
        """Persist an immutable run record append-only."""
        model = _run_model_from_record(record)
        try:
            async with self._db.session() as session:
                session.add(model)
                await session.commit()
        except StoreError as exc:
            if isinstance(exc.__cause__, IntegrityError):
                raise StoreError(
                    f"Run {record.run_id} already exists; runs table is append-only",
                    details={"run_id": record.run_id},
                ) from exc
            raise

    async def claim_run(self, record: RunRecord) -> None:
        """Atomically verify no other active run exists and persist `record`.

        Takes a Postgres advisory transaction lock so the "no active run" check and
        the insert happen atomically across concurrent callers (kernel invariant K5,
        docs/03-invariants.md). The lock is released automatically at transaction end.
        """
        model = _run_model_from_record(record)
        try:
            async with self._db.session() as session:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": _RUN_CLAIM_ADVISORY_LOCK_KEY},
                )
                stmt = select(RunModel).where(RunModel.finished_at.is_(None)).limit(1)
                res = await session.execute(stmt)
                if res.scalar_one_or_none() is not None:
                    raise StoreError(
                        "Another active run already exists; cannot claim a new run",
                        details={"run_id": record.run_id},
                    )
                session.add(model)
                await session.commit()
        except StoreError as exc:
            if isinstance(exc.__cause__, IntegrityError):
                raise StoreError(
                    f"Run {record.run_id} already exists; runs table is append-only",
                    details={"run_id": record.run_id},
                ) from exc
            raise

    async def get_run(self, run_id: str) -> RunRecord | None:
        """Retrieve a run record by its unique identifier."""
        async with self._db.session() as session:
            stmt = select(RunModel).where(RunModel.run_id == run_id)
            res = await session.execute(stmt)
            model = res.scalar_one_or_none()
            if model is None:
                return None
            return RunRecord.model_validate(model.payload)

    async def list_runs(
        self, incident_id: str | None = None, scenario_id: str | None = None
    ) -> list[RunRecord]:
        """List runs matching optional incident_id or scenario_id filters."""
        async with self._db.session() as session:
            stmt = select(RunModel)
            if incident_id is not None:
                stmt = stmt.where(RunModel.incident_id == incident_id)
            if scenario_id is not None:
                stmt = stmt.where(RunModel.scenario_id == scenario_id)
            stmt = stmt.order_by(RunModel.started_at.desc())
            res = await session.execute(stmt)
            models = res.scalars().all()
            return [RunRecord.model_validate(m.payload) for m in models]

    async def get_active_runs(self) -> list[RunRecord]:
        """List runs with no finished_at timestamp yet."""
        async with self._db.session() as session:
            stmt = (
                select(RunModel)
                .where(RunModel.finished_at.is_(None))
                .order_by(RunModel.started_at.desc())
            )
            res = await session.execute(stmt)
            models = res.scalars().all()
            return [RunRecord.model_validate(m.payload) for m in models]


class PostgresPlaybookStore(PlaybookStore):
    """PostgreSQL implementation of PlaybookStore with pgvector nearest-neighbor search."""

    def __init__(self, db: StoreDatabase | None = None, clock: Clock | None = None) -> None:
        self._db = db or StoreDatabase()
        self._clock = resolve_clock(clock)

    async def save_playbook(
        self,
        playbook_id: str,
        failure_class: FailureClass,
        signature_text: str,
        embedding: list[float],
        plan: RemediationPlan,
        evidence_refs: list[str],
        origin: str,
    ) -> None:
        """Save a new or updated playbook template."""
        now = self._clock.now()
        fc_val = failure_class.value if hasattr(failure_class, "value") else str(failure_class)
        plan_json = plan.model_dump(mode="json")

        stmt = (
            pg_insert(PlaybookModel)
            .values(
                playbook_id=playbook_id,
                failure_class=fc_val,
                signature_text=signature_text,
                embedding=embedding,
                plan=plan_json,
                evidence_refs=list(evidence_refs),
                origin=origin,
                successes=0,
                failures=0,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[PlaybookModel.playbook_id],
                set_={
                    "failure_class": fc_val,
                    "signature_text": signature_text,
                    "embedding": embedding,
                    "plan": plan_json,
                    "evidence_refs": list(evidence_refs),
                    "origin": origin,
                    "updated_at": now,
                },
            )
        )

        async with self._db.session() as session:
            await session.execute(stmt)
            await session.commit()

    async def get_playbook(self, playbook_id: str) -> RemediationPlan | None:
        """Retrieve a playbook plan template by identifier."""
        async with self._db.session() as session:
            stmt = select(PlaybookModel).where(PlaybookModel.playbook_id == playbook_id)
            res = await session.execute(stmt)
            model = res.scalar_one_or_none()
            if model is None:
                return None
            return RemediationPlan.model_validate(model.plan)

    async def search_playbooks(
        self, embedding: list[float], limit: int = 5
    ) -> list[RemediationPlan]:
        """Find candidate playbooks nearest to the given embedding using cosine distance."""
        async with self._db.session() as session:
            stmt = (
                select(PlaybookModel)
                .order_by(PlaybookModel.embedding.cosine_distance(embedding))
                .limit(limit)
            )
            res = await session.execute(stmt)
            models = res.scalars().all()
            return [RemediationPlan.model_validate(m.plan) for m in models]

    async def search_playbooks_with_scores(
        self, embedding: list[float], limit: int = 3
    ) -> list[PlaybookSearchResult]:
        """Find candidate playbooks nearest to the given embedding with similarity scores."""
        async with self._db.session() as session:
            similarity_expr = (1.0 - PlaybookModel.embedding.cosine_distance(embedding)).label(
                "similarity"
            )
            stmt = (
                select(PlaybookModel, similarity_expr)
                .order_by(PlaybookModel.embedding.cosine_distance(embedding))
                .limit(limit)
            )
            res = await session.execute(stmt)
            rows = res.all()
            results: list[PlaybookSearchResult] = []
            for model, sim in rows:
                results.append(
                    PlaybookSearchResult(
                        playbook_id=model.playbook_id,
                        failure_class=model.failure_class,
                        signature_text=model.signature_text,
                        similarity=float(sim),
                        plan=RemediationPlan.model_validate(model.plan),
                        evidence_refs=list(model.evidence_refs),
                        successes=model.successes,
                        failures=model.failures,
                        origin=model.origin,
                    )
                )
            return results

    async def list_playbooks(self) -> list[PlaybookSearchResult]:
        """List all stored playbooks with operational metadata."""
        async with self._db.session() as session:
            stmt = select(PlaybookModel).order_by(PlaybookModel.created_at.desc())
            res = await session.execute(stmt)
            models = res.scalars().all()
            return [
                PlaybookSearchResult(
                    playbook_id=m.playbook_id,
                    failure_class=m.failure_class,
                    signature_text=m.signature_text,
                    similarity=1.0,
                    plan=RemediationPlan.model_validate(m.plan),
                    evidence_refs=list(m.evidence_refs),
                    successes=m.successes,
                    failures=m.failures,
                    origin=m.origin,
                )
                for m in models
            ]

    async def increment_success(self, playbook_id: str) -> None:
        """Increment the successful resolution counter for a playbook."""
        now = self._clock.now()
        async with self._db.session() as session:
            stmt = (
                update(PlaybookModel)
                .where(PlaybookModel.playbook_id == playbook_id)
                .values(
                    successes=PlaybookModel.successes + 1,
                    updated_at=now,
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def increment_failure(self, playbook_id: str) -> None:
        """Increment the failure counter for a playbook."""
        now = self._clock.now()
        async with self._db.session() as session:
            stmt = (
                update(PlaybookModel)
                .where(PlaybookModel.playbook_id == playbook_id)
                .values(
                    failures=PlaybookModel.failures + 1,
                    updated_at=now,
                )
            )
            await session.execute(stmt)
            await session.commit()


class PostgresEvalStore(EvalStore):
    """PostgreSQL implementation of EvalStore for evaluation metrics."""

    def __init__(self, db: StoreDatabase | None = None, clock: Clock | None = None) -> None:
        self._db = db or StoreDatabase()
        self._clock = resolve_clock(clock)

    async def record_scenario_result(
        self,
        scenario_id: str,
        run_id: str,
        repeat_index: int,
        twin_predicted_success: bool | None,
        prod_actual_success: bool | None,
        expected_escalation: bool,
        did_escalate: bool,
        runner_up_plan_id: str | None = None,
        runner_up_prod_success: bool | None = None,
    ) -> None:
        """Record evaluation execution results for a scenario repeat."""
        now = self._clock.now()
        model = ScenarioResultModel(
            scenario_id=scenario_id,
            run_id=run_id,
            repeat_index=repeat_index,
            twin_predicted_success=twin_predicted_success,
            prod_actual_success=prod_actual_success,
            expected_escalation=expected_escalation,
            did_escalate=did_escalate,
            runner_up_plan_id=runner_up_plan_id,
            runner_up_prod_success=runner_up_prod_success,
            created_at=now,
        )
        async with self._db.session() as session:
            session.add(model)
            await session.commit()

    async def get_scenario_results(self, scenario_id: str | None = None) -> list[dict[str, Any]]:
        """Retrieve evaluation results across scenarios."""
        async with self._db.session() as session:
            stmt = select(ScenarioResultModel)
            if scenario_id is not None:
                stmt = stmt.where(ScenarioResultModel.scenario_id == scenario_id)
            stmt = stmt.order_by(ScenarioResultModel.id.asc())
            res = await session.execute(stmt)
            models = res.scalars().all()
            return [
                {
                    "scenario_id": m.scenario_id,
                    "run_id": m.run_id,
                    "repeat_index": m.repeat_index,
                    "twin_predicted_success": m.twin_predicted_success,
                    "prod_actual_success": m.prod_actual_success,
                    "expected_escalation": m.expected_escalation,
                    "did_escalate": m.did_escalate,
                    "runner_up_plan_id": m.runner_up_plan_id,
                    "runner_up_prod_success": m.runner_up_prod_success,
                }
                for m in models
            ]
