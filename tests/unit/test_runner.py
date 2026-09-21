import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from understudy.common.clock import FrozenClock
from understudy.common.config import TimeoutSettings
from understudy.common.errors import OrchestratorError, ScenarioError
from understudy.contracts.enums import (
    InvariantTier,
    KernelVerdictType,
    RunOutcome,
    TournamentOutcome,
)
from understudy.contracts.evidence import (
    CandidateEvidence,
    CandidateScore,
    ProbeSample,
    TournamentResult,
)
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.kernel import InvariantResult, KernelVerdict
from understudy.contracts.run import RunRecord
from understudy.contracts.twin import MirrorStats
from understudy.orchestrator.fakes import create_fake_deps
from understudy.runner import (
    ScenarioDefinition,
    create_scenario_deps,
    format_run_summary,
    format_scoreboard_table,
    load_scenario_definition,
    run_scenario,
)


def test_load_scenario_definition_from_seed(tmp_path: Path) -> None:
    """Load valid scenario YAML from disk."""
    scenarios_dir = tmp_path / "scenarios" / "seed"
    scenarios_dir.mkdir(parents=True)
    f = scenarios_dir / "test_seed.yaml"
    f.write_text(
        """
id: test_seed
origin: seed
failure_class: bad_deploy
description: test scenario description
injection:
  kind: deploy_image
  target: data-service
  params:
    image_tag: regression
  settle_seconds: 5
alert_template:
  title: "SLO breach"
  service: "edge-gateway"
  severity: "critical"
expected_remediation_class: rollback_deploy
expected_escalation: false
ground_truth_note: "Fix is rollback"
migration_boundary: false
""",
        encoding="utf-8",
    )

    scen = load_scenario_definition("test_seed", base_dir=tmp_path)
    assert scen.id == "test_seed"
    assert scen.failure_class == "bad_deploy"
    assert scen.injection is not None
    assert scen.injection["kind"] == "deploy_image"
    assert scen.expected_escalation is False


def test_load_scenario_definition_direct_file(tmp_path: Path) -> None:
    """Load scenario from direct absolute or relative file path."""
    f = tmp_path / "custom.yaml"
    f.write_text(
        """
id: custom_file
origin: custom
description: custom direct file
alert_template:
  title: "Alert"
""",
        encoding="utf-8",
    )
    scen = load_scenario_definition(str(f))
    assert scen.id == "custom_file"

    # Relative path from base_dir
    scen_rel = load_scenario_definition("custom.yaml", base_dir=tmp_path)
    assert scen_rel.id == "custom_file"


def test_load_scenario_definition_corrupt_file(tmp_path: Path) -> None:
    """Corrupt YAML raises ScenarioError."""
    bad_file = tmp_path / "bad.yaml"
    bad_file.write_text("invalid: [unclosed", encoding="utf-8")
    with pytest.raises(ScenarioError, match="Failed to parse scenario file"):
        load_scenario_definition(str(bad_file))

    # Candidate file corrupt
    scen_dir = tmp_path / "scenarios" / "seed"
    scen_dir.mkdir(parents=True)
    (scen_dir / "broken.yaml").write_text("invalid: [unclosed", encoding="utf-8")
    with pytest.raises(ScenarioError, match="Failed to parse scenario file"):
        load_scenario_definition("broken", base_dir=tmp_path)

    # Relative corrupt file
    bad_rel = tmp_path / "broken_rel.yaml"
    bad_rel.write_text("invalid: [unclosed", encoding="utf-8")
    with pytest.raises(ScenarioError, match="Failed to parse scenario file"):
        load_scenario_definition("broken_rel.yaml", base_dir=tmp_path)


def test_load_scenario_definition_fallback_template() -> None:
    """Fallback to built-in template when file not found on disk."""
    scen = load_scenario_definition("bad_deploy_data_service", base_dir=Path("/non/existent"))
    assert scen.id == "bad_deploy_data_service"
    assert scen.alert_template["service"] == "edge-gateway"
    assert scen.expected_escalation is False

    mig_scen = load_scenario_definition("bad_deploy_with_migration", base_dir=Path("/non/existent"))
    assert mig_scen.expected_escalation is True


def test_format_scoreboard_table() -> None:
    """Verify ASCII scoreboard table formatting."""
    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    evidence = [
        CandidateEvidence(
            plan_id="plan_0",
            twin_id="twin_0",
            applied_at=now,
            probes=[ProbeSample(at=now, healthy=True, p99_latency_ms=80.0, error_rate=0.0)],
            recovered=True,
            recovery_seconds=10.0,
            observed_blast_set=[],
            downstream_error_delta=0.0,
            invariant_violations=[],
            mirror_stats=MirrorStats(twin_id="twin_0", delivered=100, dropped=0),
            evidence_complete=True,
        ),
        CandidateEvidence(
            plan_id="plan_1",
            twin_id="twin_1",
            applied_at=now,
            probes=[],
            recovered=False,
            recovery_seconds=None,
            observed_blast_set=["auth-service"],
            downstream_error_delta=0.05,
            invariant_violations=[],
            mirror_stats=MirrorStats(twin_id="twin_1", delivered=100, dropped=20),
            evidence_complete=False,
        ),
        CandidateEvidence(
            plan_id="plan_2",
            twin_id="twin_2",
            applied_at=now,
            probes=[ProbeSample(at=now, healthy=True, p99_latency_ms=90.0, error_rate=0.0)],
            recovered=True,
            recovery_seconds=15.0,
            observed_blast_set=[],
            downstream_error_delta=0.0,
            invariant_violations=[],
            mirror_stats=MirrorStats(twin_id="twin_2", delivered=100, dropped=0),
            evidence_complete=True,
        ),
    ]

    scores = [
        CandidateScore(
            plan_id="plan_0",
            composite=0.05,
            components={"recovery": 0.05, "blast": 0.0, "downstream": 0.0, "violations": 0.0},
            disqualified=False,
        ),
        CandidateScore(
            plan_id="plan_1",
            composite=1.0,
            components={"recovery": 1.0, "blast": 0.2, "downstream": 0.05, "violations": 0.0},
            disqualified=True,
            disqualification_reason="mirror_drop_exceeded",
        ),
        CandidateScore(
            plan_id="plan_2",
            composite=0.20,
            components={"recovery": 0.20, "blast": 0.0, "downstream": 0.0, "violations": 0.0},
            disqualified=False,
        ),
    ]

    table = format_scoreboard_table(evidence, scores)
    assert "Scoreboard:" in table
    assert "plan_0" in table
    assert "plan_1" in table
    assert "plan_2" in table
    assert "disqualified (mirror_drop_exceeded)" in table
    assert "viable" in table
    assert 'Candidate plan_1 disqualified with reason "mirror_drop_exceeded"' in table


def test_format_run_summary() -> None:
    """Verify run summary text output across DECIDED, AMBIGUOUS, and ESCALATED outcomes."""
    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    ctx = IncidentContext(
        incident_id="inc_1",
        alert=Alert(
            alert_id="alt_1",
            source="synthetic",
            title="SLO breach",
            service="edge-gateway",
            severity="critical",
            fired_at=now,
        ),
        signatures=[],
        metrics_window=MetricWindow(service="edge-gateway", start_time=now, end_time=now),
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(observed_at=now),
        gathered_at=now,
    )

    # 1. Decided and Executed
    record_ok = RunRecord(
        run_id="run_1",
        incident_id="inc_1",
        started_at=now,
        finished_at=now,
        outcome=RunOutcome.EXECUTED,
        context=ctx,
        plans=[],
        evidence=[],
        tournament=TournamentResult(
            incident_id="inc_1",
            outcome=TournamentOutcome.DECIDED,
            winner_plan_id="plan_0",
            runner_up_plan_id="plan_1",
            margin=0.25,
            decided_at=now,
        ),
        verdict=KernelVerdict(
            incident_id="inc_1",
            plan_id="plan_0",
            verdict=KernelVerdictType.PASS,
            results=[
                InvariantResult(
                    invariant_id="K1",
                    tier=InvariantTier.PROOF,
                    satisfied=True,
                    reason="Pass",
                )
            ],
            solver_ms=5.0,
            human_reason="Proved safe",
        ),
        prod_applied_plan_id="plan_0",
        prod_outcome="resolved",
    )
    s_ok = format_run_summary(record_ok)
    assert "outcome=decided" in s_ok
    assert "winner: plan_0" in s_ok
    assert "margin: 0.2500" in s_ok
    assert "kernel verdict = pass" in s_ok
    assert "production rolled back; prod probe healthy within 180s; prod_outcome=resolved" in s_ok
    assert "outcome=executed plan=plan_0" in s_ok

    # 2. Ambiguous and Escalated
    record_amb = RunRecord(
        run_id="run_2",
        incident_id="inc_2",
        started_at=now,
        finished_at=now,
        outcome=RunOutcome.ESCALATED,
        context=ctx,
        plans=[],
        evidence=[],
        tournament=TournamentResult(
            incident_id="inc_2",
            outcome=TournamentOutcome.AMBIGUOUS,
            margin=0.05,
            decided_at=now,
        ),
        escalation_reason="Tournament ambiguous",
    )
    s_amb = format_run_summary(record_amb)
    assert "outcome=ambiguous, no winner" in s_amb
    assert "margin: 0.0500" in s_amb
    assert "no production change; escalated to PagerDuty" in s_amb
    assert 'outcome=escalated reason="Tournament ambiguous"' in s_amb

    # 3. No Viable Candidate
    record_nv = RunRecord(
        run_id="run_3",
        incident_id="inc_3",
        started_at=now,
        finished_at=now,
        outcome=RunOutcome.ESCALATED,
        context=ctx,
        plans=[],
        evidence=[],
        tournament=TournamentResult(
            incident_id="inc_3",
            outcome=TournamentOutcome.NO_VIABLE_CANDIDATE,
            decided_at=now,
        ),
    )
    s_nv = format_run_summary(record_nv)
    assert "outcome=no_viable_candidate, no winner" in s_nv

    # 4. Vetoed Invariant with Vetoed Invariant List
    record_veto = RunRecord(
        run_id="run_4",
        incident_id="inc_4",
        started_at=now,
        finished_at=now,
        outcome=RunOutcome.ESCALATED,
        context=ctx,
        plans=[],
        evidence=[],
        verdict=KernelVerdict(
            incident_id="inc_4",
            plan_id="plan_0",
            verdict=KernelVerdictType.VETO,
            results=[
                InvariantResult(
                    invariant_id="K3",
                    tier=InvariantTier.PROOF,
                    satisfied=False,
                    reason="Migration boundary violated",
                )
            ],
            solver_ms=5.0,
            human_reason="VETO K3",
        ),
    )
    s_veto = format_run_summary(record_veto)
    assert "kernel verdict = veto (K3)" in s_veto


def test_create_scenario_deps() -> None:
    """create_scenario_deps configures fake and live deps appropriately."""
    clock = FrozenClock(datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC))
    scen = ScenarioDefinition(
        id="bad_deploy_data_service",
        origin="seed",
        failure_class="bad_deploy",
        description="test",
        injection=None,
        alert_template={},
        expected_remediation_class="rollback_deploy",
        expected_escalation=False,
        ground_truth_note="",
        migration_boundary=False,
    )

    # Fake deps
    fake_deps = create_scenario_deps(scenario=scen, fake=True, clock=clock)
    assert fake_deps is not None
    assert fake_deps.safety_kernel is not None

    # Live deps
    with (
        patch("understudy.deps.create_real_deps") as mock_real,
        patch("understudy.deps.create_deps") as mock_deps,
    ):
        mock_real.return_value = create_fake_deps()
        mock_deps.return_value = create_fake_deps()
        live_deps = create_scenario_deps(scenario=scen, live=True, clock=clock)
        assert live_deps is not None
        mock_real.assert_called_once()
        mock_deps.assert_called_once()


@pytest.mark.asyncio
async def test_run_scenario_fake() -> None:
    """run_scenario executes end-to-end on fakes and returns transitions and record."""
    clock = FrozenClock(datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC))
    transitions, record = await run_scenario(
        scenario_id_or_path="seed/bad_deploy_data_service",
        fake=True,
        seed=42,
        clock=clock,
    )
    assert "ingest" in transitions
    assert "record_run" in transitions
    assert record.outcome == RunOutcome.EXECUTED
    assert record.prod_applied_plan_id == "plan_cand_0"


@pytest.mark.asyncio
async def test_run_scenario_fake_force_veto() -> None:
    """run_scenario with force_veto escalates to PagerDuty."""
    clock = FrozenClock(datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC))
    transitions, record = await run_scenario(
        scenario_id_or_path="seed/bad_deploy_with_migration",
        fake=True,
        seed=42,
        force_veto=True,
        clock=clock,
    )
    assert "escalate_pagerduty" in transitions
    assert record.outcome == RunOutcome.ESCALATED


@pytest.mark.asyncio
async def test_run_scenario_live_with_injection() -> None:
    """run_scenario with live=True and inject=True invokes inject_scenario_fault."""
    fake_deps = create_fake_deps()
    with patch(
        "understudy.runner.inject_scenario_fault",
        new_callable=AsyncMock,
    ) as mock_inj:
        transitions, _ = await run_scenario(
            scenario_id_or_path="seed/bad_deploy_data_service",
            live=True,
            inject=True,
            deps=fake_deps,
        )
        assert mock_inj.called
        assert "record_run" in transitions


@pytest.mark.asyncio
async def test_run_scenario_missing_record_raises() -> None:
    """If run record is not found in store after completion, raises OrchestratorError."""
    fake_deps = create_fake_deps()
    fake_deps.run_store.get_run = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(OrchestratorError, match=r"Run record .* not found in store"):
        await run_scenario(
            scenario_id_or_path="seed/bad_deploy_data_service",
            fake=True,
            deps=fake_deps,
        )


def test_normalize_scenario_token_yaml_extension() -> None:
    """_normalize_scenario_token strips .yaml extension from scenario name when not a file."""
    scen = load_scenario_definition(
        "bad_deploy_data_service.yaml",
        base_dir=Path("/non/existent"),
    )
    assert scen.id == "bad_deploy_data_service"


def test_format_run_summary_other_outcome() -> None:
    """format_run_summary formats RunOutcome.FAILED."""
    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    ctx = IncidentContext(
        incident_id="inc_fail",
        alert=Alert(
            alert_id="alt_fail",
            source="synthetic",
            service="edge-gateway",
            title="SLO breach",
            severity="critical",
            fired_at=now,
        ),
        signatures=[],
        metrics_window=MetricWindow(service="edge-gateway", start_time=now, end_time=now),
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(observed_at=now),
        gathered_at=now,
    )
    record = RunRecord(
        run_id="run_fail",
        incident_id="inc_fail",
        started_at=now,
        finished_at=now,
        outcome=RunOutcome.FAILED,
        context=ctx,
        plans=[],
        evidence=[],
    )
    summary = format_run_summary(record)
    assert "outcome=failed" in summary


@pytest.mark.asyncio
async def test_run_scenario_with_transition_callback_and_unknown_injection(
    tmp_path: Path,
) -> None:
    """run_scenario calls on_transition callback and ignores unknown injection kinds."""
    scen_dir = tmp_path / "scenarios" / "seed"
    scen_dir.mkdir(parents=True)
    custom_yaml = scen_dir / "unknown_inj.yaml"
    custom_yaml.write_text(
        """
id: unknown_inj
origin: test
failure_class: custom
description: test unknown injection
injection:
  kind: unsupported_chaos
  target: data-service
alert_template:
  title: "Test"
  service: "data-service"
expected_remediation_class: restart
expected_escalation: false
ground_truth_note: "none"
""",
        encoding="utf-8",
    )

    visited_nodes: list[str] = []
    fake_deps = create_fake_deps()

    transitions, _ = await run_scenario(
        scenario_id_or_path="unknown_inj",
        live=True,
        inject=True,
        deps=fake_deps,
        base_dir=tmp_path,
        on_transition=lambda node: visited_nodes.append(node),
    )

    assert len(visited_nodes) > 0
    assert visited_nodes == transitions
    assert "record_run" in transitions


@pytest.mark.asyncio
async def test_run_scenario_watchdog_timeout_with_existing_record() -> None:
    """Watchdog timeout during scenario returns existing persisted record if present."""
    fake_deps = create_fake_deps()
    fake_deps = dataclasses.replace(fake_deps, timeouts=TimeoutSettings(incident_seconds=0.01))

    with patch("understudy.runner.build_graph") as mock_build_graph:
        mock_graph = MagicMock()

        async def _slow_astream(*_: Any, **__: Any):  # type: ignore[no-untyped-def]
            import asyncio

            await asyncio.sleep(0.5)
            yield {"slow_node": {}}

        mock_graph.astream = _slow_astream
        mock_build_graph.return_value = mock_graph

        with patch.object(fake_deps.run_store, "get_run", new_callable=AsyncMock) as mock_get_run:
            fake_record = MagicMock(spec=RunRecord)
            fake_record.outcome = RunOutcome.ESCALATED
            mock_get_run.return_value = fake_record

            _transitions, record = await run_scenario(
                scenario_id_or_path="seed/bad_deploy_data_service",
                fake=True,
                deps=fake_deps,
            )
            assert record is not None
            assert record.outcome == RunOutcome.ESCALATED


@pytest.mark.asyncio
async def test_run_scenario_watchdog_timeout_missing_record_raises() -> None:
    """Watchdog timeout without persisted run record raises OrchestratorError."""
    fake_deps = create_fake_deps()
    fake_deps = dataclasses.replace(fake_deps, timeouts=TimeoutSettings(incident_seconds=0.01))

    with patch("understudy.runner.build_graph") as mock_build_graph:
        mock_graph = MagicMock()

        async def _slow_astream(*_: Any, **__: Any):  # type: ignore[no-untyped-def]
            import asyncio

            await asyncio.sleep(0.5)
            yield {"slow_node": {}}

        mock_graph.astream = _slow_astream
        mock_build_graph.return_value = mock_graph

        with patch.object(fake_deps.run_store, "get_run", new_callable=AsyncMock) as mock_get_run:
            mock_get_run.return_value = None
            with pytest.raises(OrchestratorError, match="Incident watchdog timed out"):
                await run_scenario(
                    scenario_id_or_path="seed/bad_deploy_data_service",
                    fake=True,
                    deps=fake_deps,
                )
