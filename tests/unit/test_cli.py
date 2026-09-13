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


def test_cli_plan_success_fake() -> None:
    """Verify ust plan produces plans with NO_ACTION in fake mode."""
    result = runner.invoke(
        app,
        ["plan", "--context", "fixtures/context_bad_deploy.json", "--count", "3", "--fake"],
    )
    assert result.exit_code == 0
    assert "Generated 3 candidate plan(s)" in result.stdout
    assert "rollback_deploy" in result.stdout
    assert "scale_workload" in result.stdout
    assert "no_action" in result.stdout


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
    plans = json.loads(result.stdout)
    assert isinstance(plans, list)
    assert len(plans) == 3
    assert plans[2]["action"] == "no_action"
    assert plans[2]["inverse"] is None
    assert plans[2]["target_resources"] == []
    assert plans[2]["declared_blast_set"] == []


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
    assert "Planner run 1 (3 plans)" in result.stdout
    assert "Planner run 2 (3 plans)" in result.stdout
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
