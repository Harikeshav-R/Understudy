"""Unit tests for PostgresRunStore, PostgresPlaybookStore, and PostgresEvalStore."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from understudy.common.clock import FrozenClock
from understudy.common.errors import StoreError
from understudy.contracts.enums import ActionType, FailureClass, RunOutcome
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.plan import ActionParams, RemediationPlan
from understudy.contracts.run import RunRecord
from understudy.store.models import RunModel, ScenarioResultModel
from understudy.store.postgres import (
    PostgresEvalStore,
    PostgresPlaybookStore,
    PostgresRunStore,
)

FIXED_NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


class MockDatabase:
    """Mock database providing async session context manager for unit testing."""

    def __init__(self) -> None:
        self.session_mock = AsyncMock()
        self.session_mock.add = MagicMock()
        self.session_mock.commit = AsyncMock()
        self.session_mock.execute = AsyncMock()
        self.session_mock.rollback = AsyncMock()

    @asynccontextmanager
    async def session(self) -> Any:
        yield self.session_mock


@pytest.fixture
def sample_context() -> IncidentContext:
    alert = Alert(
        alert_id="alt_001",
        source="synthetic",
        title="Elevated latency on edge-gateway",
        service="edge-gateway",
        severity="critical",
        fired_at=FIXED_NOW,
    )
    metric_window = MetricWindow(
        service="edge-gateway",
        start_time=FIXED_NOW,
        end_time=FIXED_NOW,
        p99_latency_ms=450.0,
        error_rate=0.08,
        request_count=1200,
    )
    return IncidentContext(
        incident_id="inc_001",
        alert=alert,
        signatures=[],
        metrics_window=metric_window,
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(observed_at=FIXED_NOW),
        gathered_at=FIXED_NOW,
    )


@pytest.fixture
def sample_run_record(sample_context: IncidentContext) -> RunRecord:
    return RunRecord(
        run_id="run_001",
        incident_id="inc_001",
        scenario_id="sc_001",
        started_at=FIXED_NOW,
        finished_at=None,
        outcome=RunOutcome.EXECUTED,
        prod_applied_plan_id="plan_001",
        prod_outcome="resolved",
        escalation_reason=None,
        context=sample_context,
        plans=[],
        evidence=[],
    )


@pytest.fixture
def sample_plan() -> RemediationPlan:
    return RemediationPlan(
        plan_id="plan_001",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="edge-gateway"),
        target_resources=[],
        declared_blast_set=[],
        rationale="test",
        origin="planner",
    )


# --- PostgresRunStore tests ---


@pytest.mark.asyncio
async def test_postgres_run_store_record_run(sample_run_record: RunRecord) -> None:
    db = MockDatabase()
    store = PostgresRunStore(db=db)  # type: ignore[arg-type]

    await store.record_run(sample_run_record)
    db.session_mock.add.assert_called_once()
    db.session_mock.commit.assert_awaited_once()

    # Verify model fields
    added_model = db.session_mock.add.call_args[0][0]
    assert isinstance(added_model, RunModel)
    assert added_model.run_id == "run_001"
    assert added_model.outcome == "executed"
    assert added_model.prod_applied_plan == "plan_001"


@pytest.mark.asyncio
async def test_postgres_run_store_record_run_string_outcome() -> None:
    db = MockDatabase()
    store = PostgresRunStore(db=db)  # type: ignore[arg-type]

    # Create record mock with string outcome (no .value)
    mock_record = MagicMock()
    mock_record.run_id = "run_002"
    mock_record.incident_id = "inc_002"
    mock_record.scenario_id = None
    mock_record.started_at = FIXED_NOW
    mock_record.finished_at = None
    mock_record.outcome = "custom_outcome"
    mock_record.prod_applied_plan_id = None
    mock_record.prod_outcome = None
    mock_record.escalation_reason = None
    mock_record.model_dump.return_value = {"run_id": "run_002"}

    await store.record_run(mock_record)
    added = db.session_mock.add.call_args[0][0]
    assert added.outcome == "custom_outcome"


@pytest.mark.asyncio
async def test_postgres_run_store_record_run_integrity_error(sample_run_record: RunRecord) -> None:
    db = MockDatabase()
    store = PostgresRunStore(db=db)  # type: ignore[arg-type]

    # Simulate IntegrityError wrapped in StoreError
    cause = IntegrityError("duplicate key", params=None, orig=Exception("unique constraint"))
    db.session_mock.commit.side_effect = StoreError("session failure")
    db.session_mock.commit.side_effect.__cause__ = cause

    with pytest.raises(StoreError, match="already exists; runs table is append-only"):
        await store.record_run(sample_run_record)


@pytest.mark.asyncio
async def test_postgres_run_store_record_run_other_store_error(
    sample_run_record: RunRecord,
) -> None:
    db = MockDatabase()
    store = PostgresRunStore(db=db)  # type: ignore[arg-type]

    # Simulate generic StoreError not wrapping IntegrityError
    db.session_mock.commit.side_effect = StoreError("generic db failure")

    with pytest.raises(StoreError, match="generic db failure"):
        await store.record_run(sample_run_record)


@pytest.mark.asyncio
async def test_postgres_run_store_get_run(sample_run_record: RunRecord) -> None:
    db = MockDatabase()
    store = PostgresRunStore(db=db)  # type: ignore[arg-type]

    # Test found
    mock_result = MagicMock()
    mock_model = MagicMock()
    mock_model.payload = sample_run_record.model_dump(mode="json")
    mock_result.scalar_one_or_none.return_value = mock_model
    db.session_mock.execute.return_value = mock_result

    res = await store.get_run("run_001")
    assert res == sample_run_record

    # Test not found
    mock_result.scalar_one_or_none.return_value = None
    res_none = await store.get_run("run_missing")
    assert res_none is None


@pytest.mark.asyncio
async def test_postgres_run_store_list_runs(sample_run_record: RunRecord) -> None:
    db = MockDatabase()
    store = PostgresRunStore(db=db)  # type: ignore[arg-type]

    mock_model = MagicMock()
    mock_model.payload = sample_run_record.model_dump(mode="json")
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_model]
    db.session_mock.execute.return_value = mock_result

    # 1. Neither filter
    runs = await store.list_runs()
    assert runs == [sample_run_record]

    # 2. incident_id filter
    runs_inc = await store.list_runs(incident_id="inc_001")
    assert runs_inc == [sample_run_record]

    # 3. scenario_id filter
    runs_sc = await store.list_runs(scenario_id="sc_001")
    assert runs_sc == [sample_run_record]

    # 4. Both filters
    runs_both = await store.list_runs(incident_id="inc_001", scenario_id="sc_001")
    assert runs_both == [sample_run_record]


@pytest.mark.asyncio
async def test_postgres_run_store_get_active_runs(sample_run_record: RunRecord) -> None:
    db = MockDatabase()
    store = PostgresRunStore(db=db)  # type: ignore[arg-type]

    mock_model = MagicMock()
    mock_model.payload = sample_run_record.model_dump(mode="json")
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_model]
    db.session_mock.execute.return_value = mock_result

    active = await store.get_active_runs()
    assert active == [sample_run_record]


@pytest.mark.asyncio
async def test_postgres_run_store_claim_run_success(sample_run_record: RunRecord) -> None:
    db = MockDatabase()
    store = PostgresRunStore(db=db)  # type: ignore[arg-type]

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    db.session_mock.execute.return_value = mock_result

    await store.claim_run(sample_run_record)
    db.session_mock.add.assert_called_once()
    db.session_mock.commit.assert_awaited_once()

    added_model = db.session_mock.add.call_args[0][0]
    assert isinstance(added_model, RunModel)
    assert added_model.run_id == "run_001"


@pytest.mark.asyncio
async def test_postgres_run_store_claim_run_conflict(sample_run_record: RunRecord) -> None:
    db = MockDatabase()
    store = PostgresRunStore(db=db)  # type: ignore[arg-type]

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = MagicMock()  # an active run exists
    db.session_mock.execute.return_value = mock_result

    with pytest.raises(StoreError, match="Another active run already exists"):
        await store.claim_run(sample_run_record)
    db.session_mock.add.assert_not_called()
    db.session_mock.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_postgres_run_store_claim_run_integrity_error(sample_run_record: RunRecord) -> None:
    db = MockDatabase()
    store = PostgresRunStore(db=db)  # type: ignore[arg-type]

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    db.session_mock.execute.return_value = mock_result

    cause = IntegrityError("duplicate key", params=None, orig=Exception("unique constraint"))
    db.session_mock.commit.side_effect = StoreError("session failure")
    db.session_mock.commit.side_effect.__cause__ = cause

    with pytest.raises(StoreError, match="already exists; runs table is append-only"):
        await store.claim_run(sample_run_record)


def test_postgres_run_store_default_init() -> None:
    store = PostgresRunStore()
    assert store._db is not None


# --- PostgresPlaybookStore tests ---


@pytest.mark.asyncio
async def test_postgres_playbook_store_save_and_get(sample_plan: RemediationPlan) -> None:
    db = MockDatabase()
    clock = FrozenClock(FIXED_NOW)
    store = PostgresPlaybookStore(db=db, clock=clock)  # type: ignore[arg-type]

    # 1. save_playbook with Enum FailureClass
    await store.save_playbook(
        playbook_id="pb_001",
        failure_class=FailureClass.CONFIG_DRIFT,
        signature_text="query signature",
        embedding=[0.1] * 1024,
        plan=sample_plan,
        evidence_refs=["run_001"],
        origin="incident",
    )
    db.session_mock.execute.assert_awaited_once()
    db.session_mock.commit.assert_awaited_once()

    # 2. save_playbook with string failure_class (no .value)
    fc_str = "custom_failure"
    await store.save_playbook(
        playbook_id="pb_002",
        failure_class=fc_str,  # type: ignore[arg-type]
        signature_text="query signature 2",
        embedding=[0.2] * 1024,
        plan=sample_plan,
        evidence_refs=["run_002"],
        origin="shadow",
    )

    # 3. get_playbook - found
    mock_model = MagicMock()
    mock_model.plan = sample_plan.model_dump(mode="json")
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_model
    db.session_mock.execute.return_value = mock_result

    plan = await store.get_playbook("pb_001")
    assert plan == sample_plan

    # 4. get_playbook - not found
    mock_result.scalar_one_or_none.return_value = None
    plan_none = await store.get_playbook("pb_missing")
    assert plan_none is None


@pytest.mark.asyncio
async def test_postgres_playbook_store_search_and_counters(sample_plan: RemediationPlan) -> None:
    db = MockDatabase()
    store = PostgresPlaybookStore(db=db)  # type: ignore[arg-type]

    # search_playbooks
    mock_model = MagicMock()
    mock_model.plan = sample_plan.model_dump(mode="json")
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_model]
    db.session_mock.execute.return_value = mock_result

    res = await store.search_playbooks([0.1] * 1024, limit=3)
    assert res == [sample_plan]

    # search_playbooks_with_scores
    mock_model.playbook_id = "pb_001"
    mock_model.failure_class = "bad_deploy"
    mock_model.signature_text = "sig text"
    mock_model.evidence_refs = ["run_1"]
    mock_model.successes = 1
    mock_model.failures = 0
    mock_model.origin = "seed"
    mock_result.all.return_value = [(mock_model, 0.95)]
    db.session_mock.execute.return_value = mock_result

    scored_res = await store.search_playbooks_with_scores([0.1] * 1024, limit=3)
    assert len(scored_res) == 1
    assert scored_res[0].playbook_id == "pb_001"
    assert scored_res[0].similarity == 0.95
    assert scored_res[0].plan == sample_plan

    # list_playbooks
    mock_result.scalars.return_value.all.return_value = [mock_model]
    listed_res = await store.list_playbooks()
    assert len(listed_res) == 1
    assert listed_res[0].playbook_id == "pb_001"

    # get_playbook_by_signature - found
    mock_result.scalar_one_or_none.return_value = mock_model
    sig_res = await store.get_playbook_by_signature("sig text")
    assert sig_res is not None
    assert sig_res.playbook_id == "pb_001"

    # get_playbook_by_signature - not found
    mock_result.scalar_one_or_none.return_value = None
    sig_none = await store.get_playbook_by_signature("unknown")
    assert sig_none is None

    # increment_success without and with evidence_run_id
    await store.increment_success("pb_001")
    await store.increment_success("pb_001", evidence_run_id="run_100")
    assert db.session_mock.commit.await_count >= 2

    # increment_failure without and with evidence_run_id
    await store.increment_failure("pb_001")
    await store.increment_failure("pb_001", evidence_run_id="run_fail")
    assert db.session_mock.commit.await_count >= 4


def test_postgres_playbook_store_default_init() -> None:
    store = PostgresPlaybookStore()
    assert store._db is not None
    assert store._clock is not None


# --- PostgresEvalStore tests ---


@pytest.mark.asyncio
async def test_postgres_eval_store() -> None:
    db = MockDatabase()
    clock = FrozenClock(FIXED_NOW)
    store = PostgresEvalStore(db=db, clock=clock)  # type: ignore[arg-type]

    # record_scenario_result
    await store.record_scenario_result(
        scenario_id="sc_001",
        run_id="run_001",
        repeat_index=0,
        twin_predicted_success=True,
        prod_actual_success=True,
        expected_escalation=False,
        did_escalate=False,
        runner_up_plan_id="plan_002",
        runner_up_prod_success=False,
    )
    db.session_mock.add.assert_called_once()
    db.session_mock.commit.assert_awaited_once()
    added_model = db.session_mock.add.call_args[0][0]
    assert isinstance(added_model, ScenarioResultModel)
    assert added_model.scenario_id == "sc_001"
    assert added_model.runner_up_plan_id == "plan_002"

    # get_scenario_results - with and without scenario_id
    mock_row = MagicMock()
    mock_row.scenario_id = "sc_001"
    mock_row.run_id = "run_001"
    mock_row.repeat_index = 0
    mock_row.twin_predicted_success = True
    mock_row.prod_actual_success = True
    mock_row.expected_escalation = False
    mock_row.did_escalate = False
    mock_row.runner_up_plan_id = "plan_002"
    mock_row.runner_up_prod_success = False

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_row]
    db.session_mock.execute.return_value = mock_result

    # 1. No filter
    results = await store.get_scenario_results()
    assert len(results) == 1
    assert results[0]["scenario_id"] == "sc_001"
    assert results[0]["twin_predicted_success"] is True

    # 2. Filtered
    results_filtered = await store.get_scenario_results(scenario_id="sc_001")
    assert len(results_filtered) == 1
    assert results_filtered[0]["runner_up_plan_id"] == "plan_002"


def test_postgres_eval_store_default_init() -> None:
    store = PostgresEvalStore()
    assert store._db is not None
    assert store._clock is not None
