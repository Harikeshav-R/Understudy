"""Unit tests for SQLAlchemy store models in understudy.store.models."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from understudy.store.models import (
    PlaybookModel,
    RunModel,
    ScenarioResultModel,
    _create_runs_append_only_rules,
)


def test_run_model_definition() -> None:
    """Verify RunModel table configuration, columns, and index properties."""
    assert RunModel.__tablename__ == "runs"
    assert "run_id" in RunModel.__table__.columns
    assert "incident_id" in RunModel.__table__.columns
    assert "payload" in RunModel.__table__.columns

    # Test append-only rules event callback
    mock_conn = MagicMock()
    _create_runs_append_only_rules(target=None, connection=mock_conn)
    mock_conn.execute.assert_called_once()
    sql_text = str(mock_conn.execute.call_args[0][0])
    assert "runs_no_update" in sql_text
    assert "runs_no_delete" in sql_text


def test_playbook_model_definition() -> None:
    """Verify PlaybookModel table configuration and columns."""
    assert PlaybookModel.__tablename__ == "playbooks"
    assert "playbook_id" in PlaybookModel.__table__.columns
    assert "embedding" in PlaybookModel.__table__.columns
    assert "successes" in PlaybookModel.__table__.columns
    assert "failures" in PlaybookModel.__table__.columns

    # Check table indexes
    from sqlalchemy import Table

    table = PlaybookModel.__table__
    assert isinstance(table, Table)
    index_names = {idx.name for idx in table.indexes}
    assert "ix_playbooks_embedding_hnsw" in index_names


def test_scenario_result_model_definition() -> None:
    """Verify ScenarioResultModel table configuration and foreign key."""
    assert ScenarioResultModel.__tablename__ == "scenario_results"
    assert "scenario_id" in ScenarioResultModel.__table__.columns
    assert "run_id" in ScenarioResultModel.__table__.columns
    fk_targets = [fk.target_fullname for fk in ScenarioResultModel.__table__.foreign_keys]
    assert "runs.run_id" in fk_targets


def test_model_instantiation() -> None:
    """Verify model instances can be created with valid attributes."""
    now = datetime(2026, 9, 13, 0, 0, 0, tzinfo=UTC)
    run = RunModel(
        run_id="run_1",
        incident_id="inc_1",
        scenario_id="sc_1",
        started_at=now,
        finished_at=now,
        outcome="executed",
        prod_applied_plan="plan_1",
        prod_outcome="resolved",
        escalation_reason=None,
        payload={"run_id": "run_1"},
    )
    assert run.run_id == "run_1"
    assert run.outcome == "executed"
