"""Unit tests for Understudy CLI entrypoint and package metadata."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

if TYPE_CHECKING:
    import pytest

import understudy
from understudy.cli import app

runner = CliRunner()


def test_package_version() -> None:
    """Ensure __version__ is defined."""
    assert understudy.__version__ == "0.1.0"


def test_cli_version() -> None:
    """Ensure ust version outputs expected version string."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "understudy 0.1.0" in result.stdout


def test_cli_help() -> None:
    """Ensure ust --help works and describes the tool."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Understudy" in result.stdout


def test_cli_doctor(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Ensure ust doctor runs and exits with status from run_doctor."""
    import understudy.doctor

    monkeypatch.setattr(understudy.doctor, "run_doctor", lambda: 0)
    res_ok = runner.invoke(app, ["doctor"])
    assert res_ok.exit_code == 0

    monkeypatch.setattr(understudy.doctor, "run_doctor", lambda: 1)
    res_fail = runner.invoke(app, ["doctor"])
    assert res_fail.exit_code == 1


def test_cli_demo_standard() -> None:
    """Verify ust demo --fake --seed 42 executes cleanly and outputs expected lines."""
    result = runner.invoke(app, ["demo", "--fake", "--seed", "42"])
    assert result.exit_code == 0
    expected_transitions = (
        "ingest -> gather_context -> plan_candidates -> fork_fleet -> "
        "register_mirrors -> apply_candidates -> observe -> tournament -> "
        "safety_kernel -> actuate -> notify_slack -> teardown_fleet -> record_run"
    )
    assert expected_transitions in result.stdout
    assert "outcome=executed plan=plan_cand_0" in result.stdout


def test_cli_demo_force_veto() -> None:
    """Verify ust demo --fake --seed 42 --force-veto follows escalation branch."""
    result = runner.invoke(app, ["demo", "--fake", "--seed", "42", "--force-veto"])
    assert result.exit_code == 0
    expected_transitions = (
        "ingest -> gather_context -> plan_candidates -> fork_fleet -> "
        "register_mirrors -> apply_candidates -> observe -> tournament -> "
        "safety_kernel -> escalate_pagerduty -> teardown_fleet -> record_run"
    )
    assert expected_transitions in result.stdout
    expected_reason = "outcome=escalated reason=K3 Rollback traverses schema migration boundary"
    assert expected_reason in result.stdout


def test_cli_demo_missing_fake() -> None:
    """Verify ust demo without --fake errors with code 1."""
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 1
    assert "--fake flag is required" in (result.stderr or result.output)


def test_cli_demo_fallback_branches(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify demo outcome formatting for edge cases (custom veto, no verdict, other)."""
    import asyncio

    import understudy.orchestrator.api
    from understudy.contracts.enums import KernelVerdictType, RunOutcome
    from understudy.contracts.kernel import KernelVerdict

    _, base_record = asyncio.run(understudy.orchestrator.api.run_demo(seed=42))

    # Edge case 1: escalated with verdict having no unsatisfied results but human_reason
    verdict_human = KernelVerdict(
        incident_id="inc_1",
        plan_id="p1",
        verdict=KernelVerdictType.VETO,
        results=[],
        missing_facts=[],
        solver_ms=5.0,
        human_reason="VETO: Custom manual veto reason",
    )
    rec1 = (
        base_record.model_copy(update={"outcome": RunOutcome.ESCALATED, "verdict": verdict_human})
        if base_record
        else None
    )

    async def fake_demo_human(**_kwargs: object) -> tuple[list[str], object]:
        return (["ingest", "record_run"], rec1)

    monkeypatch.setattr(understudy.orchestrator.api, "run_demo", fake_demo_human)
    res1 = runner.invoke(app, ["demo", "--fake"])
    assert res1.exit_code == 0
    assert "outcome=escalated reason=VETO: Custom manual veto reason" in res1.stdout

    # Edge case 2: escalated with no verdict at all
    rec2 = (
        base_record.model_copy(update={"outcome": RunOutcome.ESCALATED, "verdict": None})
        if base_record
        else None
    )

    async def fake_demo_no_verdict(**_kwargs: object) -> tuple[list[str], object]:
        return (["ingest", "record_run"], rec2)

    monkeypatch.setattr(understudy.orchestrator.api, "run_demo", fake_demo_no_verdict)
    res2 = runner.invoke(app, ["demo", "--fake"])
    assert res2.exit_code == 0
    assert "outcome=escalated reason=K3" in res2.stdout

    # Edge case 3: non-executed, non-escalated outcome (e.g. FAILED)
    rec3 = base_record.model_copy(update={"outcome": RunOutcome.FAILED}) if base_record else None

    async def fake_demo_failed(**_kwargs: object) -> tuple[list[str], object]:
        return (["ingest", "record_run"], rec3)

    monkeypatch.setattr(understudy.orchestrator.api, "run_demo", fake_demo_failed)
    res3 = runner.invoke(app, ["demo", "--fake"])
    assert res3.exit_code == 0
    assert "outcome=failed" in res3.stdout


def test_cli_graph_mermaid() -> None:
    """Verify ust graph prints Mermaid diagram string to stdout."""
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "graph TD;" in result.stdout
    assert "ingest" in result.stdout
    assert "record_run" in result.stdout


def test_cli_graph_render(monkeypatch: "pytest.MonkeyPatch", tmp_path: "Path") -> None:
    """Verify ust graph --render saves PNG to target file without external network I/O."""
    fake_png = b"\x89PNG\r\n\x1a\ncli-png"
    monkeypatch.setattr(
        "langchain_core.runnables.graph.Graph.draw_mermaid_png",
        lambda *_a, **_kw: fake_png,
    )

    out_file = Path(str(tmp_path)) / "custom_dir" / "graph.png"
    result = runner.invoke(app, ["graph", "--render", str(out_file)])
    assert result.exit_code == 0
    assert "Rendered control loop graph to" in result.stdout
    assert out_file.is_file()
    assert out_file.read_bytes() == fake_png


def test_cli_fleet_fork(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust fleet fork command outputs twin summaries."""
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock

    import understudy.fleet.controller
    from understudy.contracts.twin import TwinHandle

    mock_twins = [
        TwinHandle(
            twin_id="twin_inc_test_0",
            incident_id="inc_test",
            candidate_index=0,
            namespace="ust-twin-inc_test-0",
            database="twin_inc_test_0",
            forked_from_snapshot_at=datetime.now(),
            ready_at=datetime.now(),
            state="ready",
        )
    ]
    mock_ctrl = MagicMock()
    mock_ctrl.fork = AsyncMock(return_value=mock_twins)

    monkeypatch.setattr(understudy.fleet.controller, "K8sFleetController", lambda **_kw: mock_ctrl)

    result = runner.invoke(app, ["fleet", "fork", "--incident", "inc_test", "--count", "1"])
    assert result.exit_code == 0
    assert "twin_id=twin_inc_test_0" in result.stdout
    assert "namespace=ust-twin-inc_test-0" in result.stdout
    assert "state=ready" in result.stdout
    mock_ctrl.fork.assert_called_once_with(incident_id="inc_test", n=1)


def test_cli_fleet_teardown(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust fleet teardown command calls teardown_all."""
    from unittest.mock import AsyncMock, MagicMock

    import understudy.fleet.controller

    mock_ctrl = MagicMock()
    mock_ctrl.teardown_all = AsyncMock()

    monkeypatch.setattr(understudy.fleet.controller, "K8sFleetController", lambda **_kw: mock_ctrl)

    result = runner.invoke(app, ["fleet", "teardown", "--incident", "inc_test"])
    assert result.exit_code == 0
    assert "fleet torn down for incident=inc_test" in result.stdout
    mock_ctrl.teardown_all.assert_called_once_with(incident_id="inc_test")


def test_cli_fleet_gc(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust fleet gc command calls FleetTeardownManager.gc."""
    from unittest.mock import AsyncMock, MagicMock

    import understudy.fleet.teardown
    from understudy.fleet.teardown import GcResult

    mock_mgr = MagicMock()
    mock_mgr.gc = AsyncMock(
        return_value=GcResult(
            reaped_namespaces=["ust-twin-old-0"],
            dropped_databases=["twin_old_0"],
            scanned_namespaces=5,
            scanned_databases=5,
        )
    )

    monkeypatch.setattr(understudy.fleet.teardown, "FleetTeardownManager", lambda **_kw: mock_mgr)

    result = runner.invoke(app, ["fleet", "gc", "--older-than", "1800"])
    assert result.exit_code == 0
    assert "reaped 1 namespaces, 1 databases" in result.stdout
    mock_mgr.gc.assert_called_once_with(older_than_seconds=1800.0)


def test_cli_store_migrate(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust store migrate calls apply_migrations."""
    from unittest.mock import MagicMock

    import understudy.store.migrations

    mock_apply = MagicMock()
    monkeypatch.setattr(understudy.store.migrations, "apply_migrations", mock_apply)

    result = runner.invoke(app, ["store", "migrate", "--dsn", "postgresql://test/db"])
    assert result.exit_code == 0
    assert "Database migrations applied successfully." in result.stdout
    mock_apply.assert_called_once_with(dsn="postgresql://test/db")


def test_cli_store_verify_success(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust store verify checks runs and rules."""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock

    from understudy.contracts.enums import RunOutcome

    mock_run = MagicMock()
    mock_run.run_id = "run_test_1"
    mock_run.incident_id = "inc_test_1"
    mock_run.outcome = RunOutcome.EXECUTED

    mock_store = MagicMock()
    mock_store.list_runs = AsyncMock(return_value=[mock_run])

    mock_session = AsyncMock()
    rule_res = MagicMock()
    rule_res.fetchall.return_value = [("runs_no_update",), ("runs_no_delete",)]
    mock_session.execute = AsyncMock(return_value=rule_res)

    class MockDb:
        def __init__(self, **_kw: Any) -> None:
            pass

        @asynccontextmanager
        async def session(self) -> Any:
            yield mock_session

    import understudy.store.database
    import understudy.store.postgres

    monkeypatch.setattr(understudy.store.database, "StoreDatabase", MockDb)
    monkeypatch.setattr(understudy.store.postgres, "PostgresRunStore", lambda **_kw: mock_store)

    result = runner.invoke(app, ["store", "verify", "--last", "1"])
    assert result.exit_code == 0
    assert "run_id=run_test_1" in result.stdout
    assert "Verified 1 run records: complete and append-only OK" in result.stdout


def test_cli_store_verify_missing_rules(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust store verify fails when append-only rules are missing."""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock

    mock_store = MagicMock()
    mock_store.list_runs = AsyncMock(return_value=[])

    mock_session = AsyncMock()
    rule_res = MagicMock()
    rule_res.fetchall.return_value = []  # No rules
    mock_session.execute = AsyncMock(return_value=rule_res)

    class MockDb:
        def __init__(self, **_kw: Any) -> None:
            pass

        @asynccontextmanager
        async def session(self) -> Any:
            yield mock_session

    import understudy.store.database
    import understudy.store.postgres

    monkeypatch.setattr(understudy.store.database, "StoreDatabase", MockDb)
    monkeypatch.setattr(understudy.store.postgres, "PostgresRunStore", lambda **_kw: mock_store)

    result = runner.invoke(app, ["store", "verify", "--last", "2"])
    assert result.exit_code == 1
    assert "Error: append-only rules missing on runs table" in result.output
