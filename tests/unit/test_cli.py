"""Unit tests for Understudy CLI entrypoint and package metadata."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

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


def test_cli_demo_with_datadog() -> None:
    """Verify ust demo --fake --with-datadog informs user of Datadog optics."""
    result = runner.invoke(app, ["demo", "--fake", "--with-datadog"])
    assert result.exit_code == 0
    assert "Datadog telemetry mirroring enabled" in result.stdout


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


def test_cli_tunnel_fake(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust tunnel --fake starts fake session and outputs tunnel URLs."""
    from unittest.mock import AsyncMock

    import understudy.signals.tunnel

    mock_run = AsyncMock(return_value=None)
    monkeypatch.setattr(understudy.signals.tunnel.TunnelSession, "run_until_cancelled", mock_run)

    result = runner.invoke(app, ["tunnel", "--fake", "--port", "19308", "--secret", "test-secret"])
    assert result.exit_code == 0
    assert "Tunnel URL: https://fake-tunnel-19308.understudy.dev" in result.stdout
    assert "Webhook URL: https://fake-tunnel-19308.understudy.dev/webhook" in result.stdout


def test_cli_tunnel_missing_secret() -> None:
    """Verify ust tunnel fails fast when no webhook secret is configured."""
    result = runner.invoke(app, ["tunnel", "--fake", "--port", "19320"])
    assert result.exit_code == 1
    assert "Error starting webhook receiver:" in (result.stderr or result.stdout)


def test_cli_tunnel_start_failure(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust tunnel reports a clean error when the webhook server fails to start."""
    import understudy.signals.tunnel

    async def _failing_start(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("Webhook server failed to start: address already in use")

    monkeypatch.setattr(understudy.signals.tunnel.TunnelSession, "start", _failing_start)

    result = runner.invoke(app, ["tunnel", "--fake", "--port", "19321", "--secret", "test-secret"])
    assert result.exit_code == 1
    assert "Error starting webhook receiver:" in (result.stderr or result.stdout)


def test_cli_tunnel_keyboard_interrupt(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust tunnel handles keyboard interrupt cleanly."""

    import understudy.signals.tunnel

    async def _interrupt(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(understudy.signals.tunnel.TunnelSession, "run_until_cancelled", _interrupt)

    result = runner.invoke(app, ["tunnel", "--fake", "--port", "19309", "--secret", "test-secret"])
    assert result.exit_code == 0


def test_cli_alert_inject_print_only() -> None:
    """Verify ust alert inject --print-only outputs valid Alert JSON."""
    result = runner.invoke(
        app, ["alert", "inject", "--scenario", "bad_deploy_data_service", "--print-only"]
    )
    assert result.exit_code == 0
    assert '"scenario_id": "bad_deploy_data_service"' in result.stdout
    assert '"source": "synthetic"' in result.stdout


def test_cli_alert_inject_success(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust alert inject posts to receiver endpoint and echoes confirmation."""
    import httpx

    captured_url: list[str] = []

    def _mock_post(url: str, **_kwargs: Any) -> httpx.Response:
        captured_url.append(url)
        return httpx.Response(200, json={"status": "injected"})

    monkeypatch.setattr(httpx, "post", _mock_post)

    result = runner.invoke(
        app,
        [
            "alert",
            "inject",
            "--scenario",
            "bad_deploy_data_service",
            "--service",
            "edge-gateway",
            "--title",
            "Custom Title",
            "--severity",
            "error",
            "--port",
            "19310",
        ],
    )
    assert result.exit_code == 0
    assert "injected synthetic alert" in result.stdout
    assert captured_url[0] == "http://127.0.0.1:19310/api/alerts/inject"


def test_cli_alert_inject_severity_warning(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust alert inject with warning severity."""
    import httpx

    def _mock_post(_url: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={"status": "injected"})

    monkeypatch.setattr(httpx, "post", _mock_post)

    result = runner.invoke(
        app,
        [
            "alert",
            "inject",
            "--scenario",
            "flag_plus_latency",
            "--severity",
            "warning",
        ],
    )
    assert result.exit_code == 0
    assert "injected synthetic alert" in result.stdout


def test_cli_alert_inject_receiver_error(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust alert inject exits with code 1 when receiver returns error HTTP status."""
    import httpx

    def _mock_post(*_args: Any, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    monkeypatch.setattr(httpx, "post", _mock_post)

    result = runner.invoke(
        app,
        ["alert", "inject", "--scenario", "bad_deploy_data_service"],
    )
    assert result.exit_code == 1
    assert "Receiver returned HTTP 500" in result.output


def test_cli_alert_inject_unreachable(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust alert inject prints warning when receiver endpoint is unreachable."""
    import httpx

    def _mock_post(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx, "post", _mock_post)

    result = runner.invoke(
        app,
        ["alert", "inject", "--scenario", "bad_deploy_data_service"],
    )
    assert result.exit_code == 0
    assert "not reachable" in result.output
    assert "Synthetic Alert generated" in result.output


def test_cli_alert_help() -> None:
    """Verify ust alert --help renders subcommand documentation."""
    result = runner.invoke(app, ["alert", "--help"])
    assert result.exit_code == 0
    assert "inject" in result.stdout


def test_cli_signals_help() -> None:
    """Verify ust signals --help renders subcommand documentation."""
    result = runner.invoke(app, ["signals", "--help"])
    assert result.exit_code == 0
    assert "context" in result.stdout


def test_cli_signals_context_json(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust signals context outputs valid JSON IncidentContext."""
    from datetime import UTC, datetime

    from understudy.contracts.incident import (
        ErrorSignature,
        MetricPoint,
        MetricSeries,
        MetricWindow,
    )
    from understudy.signals.prometheus import PrometheusLokiAdapter

    fixed_now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

    async def _mock_metric_window(*_args: Any, **_kwargs: Any) -> MetricWindow:
        return MetricWindow(
            service="data-service",
            start_time=fixed_now,
            end_time=fixed_now,
            series=[
                MetricSeries(
                    metric_name="http_requests_total",
                    labels={"service": "data-service"},
                    points=[MetricPoint(timestamp=fixed_now, value=100.0)],
                )
            ],
            p99_latency_ms=12.5,
            error_rate=0.02,
            request_count=100,
        )

    async def _mock_error_signatures(*_args: Any, **_kwargs: Any) -> list[ErrorSignature]:
        return [
            ErrorSignature(
                fingerprint="fp123456",
                message="Mock database error",
                service="data-service",
                count=3,
                first_seen=fixed_now,
                last_seen=fixed_now,
            )
        ]

    async def _mock_close(*_args: Any, **_kwargs: Any) -> None:
        pass

    monkeypatch.setattr(PrometheusLokiAdapter, "metric_window", _mock_metric_window)
    monkeypatch.setattr(PrometheusLokiAdapter, "error_signatures", _mock_error_signatures)
    monkeypatch.setattr(PrometheusLokiAdapter, "close", _mock_close)

    result = runner.invoke(
        app,
        [
            "signals",
            "context",
            "--service",
            "data-service",
            "--minutes",
            "10",
            "--namespace",
            "ust-prod",
            "--incident-id",
            "inc_test_123",
        ],
    )
    assert result.exit_code == 0
    assert '"incident_id": "inc_test_123"' in result.stdout
    assert '"service": "data-service"' in result.stdout
    assert '"fp123456"' in result.stdout


def test_cli_signals_context_no_json(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust signals context --no-json prints human summary."""
    from datetime import UTC, datetime

    from understudy.contracts.incident import MetricWindow
    from understudy.signals.prometheus import PrometheusLokiAdapter

    fixed_now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

    async def _mock_metric_window(*_args: Any, **_kwargs: Any) -> MetricWindow:
        return MetricWindow(
            service="auth-service",
            start_time=fixed_now,
            end_time=fixed_now,
            series=[],
            p99_latency_ms=5.0,
            error_rate=0.0,
            request_count=50,
        )

    async def _mock_error_signatures(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def _mock_close(*_args: Any, **_kwargs: Any) -> None:
        pass

    monkeypatch.setattr(PrometheusLokiAdapter, "metric_window", _mock_metric_window)
    monkeypatch.setattr(PrometheusLokiAdapter, "error_signatures", _mock_error_signatures)
    monkeypatch.setattr(PrometheusLokiAdapter, "close", _mock_close)

    result = runner.invoke(
        app,
        [
            "signals",
            "context",
            "--service",
            "auth-service",
            "--no-json",
            "--prometheus-url",
            "http://custom-prom:9090",
            "--loki-url",
            "http://custom-loki:3100",
        ],
    )
    assert result.exit_code == 0
    assert "IncidentContext gathered for auth-service (ust-prod):" in result.stdout
    assert "requests=50" in result.stdout
    assert "p99=5.0ms" in result.stdout


def test_cli_signals_context_datadog(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust signals context --adapter datadog uses DatadogAdapter."""
    from datetime import UTC, datetime

    from understudy.contracts.incident import (
        ErrorSignature,
        MetricPoint,
        MetricSeries,
        MetricWindow,
    )
    from understudy.signals.datadog import DatadogAdapter

    fixed_now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

    async def _mock_metric_window(*_args: Any, **_kwargs: Any) -> MetricWindow:
        return MetricWindow(
            service="data-service",
            start_time=fixed_now,
            end_time=fixed_now,
            series=[
                MetricSeries(
                    metric_name="http_requests_total",
                    labels={"service": "data-service"},
                    points=[MetricPoint(timestamp=fixed_now, value=80.0)],
                )
            ],
            p99_latency_ms=22.0,
            error_rate=0.01,
            request_count=80,
        )

    async def _mock_error_signatures(*_args: Any, **_kwargs: Any) -> list[ErrorSignature]:
        return [
            ErrorSignature(
                fingerprint="fp_dd_123",
                message="Datadog aggregated error",
                service="data-service",
                count=2,
                first_seen=fixed_now,
                last_seen=fixed_now,
            )
        ]

    async def _mock_close(*_args: Any, **_kwargs: Any) -> None:
        pass

    monkeypatch.setattr(DatadogAdapter, "metric_window", _mock_metric_window)
    monkeypatch.setattr(DatadogAdapter, "error_signatures", _mock_error_signatures)
    monkeypatch.setattr(DatadogAdapter, "close", _mock_close)

    result = runner.invoke(
        app,
        [
            "signals",
            "context",
            "--adapter",
            "datadog",
            "--datadog-site",
            "datadoghq.eu",
            "--service",
            "data-service",
            "--minutes",
            "10",
        ],
    )
    assert result.exit_code == 0
    assert '"service": "data-service"' in result.stdout
    assert '"fp_dd_123"' in result.stdout
    assert '"request_count": 80' in result.stdout


def test_cli_signals_context_fallback_graph(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust signals context falls back to single-node snapshot if dependencies.yaml fails."""
    from datetime import UTC, datetime

    from understudy.contracts.incident import MetricWindow
    from understudy.graph.service_graph import ServiceDependencyGraph
    from understudy.signals.prometheus import PrometheusLokiAdapter

    fixed_now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

    def _failing_from_yaml(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("Missing yaml")

    async def _mock_metric_window(*_args: Any, **_kwargs: Any) -> MetricWindow:
        return MetricWindow(
            service="data-service",
            start_time=fixed_now,
            end_time=fixed_now,
            series=[],
            p99_latency_ms=5.0,
            error_rate=0.0,
            request_count=50,
        )

    async def _mock_error_signatures(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def _mock_close(*_args: Any, **_kwargs: Any) -> None:
        pass

    monkeypatch.setattr(ServiceDependencyGraph, "from_yaml", _failing_from_yaml)
    monkeypatch.setattr(PrometheusLokiAdapter, "metric_window", _mock_metric_window)
    monkeypatch.setattr(PrometheusLokiAdapter, "error_signatures", _mock_error_signatures)
    monkeypatch.setattr(PrometheusLokiAdapter, "close", _mock_close)

    result = runner.invoke(app, ["signals", "context", "--service", "data-service"])
    assert result.exit_code == 0
    assert '"nodes": [' in result.stdout
    assert '"data-service"' in result.stdout


def test_cli_signals_deploys_human_and_json(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust signals deploys outputs human summary and json list."""
    from datetime import UTC, datetime

    from understudy.contracts.incident import DeployRef
    from understudy.signals.github import GitHubDeployHistory

    fixed_now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

    mock_deploys = [
        DeployRef(
            commit_sha="c0ffee1111111111111111111111111111111111",
            image_digests={"data-service": "sha256:1111", "auth-service": "sha256:2222"},
            deployed_at=fixed_now,
            pr_number=42,
            contains_migration=True,
        ),
        DeployRef(
            commit_sha="c0ffee2222222222222222222222222222222222",
            image_digests={},
            deployed_at=fixed_now,
            pr_number=None,
            contains_migration=False,
        ),
    ]

    async def _mock_recent_deploys(*_args: Any, **_kwargs: Any) -> list[DeployRef]:
        return mock_deploys

    async def _mock_close(*_args: Any, **_kwargs: Any) -> None:
        pass

    monkeypatch.setattr(GitHubDeployHistory, "recent_deploys", _mock_recent_deploys)
    monkeypatch.setattr(GitHubDeployHistory, "close", _mock_close)

    # 1. Human table output
    res_human = runner.invoke(app, ["signals", "deploys", "--limit", "2"])
    assert res_human.exit_code == 0
    assert "Recent 2 deployment(s)" in res_human.stdout
    assert "c0ffee1 | 2026-09-13 12:00:00 UTC | PR: #42 | migration: YES" in res_human.stdout
    assert "data-service: sha256:1111" in res_human.stdout
    assert "auth-service: sha256:2222" in res_human.stdout
    assert "c0ffee2 | 2026-09-13 12:00:00 UTC | PR: - | migration: NO" in res_human.stdout

    # 2. JSON list output with flags
    res_json = runner.invoke(
        app,
        [
            "signals",
            "deploys",
            "--limit",
            "2",
            "--json",
            "--repo",
            "custom/repo",
            "--branch",
            "dev",
            "--token",
            "tok",
        ],
    )
    assert res_json.exit_code == 0
    assert '"commit_sha": "c0ffee1111111111111111111111111111111111"' in res_json.stdout
    assert '"pr_number": 42' in res_json.stdout
    assert '"contains_migration": true' in res_json.stdout


def test_cli_signals_deploys_error(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust signals deploys exits 1 when fetch fails."""
    from understudy.common.errors import GitHubError
    from understudy.signals.github import GitHubDeployHistory

    async def _failing_recent_deploys(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise GitHubError("Bad credentials")

    async def _mock_close(*_args: Any, **_kwargs: Any) -> None:
        pass

    monkeypatch.setattr(GitHubDeployHistory, "recent_deploys", _failing_recent_deploys)
    monkeypatch.setattr(GitHubDeployHistory, "close", _mock_close)

    res = runner.invoke(app, ["signals", "deploys"])
    assert res.exit_code == 1
    assert "Error fetching deploy history: Bad credentials" in (res.stderr or res.stdout)


def test_cli_graph_show_human(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust graph show outputs DAG chains and OK status."""
    from understudy.graph.models import CrossCheckReport
    from understudy.graph.service_graph import ServiceDependencyGraph

    mock_report = CrossCheckReport(
        status="OK",
        declared_services={"edge-gateway", "auth-service", "data-service", "worker"},
        observed_services={"edge-gateway", "auth-service", "data-service", "worker"},
        declared_edges={("edge-gateway", "auth-service")},
        observed_edges={("edge-gateway", "auth-service")},
        undeclared_services=set(),
        undeclared_edges=set(),
        message="declared graph matches observed traffic: OK",
    )

    async def _mock_cross_check(*_args: Any, **_kwargs: Any) -> CrossCheckReport:
        return mock_report

    monkeypatch.setattr(ServiceDependencyGraph, "cross_check_prometheus", _mock_cross_check)

    result = runner.invoke(app, ["graph", "show"])
    assert result.exit_code == 0
    assert (
        "edge-gateway -> auth-service -> data-service, "
        "edge-gateway -> data-service, "
        "worker -> data-service"
    ) in result.stdout
    assert "declared graph matches observed traffic: OK" in result.stdout
    assert "declared matches observed: OK" in result.stdout


def test_cli_graph_show_json(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust graph show --json outputs valid JSON representation."""
    from understudy.graph.models import CrossCheckReport
    from understudy.graph.service_graph import ServiceDependencyGraph

    mock_report = CrossCheckReport(
        status="OK",
        declared_services={"edge-gateway", "auth-service", "data-service", "worker"},
        observed_services={"edge-gateway", "auth-service", "data-service", "worker"},
        declared_edges={("edge-gateway", "auth-service")},
        observed_edges={("edge-gateway", "auth-service")},
        undeclared_services=set(),
        undeclared_edges=set(),
        message="declared graph matches observed traffic: OK",
    )

    async def _mock_cross_check(*_args: Any, **_kwargs: Any) -> CrossCheckReport:
        return mock_report

    monkeypatch.setattr(ServiceDependencyGraph, "cross_check_prometheus", _mock_cross_check)

    result = runner.invoke(app, ["graph", "show", "--json"])
    assert result.exit_code == 0
    assert '"status": "OK"' in result.stdout
    assert '"chains":' in result.stdout
    assert '"edge-gateway"' in result.stdout


def test_cli_graph_show_offline() -> None:
    """Verify ust graph show --offline skips Prometheus cross-check."""
    result = runner.invoke(app, ["graph", "show", "--offline"])
    assert result.exit_code == 0
    assert (
        "edge-gateway -> auth-service -> data-service, "
        "edge-gateway -> data-service, "
        "worker -> data-service"
    ) in result.stdout
    assert "declared graph traffic check: SKIPPED (offline)" in result.stdout


def test_cli_graph_show_missing_file(tmp_path: "Path") -> None:
    """Verify ust graph show fails gracefully on nonexistent dependencies file."""
    nonexistent = tmp_path / "missing.yaml"
    result = runner.invoke(app, ["graph", "show", "--dependencies-file", str(nonexistent)])
    assert result.exit_code == 1
    assert "Error loading dependency graph:" in (result.stderr or result.stdout)


def test_cli_graph_show_mismatch(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust graph show exits 1 when traffic mismatch is detected."""
    from understudy.graph.models import CrossCheckReport
    from understudy.graph.service_graph import ServiceDependencyGraph

    mock_report = CrossCheckReport(
        status="MISMATCH",
        declared_services={"edge-gateway", "auth-service", "data-service", "worker"},
        observed_services={"rogue-svc"},
        declared_edges=set(),
        observed_edges=set(),
        undeclared_services={"rogue-svc"},
        undeclared_edges=set(),
        message="Undeclared services observed in traffic: ['rogue-svc']",
    )

    async def _mock_cross_check(*_args: Any, **_kwargs: Any) -> CrossCheckReport:
        return mock_report

    monkeypatch.setattr(ServiceDependencyGraph, "cross_check_prometheus", _mock_cross_check)

    result = runner.invoke(app, ["graph", "show"])
    assert result.exit_code == 1
    assert "Undeclared services observed in traffic" in (result.stderr or result.stdout)


def test_cli_graph_show_prometheus_error(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust graph show exits 1 when Prometheus query throws exception."""
    from understudy.common.errors import GraphError
    from understudy.graph.service_graph import ServiceDependencyGraph

    async def _failing_cross_check(*_args: Any, **_kwargs: Any) -> Any:
        raise GraphError("Connection timed out")

    monkeypatch.setattr(ServiceDependencyGraph, "cross_check_prometheus", _failing_cross_check)

    result = runner.invoke(app, ["graph", "show"])
    assert result.exit_code == 1
    assert "Error cross-checking observed traffic: Connection timed out" in (
        result.stderr or result.stdout
    )


# --- ust plan CLI tests ---


def test_cli_plan_success_fake(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust plan produces plans with NO_ACTION in fake mode."""
    import understudy.graph.k8s

    monkeypatch.setattr(
        understudy.graph.k8s,
        "get_cluster_workloads",
        lambda **_kw: {"data-service", "edge-gateway"},
    )
    result = runner.invoke(
        app,
        ["plan", "--context", "fixtures/context_bad_deploy.json", "--count", "3", "--fake"],
    )
    assert result.exit_code == 0
    assert "Generated 4 candidate plan(s)" in result.stdout
    assert "rollback_deploy" in result.stdout
    assert "scale_workload" in result.stdout
    assert "restart_workload" in result.stdout
    assert "no_action" in result.stdout


def test_cli_plan_live_workloads_empty(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust plan falls back to context graph nodes when live workloads set is empty."""
    import understudy.graph.k8s

    monkeypatch.setattr(
        understudy.graph.k8s,
        "get_cluster_workloads",
        lambda **_kw: set(),
    )
    result = runner.invoke(
        app,
        ["plan", "--context", "fixtures/context_bad_deploy.json", "--count", "3", "--fake"],
    )
    assert result.exit_code == 0
    assert "Generated 4 candidate plan(s)" in result.stdout


def test_cli_plan_json_output() -> None:
    """Verify ust plan --json returns valid JSON array of RemediationPlan."""
    import json

    result = runner.invoke(
        app,
        [
            "plan",
            "--context",
            "fixtures/context_bad_deploy.json",
            "--count",
            "3",
            "--fake",
            "--json",
        ],
    )
    assert result.exit_code == 0
    json_start = result.stdout.find("[\n")
    assert json_start != -1
    plans = json.loads(result.stdout[json_start:])
    assert isinstance(plans, list)
    assert len(plans) == 4
    assert plans[3]["action"] == "no_action"
    assert plans[3]["inverse"] is None
    assert plans[3]["target_resources"] == []
    assert plans[3]["declared_blast_set"] == []


def test_cli_plan_twice_stability() -> None:
    """Verify ust plan --twice evaluates action-type stability."""
    result = runner.invoke(
        app,
        [
            "plan",
            "--context",
            "fixtures/context_bad_deploy.json",
            "--count",
            "3",
            "--fake",
            "--seed",
            "42",
            "--twice",
        ],
    )
    assert result.exit_code == 0
    assert "Planner run 1 (4 plans)" in result.stdout
    assert "Planner run 2 (4 plans)" in result.stdout
    assert "Action-type stability: 1.00" in result.stdout


def test_cli_plan_file_not_found() -> None:
    """Verify ust plan exits 1 when context file does not exist."""
    result = runner.invoke(
        app,
        ["plan", "--context", "fixtures/nonexistent_context.json"],
    )
    assert result.exit_code == 1
    assert "Context file not found" in (result.stderr or result.stdout)


def test_cli_plan_invalid_json(tmp_path: Path) -> None:
    """Verify ust plan exits 1 when context file has invalid JSON."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("invalid json content", encoding="utf-8")

    result = runner.invoke(
        app,
        ["plan", "--context", str(bad_file)],
    )
    assert result.exit_code == 1
    assert "Failed to parse IncidentContext" in (result.stderr or result.stdout)


def test_cli_plan_live_llm_mocked(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust plan non-fake path calls LLMPlanner."""
    from understudy.contracts.enums import ActionType
    from understudy.contracts.plan import ActionParams, RemediationPlan
    from understudy.planner.validate import LLMPlanner

    dummy_plan = RemediationPlan(
        plan_id="plan_cand_0",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="data-service"),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="No action",
        origin="planner",
    )

    async def _mock_generate(*_args: Any, **_kwargs: Any) -> list[RemediationPlan]:
        return [dummy_plan]

    monkeypatch.setattr(LLMPlanner, "generate_candidates", _mock_generate)

    result = runner.invoke(
        app,
        ["plan", "--context", "fixtures/context_bad_deploy.json", "--count", "1"],
    )
    assert result.exit_code == 0
    assert "Generated 1 candidate plan(s)" in result.stdout
    assert "plan_cand_0: no_action" in result.stdout


def test_cli_plan_live_llm_error(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust plan exits 1 when planner raises PlannerError."""
    from understudy.common.errors import PlannerError
    from understudy.planner.validate import LLMPlanner

    async def _mock_generate(*_args: Any, **_kwargs: Any) -> Any:
        raise PlannerError("OpenRouter rate limited")

    monkeypatch.setattr(LLMPlanner, "generate_candidates", _mock_generate)

    result = runner.invoke(
        app,
        ["plan", "--context", "fixtures/context_bad_deploy.json"],
    )
    assert result.exit_code == 1
    assert "Planner error: OpenRouter rate limited" in (result.stderr or result.stdout)


def test_cli_plan_twice_second_error(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust plan --twice exits 1 when second run fails."""
    from understudy.common.errors import PlannerError
    from understudy.contracts.enums import ActionType
    from understudy.contracts.plan import ActionParams, RemediationPlan
    from understudy.planner.validate import LLMPlanner

    dummy_plan = RemediationPlan(
        plan_id="plan_cand_0",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="data-service"),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale="No action",
        origin="planner",
    )

    calls = 0

    async def _mock_generate(*_args: Any, **_kwargs: Any) -> list[RemediationPlan]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return [dummy_plan]
        raise PlannerError("Second run crashed")

    monkeypatch.setattr(LLMPlanner, "generate_candidates", _mock_generate)

    result = runner.invoke(
        app,
        ["plan", "--context", "fixtures/context_bad_deploy.json", "--twice"],
    )
    assert result.exit_code == 1
    assert "Second planner run failed: Second run crashed" in (result.stderr or result.stdout)


def test_cli_mirror_help() -> None:
    """Verify ust mirror --help lists register, stats, unregister, compare."""
    result = runner.invoke(app, ["mirror", "--help"])
    assert result.exit_code == 0
    assert "register" in result.stdout
    assert "stats" in result.stdout
    assert "unregister" in result.stdout
    assert "compare" in result.stdout


def test_cli_mirror_register_success(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust mirror register registers synthesized twin handles successfully."""
    from understudy.mirror.registry import HttpMirrorRegistry

    registered_twins: list[str] = []

    async def _mock_register_twin(
        _self: Any, twin_handle: Any, _base_url: str | None = None
    ) -> None:
        registered_twins.append(twin_handle.twin_id)

    monkeypatch.setattr(HttpMirrorRegistry, "register_twin", _mock_register_twin)

    result = runner.invoke(app, ["mirror", "register", "--incident", "inc_test", "--count", "2"])
    assert result.exit_code == 0
    assert "registered twin_id=twin_inc_test_0" in result.stdout
    assert "registered twin_id=twin_inc_test_1" in result.stdout
    assert len(registered_twins) == 2


def test_cli_mirror_register_failure(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust mirror register exits 1 on failure."""
    from understudy.common.errors import MirrorError
    from understudy.mirror.registry import HttpMirrorRegistry

    async def _mock_register_twin(*_args: Any, **_kwargs: Any) -> None:
        raise MirrorError("Gateway unavailable")

    monkeypatch.setattr(HttpMirrorRegistry, "register_twin", _mock_register_twin)

    result = runner.invoke(app, ["mirror", "register", "--incident", "inc_fail"])
    assert result.exit_code == 1
    assert "Error registering twins with mirror gateway: Gateway unavailable" in (
        result.stderr or result.stdout
    )


def test_cli_mirror_stats_success(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust mirror stats prints tabular data for matching twins."""
    from understudy.contracts.twin import MirrorStats
    from understudy.mirror.registry import HttpMirrorRegistry

    async def _mock_get_all_stats(_self: Any) -> dict[str, MirrorStats]:
        return {
            "twin_inc_mirror_0": MirrorStats(
                twin_id="twin_inc_mirror_0", delivered=3000, dropped=10
            ),
            "twin_inc_mirror_1": MirrorStats(
                twin_id="twin_inc_mirror_1", delivered=3000, dropped=0
            ),
            "twin_other_0": MirrorStats(twin_id="twin_other_0", delivered=50, dropped=0),
        }

    monkeypatch.setattr(HttpMirrorRegistry, "get_all_stats", _mock_get_all_stats)

    # Filtered by incident
    result = runner.invoke(app, ["mirror", "stats", "--incident", "inc_mirror"])
    assert result.exit_code == 0
    assert "twin_inc_mirror_0" in result.stdout
    assert "twin_inc_mirror_1" in result.stdout
    assert "twin_other_0" not in result.stdout

    # All twins
    result_all = runner.invoke(app, ["mirror", "stats"])
    assert result_all.exit_code == 0
    assert "twin_other_0" in result_all.stdout


def test_cli_mirror_stats_empty_and_error(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust mirror stats handles empty results and errors."""
    from understudy.common.errors import MirrorError
    from understudy.contracts.twin import MirrorStats
    from understudy.mirror.registry import HttpMirrorRegistry

    async def _mock_get_all_empty(_self: Any) -> dict[str, MirrorStats]:
        return {}

    monkeypatch.setattr(HttpMirrorRegistry, "get_all_stats", _mock_get_all_empty)

    result_empty = runner.invoke(app, ["mirror", "stats", "--incident", "inc_none"])
    assert result_empty.exit_code == 0
    assert "No mirror stats found for incident=inc_none" in result_empty.stdout

    result_all_empty = runner.invoke(app, ["mirror", "stats"])
    assert result_all_empty.exit_code == 0
    assert "No mirror stats found" in result_all_empty.stdout

    async def _mock_get_all_error(_self: Any) -> dict[str, MirrorStats]:
        raise MirrorError("Network timeout")

    monkeypatch.setattr(HttpMirrorRegistry, "get_all_stats", _mock_get_all_error)
    result_err = runner.invoke(app, ["mirror", "stats"])
    assert result_err.exit_code == 1
    assert "Error fetching mirror stats: Network timeout" in (
        result_err.stderr or result_err.stdout
    )


def test_cli_mirror_unregister(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust mirror unregister handles success and error."""
    from understudy.common.errors import MirrorError
    from understudy.mirror.registry import HttpMirrorRegistry

    unregistered: list[str] = []

    async def _mock_unreg(_self: Any, twin_id: str, **_kwargs: Any) -> None:
        if twin_id == "fail_id":
            raise MirrorError("Failed to unregister")
        unregistered.append(twin_id)

    monkeypatch.setattr(HttpMirrorRegistry, "unregister_twin", _mock_unreg)

    res_ok = runner.invoke(app, ["mirror", "unregister", "--twin-id", "twin-1"])
    assert res_ok.exit_code == 0
    assert "unregistered twin_id=twin-1" in res_ok.stdout
    assert "twin-1" in unregistered

    res_fail = runner.invoke(app, ["mirror", "unregister", "--twin-id", "fail_id"])
    assert res_fail.exit_code == 1
    assert "Error unregistering twin: Failed to unregister" in (res_fail.stderr or res_fail.stdout)


def test_cli_mirror_compare(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust mirror compare computes fidelity deltas and handles missing incident."""
    from understudy.common.errors import MirrorError
    from understudy.contracts.twin import MirrorStats
    from understudy.mirror.registry import HttpMirrorRegistry

    async def _mock_stats(_self: Any) -> dict[str, MirrorStats]:
        return {
            "twin_inc_comp_0": MirrorStats(twin_id="twin_inc_comp_0", delivered=1000, dropped=0),
            "twin_inc_comp_1": MirrorStats(twin_id="twin_inc_comp_1", delivered=990, dropped=10),
            "twin_inc_comp_2": MirrorStats(twin_id="twin_inc_comp_2", delivered=800, dropped=200),
        }

    monkeypatch.setattr(HttpMirrorRegistry, "get_all_stats", _mock_stats)

    res = runner.invoke(app, ["mirror", "compare", "--incident", "inc_comp"])
    assert res.exit_code == 0
    assert "twin_inc_comp_0" in res.stdout
    assert "OK" in res.stdout
    assert "DEGRADED" in res.stdout

    # All twins passing fidelity check
    async def _mock_stats_all_ok(_self: Any) -> dict[str, MirrorStats]:
        return {
            "twin_inc_comp_0": MirrorStats(twin_id="twin_inc_comp_0", delivered=1000, dropped=0),
            "twin_inc_comp_1": MirrorStats(twin_id="twin_inc_comp_1", delivered=995, dropped=5),
        }

    monkeypatch.setattr(HttpMirrorRegistry, "get_all_stats", _mock_stats_all_ok)
    res_all_ok = runner.invoke(app, ["mirror", "compare", "--incident", "inc_comp"])
    assert res_all_ok.exit_code == 0
    assert (
        "Fidelity check: per-twin request count within 2% of prod, path distribution identical."
        in res_all_ok.stdout
    )

    # Incident with no matching twins
    res_missing = runner.invoke(app, ["mirror", "compare", "--incident", "nonexistent"])
    assert res_missing.exit_code == 1
    assert "No registered twins found for incident=nonexistent" in (
        res_missing.stderr or res_missing.stdout
    )

    # Error handling
    async def _mock_stats_err(_self: Any) -> dict[str, MirrorStats]:
        raise MirrorError("Connection refused")

    monkeypatch.setattr(HttpMirrorRegistry, "get_all_stats", _mock_stats_err)
    res_err = runner.invoke(app, ["mirror", "compare", "--incident", "inc_comp"])
    assert res_err.exit_code == 1
    assert "Error fetching stats for comparison: Connection refused" in (
        res_err.stderr or res_err.stdout
    )


def test_cli_mirror_compare_with_paths(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Verify ust mirror compare displays path breakdown when paths are present."""
    from understudy.mirror.fidelity import TwinFidelityReport
    from understudy.mirror.registry import HttpMirrorRegistry

    async def _mock_reports(_self: Any, _incident: str) -> list[TwinFidelityReport]:
        return [
            TwinFidelityReport(
                twin_id="twin_inc_1_0",
                prod_delivered=100,
                twin_delivered=100,
                delivered_delta_ratio=0.0,
                drop_ratio=0.0,
                path_distribution_match=True,
                status="OK",
                prod_paths={"/api/items": 80, "/healthz": 20},
                twin_paths={"/api/items": 80, "/healthz": 20},
            )
        ]

    monkeypatch.setattr(HttpMirrorRegistry, "get_fidelity_reports", _mock_reports)
    res = runner.invoke(app, ["mirror", "compare", "--incident", "inc_1"])
    assert res.exit_code == 0
    assert "twin_inc_1_0" in res.stdout
    assert "Path: /api/items" in res.stdout
    assert "prod=80 (80.0%)" in res.stdout
    assert "twin=80 (80.0%)" in res.stdout
    assert (
        "Fidelity check: per-twin request count within 2% of prod, path distribution identical."
        in res.stdout
    )


def test_cli_tournament_replay_three_candidates() -> None:
    """Verify ust tournament replay on three candidates fixture."""
    res = runner.invoke(
        app,
        ["tournament", "replay", "--fixture", "fixtures/evidence_three_candidates.json"],
    )
    assert res.exit_code == 0
    assert "Scoreboard:" in res.stdout
    assert "plan_rollback" in res.stdout
    assert "plan_restart" in res.stdout
    assert "plan_no_action" in res.stdout
    assert "outcome=decided" in res.stdout
    assert "winner: plan_rollback" in res.stdout
    assert "runner-up: plan_restart" in res.stdout
    assert "margin: 0.1744" in res.stdout


def test_cli_tournament_replay_near_tie() -> None:
    """Verify ust tournament replay on near tie fixture reports ambiguous outcome."""
    res = runner.invoke(
        app,
        ["tournament", "replay", "--fixture", "fixtures/evidence_near_tie.json"],
    )
    assert res.exit_code == 0
    assert "Scoreboard:" in res.stdout
    assert "outcome=ambiguous, no winner" in res.stdout
    assert "margin: 0.0378" in res.stdout

    # With margin override smaller than delta
    res_override = runner.invoke(
        app,
        [
            "tournament",
            "replay",
            "--fixture",
            "fixtures/evidence_near_tie.json",
            "--margin",
            "0.02",
        ],
    )
    assert res_override.exit_code == 0
    assert "outcome=decided" in res_override.stdout
    assert "winner: plan_rollback" in res_override.stdout


def test_cli_tournament_replay_high_drop() -> None:
    """Verify ust tournament replay on high drop candidate disqualifies with evidence_incomplete."""
    res = runner.invoke(
        app,
        ["tournament", "replay", "--fixture", "fixtures/evidence_high_drop.json"],
    )
    assert res.exit_code == 0
    assert "Scoreboard:" in res.stdout
    assert 'Candidate plan_unreliable disqualified with reason "evidence_incomplete"' in res.stdout
    assert "outcome=decided" in res.stdout
    assert "winner: plan_rollback" in res.stdout


def test_cli_tournament_replay_dict_envelope(tmp_path: Path) -> None:
    """Verify ust tournament replay accepts dictionary with evidence key."""
    import json

    orig_fixture = Path("fixtures/evidence_three_candidates.json")
    with orig_fixture.open(encoding="utf-8") as f:
        data = json.load(f)
    env_file = tmp_path / "envelope.json"
    env_file.write_text(json.dumps({"evidence": data}), encoding="utf-8")

    res = runner.invoke(app, ["tournament", "replay", "--fixture", str(env_file)])
    assert res.exit_code == 0
    assert "outcome=decided" in res.stdout
    assert "winner: plan_rollback" in res.stdout


def test_cli_tournament_replay_no_viable_candidate(tmp_path: Path) -> None:
    """Verify ust tournament replay reports no_viable_candidate when all are disqualified."""
    import json

    orig_fixture = Path("fixtures/evidence_high_drop.json")
    with orig_fixture.open(encoding="utf-8") as f:
        data = json.load(f)
    # Mark all evidence incomplete
    for item in data:
        item["evidence_complete"] = False
    all_disq_file = tmp_path / "all_disq.json"
    all_disq_file.write_text(json.dumps(data), encoding="utf-8")

    res = runner.invoke(app, ["tournament", "replay", "--fixture", str(all_disq_file)])
    assert res.exit_code == 0
    assert "outcome=no_viable_candidate, no winner" in res.stdout


def test_cli_tournament_replay_errors(tmp_path: Path) -> None:
    """Verify error handling in ust tournament replay."""
    # 1. Missing file
    res_not_found = runner.invoke(app, ["tournament", "replay", "--fixture", "nonexistent.json"])
    assert res_not_found.exit_code == 1
    assert "Error: fixture file not found" in (res_not_found.stderr or res_not_found.stdout)

    # 2. Malformed JSON
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("invalid json", encoding="utf-8")
    res_bad_json = runner.invoke(app, ["tournament", "replay", "--fixture", str(bad_json)])
    assert res_bad_json.exit_code == 1
    assert "Error reading JSON from fixture" in (res_bad_json.stderr or res_bad_json.stdout)

    # 3. Invalid structure (not list or dict with evidence)
    bad_struct = tmp_path / "bad_struct.json"
    bad_struct.write_text('"a string"', encoding="utf-8")
    res_bad_struct = runner.invoke(app, ["tournament", "replay", "--fixture", str(bad_struct)])
    assert res_bad_struct.exit_code == 1
    assert "Error: expected list of candidate evidences" in (
        res_bad_struct.stderr or res_bad_struct.stdout
    )

    # 4. Empty list
    empty_list = tmp_path / "empty.json"
    empty_list.write_text("[]", encoding="utf-8")
    res_empty = runner.invoke(app, ["tournament", "replay", "--fixture", str(empty_list)])
    assert res_empty.exit_code == 1
    assert "Error: no candidate evidence found" in (res_empty.stderr or res_empty.stdout)

    # 5. Schema validation error
    invalid_schema = tmp_path / "invalid_schema.json"
    invalid_schema.write_text('[{"missing_fields": true}]', encoding="utf-8")
    res_invalid = runner.invoke(app, ["tournament", "replay", "--fixture", str(invalid_schema)])
    assert res_invalid.exit_code == 1
    assert "Error validating CandidateEvidence" in (res_invalid.stderr or res_invalid.stdout)


def test_cli_notify_slack_preview() -> None:
    """Verify ust notify slack in preview mode with fixtures."""
    fixture_path = Path("fixtures/evidence_three_candidates.json")
    context_path = Path("fixtures/context_bad_deploy.json")

    res = runner.invoke(
        app,
        [
            "notify",
            "slack",
            "--incident-id",
            "inc_test_001",
            "--fixture",
            str(fixture_path),
            "--context",
            str(context_path),
        ],
    )
    assert res.exit_code == 0
    assert "=== Slack Message Preview" in res.stdout
    assert "Candidate Scoreboard:" in res.stdout
    assert "Decision Analysis:" in res.stdout
    assert "Mirror Traffic Fidelity:" in res.stdout


def test_cli_notify_slack_json() -> None:
    """Verify ust notify slack --json outputs valid JSON blocks."""
    fixture_path = Path("fixtures/evidence_three_candidates.json")
    res = runner.invoke(
        app,
        ["notify", "slack", "--fixture", str(fixture_path), "--json"],
    )
    assert res.exit_code == 0
    json_start = res.stdout.find("[\n")
    assert json_start != -1
    blocks = json.loads(res.stdout[json_start:])
    assert isinstance(blocks, list)
    assert any(b.get("type") == "header" for b in blocks)


def test_cli_notify_slack_post_success() -> None:
    """Verify ust notify slack --post delivers message."""
    mock_resp = {"ok": True, "channel": "C12345", "ts": "1726500000.0001"}
    with patch(
        "understudy.notify.slack.SlackNotifier.post_reasoning",
        new_callable=AsyncMock,
        return_value=mock_resp,
    ):
        res = runner.invoke(
            app,
            ["notify", "slack", "--incident-id", "inc_live", "--post"],
        )
        assert res.exit_code == 0
        assert "Successfully posted Slack reasoning message to channel C12345" in res.stdout


def test_cli_notify_slack_post_error() -> None:
    """Verify ust notify slack --post handles delivery failure."""
    with patch(
        "understudy.notify.slack.SlackNotifier.post_reasoning",
        new_callable=AsyncMock,
        side_effect=Exception("Slack API token rejected"),
    ):
        res = runner.invoke(
            app,
            ["notify", "slack", "--incident-id", "inc_live", "--post"],
        )
        assert res.exit_code == 1
        assert "Error posting to Slack: Slack API token rejected" in (res.stderr or res.stdout)


def test_cli_notify_slack_file_errors(tmp_path: Path) -> None:
    """Verify file error handling in ust notify slack."""
    # 1. Nonexistent fixture
    res1 = runner.invoke(app, ["notify", "slack", "--fixture", "nonexistent.json"])
    assert res1.exit_code == 1
    assert "Error: fixture file not found" in (res1.stderr or res1.stdout)

    # 2. Bad JSON in fixture
    bad_fix = tmp_path / "bad_fixture.json"
    bad_fix.write_text("not json", encoding="utf-8")
    res2 = runner.invoke(app, ["notify", "slack", "--fixture", str(bad_fix)])
    assert res2.exit_code == 1
    assert "Error loading fixture" in (res2.stderr or res2.stdout)

    # 3. Nonexistent context
    res3 = runner.invoke(app, ["notify", "slack", "--context", "nonexistent_context.json"])
    assert res3.exit_code == 1
    assert "Error: context file not found" in (res3.stderr or res3.stdout)

    # 4. Bad JSON in context
    bad_ctx = tmp_path / "bad_context.json"
    bad_ctx.write_text("not json", encoding="utf-8")
    res4 = runner.invoke(app, ["notify", "slack", "--context", str(bad_ctx)])
    assert res4.exit_code == 1
    assert "Error loading context file" in (res4.stderr or res4.stdout)
