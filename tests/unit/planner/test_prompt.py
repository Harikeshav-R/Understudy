"""Unit tests for the candidate-generation prompt engine and schemas."""

from datetime import UTC, datetime

import pytest

from understudy.common.errors import PlannerError
from understudy.contracts.enums import ActionType, FailureClass
from understudy.contracts.incident import (
    Alert,
    DependencyEdge,
    DependencyGraphSnapshot,
    DeployRef,
    ErrorSignature,
    IncidentContext,
    MetricPoint,
    MetricSeries,
    MetricWindow,
)
from understudy.contracts.plan import (
    ActionParams,
    RemediationPlan,
    ResourceRef,
)
from understudy.planner.prompt import (
    PlannerPromptResponse,
    PromptCandidatePlan,
    PromptInversePlan,
    build_system_prompt,
    build_user_prompt,
    format_alert,
    format_dependency_graph,
    format_deploys,
    format_incident_context,
    format_metrics,
    format_playbook_candidate,
    format_signatures,
    get_planner_output_schema,
    parse_raw_planner_json,
    prompt_candidate_to_remediation_plan,
    render_candidate_generation_prompt,
)

FIXED_NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def _sample_context(
    with_signatures: bool = True,
    with_deploys: bool = True,
    with_edges: bool = True,
    with_series: bool = True,
    inferred_fc: FailureClass | None = FailureClass.BAD_DEPLOY,
) -> IncidentContext:
    now = FIXED_NOW
    alert = Alert(
        alert_id="alt_999",
        source="pagerduty",
        title="High latency on edge-gateway",
        service="edge-gateway",
        severity="critical",
        fired_at=now,
        raw={"incident_key": "pd-1234"},
    )
    signatures = (
        [
            ErrorSignature(
                fingerprint="fp_n_plus_one",
                message="N+1 query detected on item list",
                service="data-service",
                count=42,
                first_seen=now,
                last_seen=now,
            )
        ]
        if with_signatures
        else []
    )
    series_list = (
        [
            MetricSeries(
                metric_name="http_request_duration_seconds",
                labels={"service": "edge-gateway", "route": "/items"},
                points=[MetricPoint(timestamp=now, value=0.45)],
            ),
            MetricSeries(
                metric_name="empty_series",
                labels={},
                points=[],
            ),
        ]
        if with_series
        else []
    )
    metrics = MetricWindow(
        service="edge-gateway",
        start_time=now,
        end_time=now,
        p99_latency_ms=450.0,
        error_rate=0.035,
        request_count=1200,
        series=series_list,
    )
    deploys = (
        [
            DeployRef(
                commit_sha="c0ffee1",
                image_digests={"data-service": "sha256:abcd"},
                deployed_at=now,
                pr_number=101,
                contains_migration=False,
            ),
            DeployRef(
                commit_sha="c0ffee0",
                image_digests={},
                deployed_at=now,
                pr_number=None,
                contains_migration=True,
            ),
        ]
        if with_deploys
        else []
    )
    edges = (
        [
            DependencyEdge(source="edge-gateway", target="auth-service"),
            DependencyEdge(source="edge-gateway", target="data-service"),
        ]
        if with_edges
        else []
    )
    graph = DependencyGraphSnapshot(
        nodes=["edge-gateway", "auth-service", "data-service"] if with_edges else [],
        edges=edges,
        observed_at=now,
    )
    return IncidentContext(
        incident_id="inc_test_001",
        alert=alert,
        signatures=signatures,
        metrics_window=metrics,
        recent_deploys=deploys,
        dependency_graph=graph,
        inferred_failure_class=inferred_fc,
        gathered_at=now,
    )


def test_format_alert() -> None:
    now = FIXED_NOW
    alert_with_raw = Alert(
        alert_id="alt_1",
        source="pagerduty",
        title="Outage",
        service="auth-service",
        severity="error",
        fired_at=now,
        raw={"foo": "bar"},
    )
    text = format_alert(alert_with_raw)
    assert "Alert ID: alt_1" in text
    assert "Raw Details:" in text
    assert '"foo": "bar"' in text

    alert_no_raw = Alert(
        alert_id="alt_2",
        source="synthetic",
        title="Synthetic issue",
        service="worker",
        severity="warning",
        fired_at=now,
        raw={},
    )
    text2 = format_alert(alert_no_raw)
    assert "Alert ID: alt_2" in text2
    assert "Raw Details:" not in text2


def test_format_signatures() -> None:
    empty_text = format_signatures([])
    assert empty_text == "None observed."

    now = FIXED_NOW
    sigs = [
        ErrorSignature(
            fingerprint="fp_1",
            message="Database deadlocked",
            service="data-service",
            count=10,
            first_seen=now,
            last_seen=now,
        )
    ]
    text = format_signatures(sigs)
    assert "1. [data-service] count=10 fingerprint=fp_1" in text
    assert "Database deadlocked" in text


def test_format_metrics() -> None:
    now = FIXED_NOW
    metrics_full = MetricWindow(
        service="edge-gateway",
        start_time=now,
        end_time=now,
        p99_latency_ms=123.4,
        error_rate=0.015,
        request_count=500,
        series=[
            MetricSeries(
                metric_name="latency",
                labels={"env": "prod"},
                points=[MetricPoint(timestamp=now, value=123.4)],
            )
        ],
    )
    text = format_metrics(metrics_full)
    assert "p99 Latency: 123.4 ms" in text
    assert "Error Rate: 1.50%" in text
    assert "Total Requests: 500" in text
    assert "latency{env=prod}: latest_value=123.40" in text

    metrics_empty = MetricWindow(
        service="data-service",
        start_time=now,
        end_time=now,
        p99_latency_ms=None,
        error_rate=None,
        request_count=0,
        series=[],
    )
    text_empty = format_metrics(metrics_empty)
    assert "p99 Latency: N/A" in text_empty
    assert "Error Rate: N/A" in text_empty
    assert "Series:" not in text_empty


def test_format_deploys() -> None:
    empty_text = format_deploys([])
    assert empty_text == "No recent deployments recorded."

    now = FIXED_NOW
    deploys = [
        DeployRef(
            commit_sha="abcdef1",
            image_digests={"data-service": "sha256:1111"},
            deployed_at=now,
            pr_number=50,
            contains_migration=True,
        )
    ]
    text = format_deploys(deploys)
    assert "commit: abcdef1" in text
    assert "contains_migration: True" in text
    assert "data-service=sha256:1111" in text


def test_format_dependency_graph() -> None:
    ctx_empty = _sample_context(with_edges=False)
    text_empty = format_dependency_graph(ctx_empty)
    assert "Services (nodes): None" in text_empty
    assert "Dependencies (edges): None declared" in text_empty

    ctx_full = _sample_context(with_edges=True)
    text_full = format_dependency_graph(ctx_full)
    assert "auth-service" in text_full
    assert "data-service" in text_full
    assert "edge-gateway" in text_full
    assert "edge-gateway -> auth-service" in text_full
    assert "edge-gateway -> data-service" in text_full


def test_format_playbook_candidate() -> None:
    assert format_playbook_candidate(None) == "None matched."

    plan = RemediationPlan(
        plan_id="pb_plan_1",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service", target_commit="c0ffee0"),
        target_resources=[
            ResourceRef(namespace="ust-twin", kind="Deployment", name="data-service")
        ],
        declared_blast_set=["data-service", "edge-gateway"],
        inverse=None,
        rationale="Roll back known regression",
        origin="playbook",
        playbook_id="pb_rollback_01",
    )
    text = format_playbook_candidate(plan)
    assert "Playbook ID: pb_rollback_01" in text
    assert "Recommended Action: rollback_deploy" in text
    assert "data-service, edge-gateway" in text
    assert "Roll back known regression" in text


def test_format_incident_context() -> None:
    ctx = _sample_context(inferred_fc=FailureClass.CONFIG_DRIFT)
    text = format_incident_context(ctx)
    assert "=== INCIDENT IDENTIFIER ===" in text
    assert "=== INFERRED FAILURE CLASS ===\nconfig_drift" in text
    assert "=== ACTIVE ALERT ===" in text
    assert "=== ERROR SIGNATURES (LOGS) ===" in text
    assert "=== METRICS TELEMETRY ===" in text
    assert "=== RECENT DEPLOYMENTS (GITHUB) ===" in text
    assert "=== DEPENDENCY GRAPH ===" in text
    assert "=== PLAYBOOK MATCH CANDIDATE ===\nNone matched." in text

    ctx_no_fc = _sample_context(inferred_fc=None)
    text_no_fc = format_incident_context(ctx_no_fc)
    assert "=== INFERRED FAILURE CLASS ===\nUnknown" in text_no_fc


def test_get_planner_output_schema() -> None:
    schema = get_planner_output_schema()
    assert "properties" in schema
    assert "plans" in schema["properties"]
    assert "required" in schema
    assert "plans" in schema["required"]


def test_build_system_prompt() -> None:
    sys_prompt = build_system_prompt()
    assert "Autonomous Remediation Planner for Understudy" in sys_prompt
    assert "CLOSED ACTION ENUMERATION:" in sys_prompt
    assert '"rollback_deploy"' in sys_prompt
    assert '"restart_workload"' in sys_prompt
    assert '"scale_workload"' in sys_prompt
    assert '"disable_flag"' in sys_prompt
    assert '"revert_config"' in sys_prompt
    assert '"no_action"' in sys_prompt
    assert "OUTPUT FORMAT:" in sys_prompt
    assert "CRITICAL RULES:" in sys_prompt


def test_build_user_prompt_and_render() -> None:
    ctx = _sample_context()
    user_prompt = build_user_prompt(ctx, count=3)
    assert "inc_test_001" in user_prompt
    assert (
        "Generate exactly 3 distinct, viable, and diverse candidate remediation plans"
        in user_prompt
    )

    messages = render_candidate_generation_prompt(ctx, count=4)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "Generate exactly 4 distinct" in messages[1]["content"]


def test_parse_raw_planner_json_success() -> None:
    valid_json = """
    {
      "plans": [
        {
          "action": "rollback_deploy",
          "params": {
            "workload": "data-service",
            "target_commit": "c0ffee0"
          },
          "declared_blast_set": ["data-service", "edge-gateway"],
          "inverse": {
            "action": "rollback_deploy",
            "params": {
              "workload": "data-service",
              "target_commit": "c0ffee1"
            },
            "declared_blast_set": ["data-service"],
            "rationale": "Roll forward to current commit"
          },
          "rationale": "Roll back bad deployment"
        },
        {
          "action": "no_action",
          "params": {
            "workload": "edge-gateway"
          },
          "declared_blast_set": [],
          "inverse": null,
          "rationale": "Observe without intervention"
        }
      ]
    }
    """
    res = parse_raw_planner_json(valid_json)
    assert isinstance(res, PlannerPromptResponse)
    assert len(res.plans) == 2
    assert res.plans[0].action == ActionType.ROLLBACK_DEPLOY
    assert res.plans[0].inverse is not None
    assert res.plans[1].action == ActionType.NO_ACTION
    assert res.plans[1].inverse is None

    # Test markdown fence stripping
    fenced_json = f"```json\n{valid_json}\n```"
    res_fenced = parse_raw_planner_json(fenced_json)
    assert len(res_fenced.plans) == 2

    generic_fenced = f"```\n{valid_json}\n```"
    res_gen = parse_raw_planner_json(generic_fenced)
    assert len(res_gen.plans) == 2


def test_parse_raw_planner_json_failures() -> None:
    # Invalid JSON
    with pytest.raises(PlannerError) as exc_info:
        parse_raw_planner_json("not valid json")
    assert "Failed to decode planner response as JSON" in str(exc_info.value)
    assert "raw_text" in exc_info.value.details

    # Schema violation: invalid action
    bad_action_json = """
    {
      "plans": [
        {
          "action": "destroy_everything",
          "params": {"workload": "data-service"},
          "declared_blast_set": [],
          "rationale": "Bad"
        }
      ]
    }
    """
    with pytest.raises(PlannerError) as exc_info2:
        parse_raw_planner_json(bad_action_json)
    assert "Planner response violates output schema" in str(exc_info2.value)
    assert "errors" in exc_info2.value.details


def test_prompt_candidate_to_remediation_plan() -> None:
    # 1. Action with inverse and deployment resource
    cand_with_inv = PromptCandidatePlan(
        action=ActionType.SCALE_WORKLOAD,
        params=ActionParams(workload="data-service", replica_delta=2),
        declared_blast_set=["data-service"],
        inverse=PromptInversePlan(
            action=ActionType.SCALE_WORKLOAD,
            params=ActionParams(workload="data-service", replica_delta=-2),
            declared_blast_set=["data-service"],
            rationale="Scale back down",
        ),
        rationale="Scale up under load",
    )
    plan = prompt_candidate_to_remediation_plan(
        cand_with_inv, candidate_index=1, plan_id="custom_plan_1", playbook_id="pb_99"
    )
    assert plan.plan_id == "custom_plan_1"
    assert plan.candidate_index == 1
    assert plan.action == ActionType.SCALE_WORKLOAD
    assert plan.origin == "planner"
    assert plan.playbook_id == "pb_99"
    assert len(plan.target_resources) == 1
    assert plan.target_resources[0].kind == "Deployment"
    assert plan.target_resources[0].name == "data-service"
    assert plan.inverse is not None
    assert plan.inverse.plan_id == "custom_plan_1_inv"
    assert plan.inverse.params.replica_delta == -2
    assert len(plan.inverse.target_resources) == 1
    assert plan.inverse.target_resources[0].kind == "Deployment"

    # 2. Config revert action (kind should be ConfigMap)
    cand_revert = PromptCandidatePlan(
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(
            workload="auth-service", config_key="CACHE_ENABLED", config_value="true"
        ),
        declared_blast_set=["auth-service"],
        inverse=PromptInversePlan(
            action=ActionType.REVERT_CONFIG,
            params=ActionParams(
                workload="auth-service", config_key="CACHE_ENABLED", config_value="false"
            ),
            declared_blast_set=["auth-service"],
            rationale="Revert back",
        ),
        rationale="Restore cache setting",
    )
    plan_revert = prompt_candidate_to_remediation_plan(cand_revert, candidate_index=0)
    assert plan_revert.plan_id == "plan_cand_0"
    assert plan_revert.target_resources[0].kind == "ConfigMap"
    assert plan_revert.inverse is not None
    assert plan_revert.inverse.target_resources[0].kind == "ConfigMap"

    # 3. NO_ACTION plan (no target resources, inverse null)
    cand_no_action = PromptCandidatePlan(
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="edge-gateway"),
        declared_blast_set=[],
        inverse=None,
        rationale="Do nothing",
    )
    plan_no_action = prompt_candidate_to_remediation_plan(cand_no_action, candidate_index=2)
    assert plan_no_action.action == ActionType.NO_ACTION
    assert plan_no_action.target_resources == []
    assert plan_no_action.inverse is None

    # 4. Action with empty workload
    cand_no_workload = PromptCandidatePlan(
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload=""),
        declared_blast_set=[],
        inverse=PromptInversePlan(
            action=ActionType.RESTART_WORKLOAD,
            params=ActionParams(workload=""),
            declared_blast_set=[],
            rationale="Restart inverse",
        ),
        rationale="Restart with empty workload",
    )
    plan_empty_wl = prompt_candidate_to_remediation_plan(cand_no_workload, candidate_index=3)
    assert plan_empty_wl.target_resources == []
    assert plan_empty_wl.inverse is not None
    assert plan_empty_wl.inverse.target_resources == []


@pytest.mark.asyncio
async def test_planner_protocol_and_fake() -> None:
    from understudy.planner.api import Planner
    from understudy.planner.fakes import FakePlanner

    fake = FakePlanner(seed=42)
    assert isinstance(fake, Planner)

    ctx = _sample_context()
    plans = await fake.generate_candidates(ctx, count=3)
    assert len(plans) == 3
    assert plans[0].action == ActionType.ROLLBACK_DEPLOY
    assert plans[1].action == ActionType.SCALE_WORKLOAD
    assert plans[2].action == ActionType.NO_ACTION
