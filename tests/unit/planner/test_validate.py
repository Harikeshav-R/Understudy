"""Unit tests for planner schema validation, retry coordination, and normalisation.

Implements unit tests for build-plan step B3.2 to 100% line and branch coverage.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from understudy.common.config import ClusterSettings, SecretSettings, Settings, TimeoutSettings
from understudy.common.errors import PlannerError
from understudy.contracts.enums import ActionType, FailureClass
from understudy.contracts.incident import (
    Alert,
    DependencyEdge,
    DependencyGraphSnapshot,
    DeployRef,
    ErrorSignature,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.graph.api import DependencyGraph
from understudy.planner.api import Planner
from understudy.planner.prompt import (
    PlannerPromptResponse,
    PromptCandidatePlan,
    PromptInversePlan,
)
from understudy.planner.validate import (
    LLMPlanner,
    PlannerSchemaValidationError,
    compute_blast_set_validity,
    generate_candidates_with_retry,
    normalise_plan,
    normalise_plans,
    resolve_commit_ref,
    resolve_workload_name,
    validate_and_parse_plans,
)

FIXED_NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def _make_context(
    nodes: list[str] | None = None,
    edges: list[DependencyEdge] | None = None,
    deploys: list[DeployRef] | None = None,
) -> IncidentContext:
    now = FIXED_NOW
    graph_nodes = nodes or ["edge-gateway", "auth-service", "data-service", "worker"]
    graph_edges = edges or [
        DependencyEdge(source="edge-gateway", target="auth-service"),
        DependencyEdge(source="edge-gateway", target="data-service"),
        DependencyEdge(source="data-service", target="worker"),
    ]
    recent_deploys = (
        deploys
        if deploys is not None
        else [
            DeployRef(
                commit_sha="c0ffee0123456789abcdef0123456789abcdef01",  # pragma: allowlist secret
                image_digests={
                    "edge-gateway": "sha256:edge0",
                    "data-service": "sha256:data0",
                    "auth-service": "sha256:auth0",
                    "worker": "sha256:worker0",
                },
                deployed_at=now,
                pr_number=101,
                contains_migration=False,
            ),
            DeployRef(
                commit_sha="deadbeef12345678abcdef0123456789abcdef02",  # pragma: allowlist secret
                image_digests={
                    "edge-gateway": "sha256:edge1",
                    "data-service": "",  # empty digest test
                },
                deployed_at=now,
                pr_number=100,
                contains_migration=True,
            ),
        ]
    )
    return IncidentContext(
        incident_id="inc_test_123",
        alert=Alert(
            alert_id="alt_1",
            source="pagerduty",
            title="High error rate on data-service",
            service="data-service",
            severity="critical",
            fired_at=now,
            raw={},
        ),
        signatures=[
            ErrorSignature(
                fingerprint="fp_1",
                message="Connection timeout",
                service="data-service",
                count=5,
                first_seen=now,
                last_seen=now,
            )
        ],
        metrics_window=MetricWindow(
            service="data-service",
            start_time=now,
            end_time=now,
            p99_latency_ms=350.0,
            error_rate=0.05,
            request_count=500,
        ),
        recent_deploys=recent_deploys,
        dependency_graph=DependencyGraphSnapshot(
            nodes=graph_nodes,
            edges=graph_edges,
            observed_at=now,
        ),
        inferred_failure_class=FailureClass.BAD_DEPLOY,
        gathered_at=now,
    )


class DummyDependencyGraph(DependencyGraph):
    """Stub DependencyGraph for protocol checking."""

    def __init__(self, nodes: list[str], reachable_map: dict[str, set[str]]) -> None:
        self._nodes = nodes
        self._reachable = reachable_map

    def dependents(self, service: str) -> set[str]:
        return self._reachable.get(service, set())

    def reachable_set(self, service: str) -> set[str]:
        if service == "error-service":
            raise RuntimeError("Graph reachability failure")
        if service not in self._reachable:
            raise KeyError(service)
        return self._reachable[service]

    def request_share(self, service: str) -> float:
        del service
        return 0.25

    def snapshot(self) -> DependencyGraphSnapshot:
        return DependencyGraphSnapshot(
            nodes=self._nodes,
            edges=[],
            observed_at=FIXED_NOW,
        )


# --- 1. Schema Parsing & Validation ---


def test_validate_and_parse_plans_success_from_str() -> None:
    raw = """
    {
      "plans": [
        {
          "action": "scale_workload",
          "params": {"workload": "data-service", "replica_delta": 2},
          "declared_blast_set": ["data-service"],
          "inverse": {
            "action": "scale_workload",
            "params": {"workload": "data-service", "replica_delta": -2},
            "declared_blast_set": ["data-service"],
            "rationale": "Scale back down"
          },
          "rationale": "Scale up replicas to handle queue build-up"
        }
      ]
    }
    """
    plans = validate_and_parse_plans(raw)
    assert len(plans) == 1
    assert plans[0].action == ActionType.SCALE_WORKLOAD
    assert plans[0].params.workload == "data-service"
    assert plans[0].params.replica_delta == 2
    assert plans[0].inverse is not None
    assert plans[0].inverse.params.replica_delta == -2


def test_validate_and_parse_plans_success_from_dict_and_object() -> None:
    candidate = PromptCandidatePlan(
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="edge-gateway"),
        declared_blast_set=[],
        inverse=None,
        rationale="Take no intervention",
    )
    resp = PlannerPromptResponse(plans=[candidate])

    # Test from object
    plans1 = validate_and_parse_plans(resp)
    assert len(plans1) == 1
    assert plans1[0].action == ActionType.NO_ACTION

    # Test from dict
    dict_payload = resp.model_dump()
    plans2 = validate_and_parse_plans(dict_payload)
    assert len(plans2) == 1
    assert plans2[0].action == ActionType.NO_ACTION


def test_validate_and_parse_plans_flag_and_config() -> None:
    cands = [
        PromptCandidatePlan(
            action=ActionType.DISABLE_FLAG,
            params=ActionParams(workload="data-service", flag_name="EXPERIMENTAL_QUERY"),
            declared_blast_set=["data-service"],
            inverse=PromptInversePlan(
                action=ActionType.DISABLE_FLAG,
                params=ActionParams(workload="data-service", flag_name="EXPERIMENTAL_QUERY"),
                declared_blast_set=["data-service"],
                rationale="Re-enable flag",
            ),
            rationale="Disable flag to mitigate regression",
        ),
        PromptCandidatePlan(
            action=ActionType.REVERT_CONFIG,
            params=ActionParams(workload="auth-service", config_key="CACHE_TTL", config_value="60"),
            declared_blast_set=["auth-service"],
            inverse=PromptInversePlan(
                action=ActionType.REVERT_CONFIG,
                params=ActionParams(
                    workload="auth-service", config_key="CACHE_TTL", config_value="0"
                ),
                declared_blast_set=["auth-service"],
                rationale="Revert config back",
            ),
            rationale="Revert cache TTL",
        ),
    ]
    plans = validate_and_parse_plans(PlannerPromptResponse(plans=cands))
    assert len(plans) == 2
    assert plans[0].action == ActionType.DISABLE_FLAG
    assert plans[1].action == ActionType.REVERT_CONFIG


def test_validate_and_parse_plans_unsupported_type() -> None:
    with pytest.raises(PlannerSchemaValidationError) as exc:
        validate_and_parse_plans(12345)  # type: ignore[arg-type]
    assert "Unsupported payload type" in str(exc.value)


def test_validate_and_parse_plans_empty_array() -> None:
    with pytest.raises(PlannerSchemaValidationError) as exc:
        validate_and_parse_plans('{"plans": []}')
    assert "empty plans array" in str(exc.value)


def test_validate_and_parse_plans_invalid_json_string() -> None:
    with pytest.raises(PlannerSchemaValidationError) as exc:
        validate_and_parse_plans("not a json string")
    assert "Schema validation failed during JSON parsing" in str(exc.value)


def test_validate_and_parse_plans_dict_validation_error() -> None:
    with pytest.raises(PlannerSchemaValidationError) as exc:
        validate_and_parse_plans({"plans": [{"action": "invalid_action"}]})
    assert "Schema validation failed on dictionary payload" in str(exc.value)


@pytest.mark.parametrize(
    "candidate, expected_err",
    [
        (
            PromptCandidatePlan(
                action=ActionType.RESTART_WORKLOAD,
                params=ActionParams(workload="data-service"),
                declared_blast_set=["data-service"],
                inverse=PromptInversePlan(
                    action=ActionType.RESTART_WORKLOAD,
                    params=ActionParams(workload="data-service"),
                    rationale="Restart again",
                ),
                rationale="   ",  # empty rationale
            ),
            "empty rationale",
        ),
        (
            PromptCandidatePlan(
                action=ActionType.NO_ACTION,
                params=ActionParams(workload="data-service"),
                declared_blast_set=[],
                inverse=PromptInversePlan(
                    action=ActionType.NO_ACTION,
                    params=ActionParams(workload="data-service"),
                    rationale="inv",
                ),
                rationale="Do nothing",
            ),
            "is no_action but declares non-null inverse",
        ),
        (
            PromptCandidatePlan(
                action=ActionType.NO_ACTION,
                params=ActionParams(workload="data-service"),
                declared_blast_set=["data-service"],  # non-empty blast
                inverse=None,
                rationale="Do nothing",
            ),
            "is no_action but declares non-empty blast set",
        ),
        (
            PromptCandidatePlan(
                action=ActionType.RESTART_WORKLOAD,
                params=ActionParams(workload="data-service"),
                declared_blast_set=["data-service"],
                inverse=None,  # missing inverse
                rationale="Restart service",
            ),
            "requires a non-null inverse plan",
        ),
        (
            PromptCandidatePlan(
                action=ActionType.RESTART_WORKLOAD,
                params=ActionParams(workload=""),  # empty workload
                declared_blast_set=[],
                inverse=PromptInversePlan(
                    action=ActionType.RESTART_WORKLOAD,
                    params=ActionParams(workload=""),
                    rationale="inv",
                ),
                rationale="Restart with empty workload",
            ),
            "requires non-empty workload",
        ),
        (
            PromptCandidatePlan(
                action=ActionType.ROLLBACK_DEPLOY,
                params=ActionParams(workload="data-service", target_commit=None),
                declared_blast_set=["data-service"],
                inverse=PromptInversePlan(
                    action=ActionType.ROLLBACK_DEPLOY,
                    params=ActionParams(workload="data-service", target_commit="c0ffee0"),
                    rationale="inv",
                ),
                rationale="Rollback deploy",
            ),
            "requires target_commit",
        ),
        (
            PromptCandidatePlan(
                action=ActionType.SCALE_WORKLOAD,
                params=ActionParams(workload="data-service", replica_delta=None),
                declared_blast_set=["data-service"],
                inverse=PromptInversePlan(
                    action=ActionType.SCALE_WORKLOAD,
                    params=ActionParams(workload="data-service", replica_delta=1),
                    rationale="inv",
                ),
                rationale="Scale workload",
            ),
            "requires non-zero replica_delta",
        ),
        (
            PromptCandidatePlan(
                action=ActionType.SCALE_WORKLOAD,
                params=ActionParams(workload="data-service", replica_delta=0),
                declared_blast_set=["data-service"],
                inverse=PromptInversePlan(
                    action=ActionType.SCALE_WORKLOAD,
                    params=ActionParams(workload="data-service", replica_delta=0),
                    rationale="inv",
                ),
                rationale="Scale workload with 0",
            ),
            "requires non-zero replica_delta",
        ),
        (
            PromptCandidatePlan(
                action=ActionType.DISABLE_FLAG,
                params=ActionParams(workload="data-service", flag_name=None),
                declared_blast_set=["data-service"],
                inverse=PromptInversePlan(
                    action=ActionType.DISABLE_FLAG,
                    params=ActionParams(workload="data-service", flag_name="flag"),
                    rationale="inv",
                ),
                rationale="Disable flag",
            ),
            "requires flag_name",
        ),
        (
            PromptCandidatePlan(
                action=ActionType.REVERT_CONFIG,
                params=ActionParams(workload="data-service", config_key=None, config_value="val"),
                declared_blast_set=["data-service"],
                inverse=PromptInversePlan(
                    action=ActionType.REVERT_CONFIG,
                    params=ActionParams(workload="data-service", config_key="k", config_value="v"),
                    rationale="inv",
                ),
                rationale="Revert config key missing",
            ),
            "requires config_key and config_value",
        ),
        (
            PromptCandidatePlan(
                action=ActionType.REVERT_CONFIG,
                params=ActionParams(workload="data-service", config_key="k", config_value=None),
                declared_blast_set=["data-service"],
                inverse=PromptInversePlan(
                    action=ActionType.REVERT_CONFIG,
                    params=ActionParams(workload="data-service", config_key="k", config_value="v"),
                    rationale="inv",
                ),
                rationale="Revert config val missing",
            ),
            "requires config_key and config_value",
        ),
    ],
)
def test_validate_candidate_schema_violations(
    candidate: PromptCandidatePlan, expected_err: str
) -> None:
    resp = PlannerPromptResponse(plans=[candidate])
    with pytest.raises(PlannerSchemaValidationError) as exc:
        validate_and_parse_plans(resp)
    assert expected_err in str(exc.value)


# --- 2. Commit Ref & Workload Resolution ---


def test_resolve_commit_ref() -> None:
    ctx = _make_context()
    deploys = ctx.recent_deploys
    full_sha = deploys[0].commit_sha

    # 1. Empty or None commit ref
    assert resolve_commit_ref(None, deploys) is None
    assert resolve_commit_ref("", deploys) is None
    assert resolve_commit_ref("   ", deploys) is None

    # 2. Exact match
    assert resolve_commit_ref(full_sha, deploys) == full_sha

    # 3. Short SHA prefix match
    short_sha = full_sha[:7]
    assert resolve_commit_ref(short_sha, deploys) == full_sha

    # 4. Ref longer than sha (e.g. tag or ref starting with sha)
    assert resolve_commit_ref(f"{full_sha}-tag", deploys) == full_sha

    # 5. Nonexistent commit
    assert resolve_commit_ref("nonexistent_commit_sha", deploys) is None

    # 6. With workload checking
    # Valid workload with non-empty digest
    assert resolve_commit_ref(short_sha, deploys, workload="data-service") == full_sha
    # Workload not in image_digests
    assert resolve_commit_ref(short_sha, deploys, workload="unlisted-service") is None
    # Workload has empty digest in deploy 1
    sha2 = deploys[1].commit_sha
    assert resolve_commit_ref(sha2[:7], deploys, workload="data-service") is None


def test_resolve_workload_name() -> None:
    valid = {"data-service", "edge-gateway", "auth-service"}
    assert resolve_workload_name("", valid) is None
    assert resolve_workload_name("data-service", valid) == "data-service"
    assert resolve_workload_name(" data-service ", valid) == "data-service"
    assert resolve_workload_name("unknown-service", valid) is None


# --- 3. Blast Set Validity ---


def test_compute_blast_set_validity_snapshot() -> None:
    ctx = _make_context()
    graph = ctx.dependency_graph
    # edge-gateway -> auth-service, edge-gateway -> data-service, data-service -> worker
    # Dependents (callers):
    # data-service callers = {edge-gateway}
    # worker callers = {data-service, edge-gateway}

    # 1. Valid blast set: target only
    assert compute_blast_set_validity(["data-service"], "data-service", graph) is True

    # 2. Valid blast set: target and its callers
    assert (
        compute_blast_set_validity(["data-service", "edge-gateway"], "data-service", graph) is True
    )

    # 3. Transitive callers of worker
    assert (
        compute_blast_set_validity(["worker", "data-service", "edge-gateway"], "worker", graph)
        is True
    )

    # 4. Service outside graph
    assert (
        compute_blast_set_validity(["data-service", "foreign-service"], "data-service", graph)
        is False
    )

    # 5. Target workload not in graph
    assert compute_blast_set_validity(["auth-service"], "foreign-service", graph) is False

    # 6. Service in graph but NOT caller of target
    # edge-gateway has NO callers (it is ingress).
    # auth-service is callee of edge-gateway, not caller.
    assert (
        compute_blast_set_validity(["edge-gateway", "auth-service"], "edge-gateway", graph) is False
    )

    # 7. Empty target workload
    assert compute_blast_set_validity([], "", graph) is True

    # 8. Diamond callers (exercises caller already in visited) and unknown target edge in snapshot
    diamond_snapshot = DependencyGraphSnapshot(
        nodes=["target", "mid1", "mid2", "top"],
        edges=[
            DependencyEdge(source="mid1", target="target"),
            DependencyEdge(source="mid2", target="target"),
            DependencyEdge(source="top", target="mid1"),
            DependencyEdge(source="top", target="mid2"),
            DependencyEdge(source="target", target="external-db"),  # external-db not in nodes
        ],
        observed_at=FIXED_NOW,
    )
    assert (
        compute_blast_set_validity(["target", "top", "mid1", "mid2"], "target", diamond_snapshot)
        is True
    )


def test_compute_blast_set_validity_protocol() -> None:
    nodes = ["edge-gateway", "data-service", "auth-service"]
    reachable = {
        "data-service": {"edge-gateway"},
        "edge-gateway": set(),
        "auth-service": {"edge-gateway"},
    }
    graph = DummyDependencyGraph(nodes=nodes, reachable_map=reachable)

    # Valid
    assert (
        compute_blast_set_validity(["data-service", "edge-gateway"], "data-service", graph) is True
    )

    # Target not in nodes
    assert compute_blast_set_validity(["data-service"], "missing-svc", graph) is False

    # Target not in reachable dict (raises exception in reachable_set)
    assert compute_blast_set_validity(["data-service"], "other-svc", graph) is False

    # Reachable set raises runtime error
    err_graph = DummyDependencyGraph(nodes=["error-service"], reachable_map={})
    assert compute_blast_set_validity(["error-service"], "error-service", err_graph) is False

    # Service not in graph
    assert (
        compute_blast_set_validity(["data-service", "nonexistent"], "data-service", graph) is False
    )

    # Service not in reachable set
    assert (
        compute_blast_set_validity(["data-service", "auth-service"], "data-service", graph) is False
    )


# --- 4. Normalise Plan (Single & Batch) ---


def test_normalise_plan_no_action() -> None:
    ctx = _make_context()
    cand = PromptCandidatePlan(
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="data-service"),
        declared_blast_set=[],
        inverse=None,
        rationale="No intervention",
    )
    plan = validate_and_parse_plans(PlannerPromptResponse(plans=[cand]))[0]

    # Valid no action
    norm = normalise_plan(plan, ctx)
    assert norm is not None
    assert norm.action == ActionType.NO_ACTION

    # No action with non-empty blast set (dropped)
    bad_blast_plan = plan.model_copy(update={"declared_blast_set": ["data-service"]})
    assert normalise_plan(bad_blast_plan, ctx) is None

    # No action with nonexistent workload (dropped)
    bad_wl_plan = plan.model_copy(update={"params": ActionParams(workload="nonexistent")})
    assert normalise_plan(bad_wl_plan, ctx) is None


def test_normalise_plan_rollback_deploy() -> None:
    ctx = _make_context()
    full_sha = ctx.recent_deploys[0].commit_sha
    short_sha = full_sha[:7]

    cand = PromptCandidatePlan(
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service", target_commit=short_sha),
        declared_blast_set=["data-service"],
        inverse=PromptInversePlan(
            action=ActionType.ROLLBACK_DEPLOY,
            params=ActionParams(workload="data-service", target_commit=full_sha),
            declared_blast_set=["data-service"],
            rationale="Roll forward",
        ),
        rationale="Rollback bad deploy",
    )
    plan = validate_and_parse_plans(PlannerPromptResponse(plans=[cand]))[0]

    # 1. Normal resolution: short SHA expands to full SHA
    norm = normalise_plan(plan, ctx)
    assert norm is not None
    assert norm.params.target_commit == full_sha
    assert norm.inverse is not None
    assert norm.inverse.params.target_commit == full_sha

    # 2. Nonexistent target commit -> dropped
    bad_commit_plan = plan.model_copy(
        update={"params": plan.params.model_copy(update={"target_commit": "nonexistent_sha"})}
    )
    assert normalise_plan(bad_commit_plan, ctx) is None

    # 3. Workload not in cluster -> dropped
    bad_wl_plan = plan.model_copy(
        update={"params": plan.params.model_copy(update={"workload": "nonexistent_service"})}
    )
    assert normalise_plan(bad_wl_plan, ctx) is None

    # 4. Target resource with nonexistent name -> dropped
    bad_res_plan = plan.model_copy(
        update={
            "target_resources": [
                ResourceRef(namespace="ust-twin", kind="Deployment", name="nonexistent")
            ]
        }
    )
    assert normalise_plan(bad_res_plan, ctx) is None

    # 5. Invalid blast set -> dropped
    bad_blast_plan = plan.model_copy(update={"declared_blast_set": ["unrelated-foreign-service"]})
    assert normalise_plan(bad_blast_plan, ctx) is None

    # 6. Inverse with invalid workload -> dropped
    assert plan.inverse is not None
    bad_inv_wl_plan = plan.model_copy(
        update={
            "inverse": plan.inverse.model_copy(
                update={
                    "params": plan.inverse.params.model_copy(update={"workload": "ghost-service"})
                }
            )
        }
    )
    assert normalise_plan(bad_inv_wl_plan, ctx) is None

    # 7. Inverse with unresolvable rollback commit -> dropped
    bad_inv_commit_plan = plan.model_copy(
        update={
            "inverse": plan.inverse.model_copy(
                update={
                    "params": plan.inverse.params.model_copy(
                        update={"target_commit": "bad_inv_sha"}
                    )
                }
            )
        }
    )
    assert normalise_plan(bad_inv_commit_plan, ctx) is None

    # 8. Plan with inverse is None
    cand_no_inv = RemediationPlan(
        plan_id="cand_no_inv",
        candidate_index=0,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload="data-service"),
        target_resources=[
            ResourceRef(namespace="ust-twin", kind="Deployment", name="data-service")
        ],
        declared_blast_set=["data-service"],
        inverse=None,
        rationale="Restart without inverse",
        origin="planner",
    )
    norm_no_inv = normalise_plan(cand_no_inv, ctx)
    assert norm_no_inv is not None
    assert norm_no_inv.inverse is None

    # 9. Inverse with empty workload (non-rollback)
    plan_inv_empty_wl = plan.model_copy(
        update={
            "inverse": plan.inverse.model_copy(
                update={
                    "action": ActionType.RESTART_WORKLOAD,
                    "params": plan.inverse.params.model_copy(update={"workload": ""}),
                }
            )
        }
    )
    norm_empty_wl = normalise_plan(plan_inv_empty_wl, ctx)
    assert norm_empty_wl is not None

    # 10. Inverse with action other than ROLLBACK_DEPLOY (e.g. SCALE_WORKLOAD)
    plan_inv_scale = plan.model_copy(
        update={
            "inverse": plan.inverse.model_copy(
                update={
                    "action": ActionType.SCALE_WORKLOAD,
                    "params": ActionParams(workload="data-service", replica_delta=1),
                }
            )
        }
    )
    norm_inv_scale = normalise_plan(plan_inv_scale, ctx)
    assert norm_inv_scale is not None
    assert norm_inv_scale.inverse is not None
    assert norm_inv_scale.inverse.action == ActionType.SCALE_WORKLOAD


def test_normalise_plans_batch_filtering_and_reindexing() -> None:
    ctx = _make_context()
    full_sha = ctx.recent_deploys[0].commit_sha

    # Create 3 plans:
    # Plan 0: valid rollback
    # Plan 1: invalid workload (should be dropped)
    # Plan 2: valid scale workload
    plans = [
        RemediationPlan(
            plan_id="raw_0",
            candidate_index=0,
            action=ActionType.ROLLBACK_DEPLOY,
            params=ActionParams(workload="data-service", target_commit=full_sha[:7]),
            target_resources=[
                ResourceRef(namespace="ust-twin", kind="Deployment", name="data-service")
            ],
            declared_blast_set=["data-service"],
            inverse=RemediationPlan(
                plan_id="raw_0_inv",
                candidate_index=0,
                action=ActionType.ROLLBACK_DEPLOY,
                params=ActionParams(workload="data-service", target_commit=full_sha),
                target_resources=[],
                declared_blast_set=[],
                inverse=None,
                rationale="inv",
                origin="planner",
            ),
            rationale="Rollback 0",
            origin="planner",
        ),
        RemediationPlan(
            plan_id="raw_1",
            candidate_index=1,
            action=ActionType.SCALE_WORKLOAD,
            params=ActionParams(workload="phantom-service", replica_delta=1),
            target_resources=[],
            declared_blast_set=["phantom-service"],
            inverse=None,
            rationale="Scale nonexistent",
            origin="planner",
        ),
        RemediationPlan(
            plan_id="raw_2",
            candidate_index=2,
            action=ActionType.SCALE_WORKLOAD,
            params=ActionParams(workload="edge-gateway", replica_delta=2),
            target_resources=[
                ResourceRef(namespace="ust-twin", kind="Deployment", name="edge-gateway")
            ],
            declared_blast_set=["edge-gateway"],
            inverse=RemediationPlan(
                plan_id="raw_2_inv",
                candidate_index=2,
                action=ActionType.SCALE_WORKLOAD,
                params=ActionParams(workload="edge-gateway", replica_delta=-2),
                target_resources=[],
                declared_blast_set=[],
                inverse=None,
                rationale="Scale back down",
                origin="planner",
            ),
            rationale="Scale gateway",
            origin="planner",
        ),
    ]

    normalised = normalise_plans(plans, ctx)
    assert len(normalised) == 2
    # Plan 0 retains index 0
    assert normalised[0].plan_id == "plan_cand_0"
    assert normalised[0].candidate_index == 0
    assert normalised[0].inverse is not None
    assert normalised[0].inverse.plan_id == "plan_cand_0_inv"
    assert normalised[0].inverse.candidate_index == 0
    assert normalised[0].params.target_commit == full_sha

    # Plan 2 becomes index 1
    assert normalised[1].plan_id == "plan_cand_1"
    assert normalised[1].candidate_index == 1
    assert normalised[1].params.workload == "edge-gateway"
    assert normalised[1].inverse is not None
    assert normalised[1].inverse.plan_id == "plan_cand_1_inv"
    assert normalised[1].inverse.candidate_index == 1


def test_normalise_plans_all_dropped() -> None:
    ctx = _make_context()
    plans = [
        RemediationPlan(
            plan_id="raw_bad",
            candidate_index=0,
            action=ActionType.SCALE_WORKLOAD,
            params=ActionParams(workload="nonexistent", replica_delta=1),
            target_resources=[],
            declared_blast_set=[],
            inverse=None,
            rationale="Bad plan",
            origin="planner",
        )
    ]
    assert normalise_plans(plans, ctx) == []


# --- 5. Retry Mechanism on Schema Failure ---


@pytest.mark.asyncio
async def test_generate_candidates_with_retry_success_first_attempt() -> None:
    ctx = _make_context()
    valid_json = """
    {
      "plans": [
        {
          "action": "restart_workload",
          "params": {"workload": "data-service"},
          "declared_blast_set": ["data-service"],
          "inverse": {
            "action": "restart_workload",
            "params": {"workload": "data-service"},
            "declared_blast_set": ["data-service"],
            "rationale": "Restart again"
          },
          "rationale": "Rolling restart to clear leak"
        }
      ]
    }
    """
    caller = AsyncMock(return_value=valid_json)

    plans = await generate_candidates_with_retry(
        llm_caller=caller, context=ctx, count=1, max_retries=2
    )
    assert len(plans) == 1
    assert plans[0].action == ActionType.RESTART_WORKLOAD
    assert caller.call_count == 1


@pytest.mark.asyncio
async def test_generate_candidates_with_retry_success_second_attempt() -> None:
    ctx = _make_context()
    invalid_json = '{"plans": [{"action": "invalid_action"}]}'
    valid_json = """
    {
      "plans": [
        {
          "action": "scale_workload",
          "params": {"workload": "data-service", "replica_delta": 1},
          "declared_blast_set": ["data-service"],
          "inverse": {
            "action": "scale_workload",
            "params": {"workload": "data-service", "replica_delta": -1},
            "declared_blast_set": ["data-service"],
            "rationale": "Scale down"
          },
          "rationale": "Scale up by 1"
        }
      ]
    }
    """
    caller = AsyncMock(side_effect=[invalid_json, valid_json])

    plans = await generate_candidates_with_retry(
        llm_caller=caller, context=ctx, count=1, max_retries=2
    )
    assert len(plans) == 1
    assert plans[0].action == ActionType.SCALE_WORKLOAD
    assert caller.call_count == 2

    # Verify that the second call received assistant response and error feedback
    second_call_messages = caller.call_args_list[1][0][0]
    assert len(second_call_messages) == 4  # system, user, assistant, user feedback
    assert second_call_messages[2]["role"] == "assistant"
    assert second_call_messages[3]["role"] == "user"
    assert "schema validation errors" in second_call_messages[3]["content"]


@pytest.mark.asyncio
async def test_generate_candidates_with_retry_exhaustion_raises() -> None:
    ctx = _make_context()
    invalid_json = "bad_json_not_parseable"
    caller = AsyncMock(return_value=invalid_json)

    with pytest.raises(PlannerError) as exc:
        await generate_candidates_with_retry(llm_caller=caller, context=ctx, count=1, max_retries=2)
    assert "failed schema validation after 2 retries" in str(exc.value)
    assert caller.call_count == 3  # Initial + 2 retries


# --- 6. LLMPlanner Class & OpenRouter Integration ---


@pytest.mark.asyncio
async def test_llm_planner_protocol_and_custom_caller() -> None:
    ctx = _make_context()
    valid_json = """
    {
      "plans": [
        {
          "action": "no_action",
          "params": {"workload": "data-service"},
          "declared_blast_set": [],
          "inverse": null,
          "rationale": "Do nothing"
        }
      ]
    }
    """
    custom_caller = AsyncMock(return_value=valid_json)
    planner = LLMPlanner(llm_caller=custom_caller, max_retries=2)
    assert isinstance(planner, Planner)

    plans = await planner.generate_candidates(ctx, count=1)
    assert len(plans) == 1
    assert plans[0].action == ActionType.NO_ACTION
    assert custom_caller.call_count == 1


@pytest.mark.asyncio
async def test_llm_planner_missing_api_key() -> None:
    ctx = _make_context()
    settings = Settings(
        secrets=SecretSettings(openrouter_api_key=None),
        cluster=ClusterSettings(),
        timeouts=TimeoutSettings(),
    )
    planner = LLMPlanner(settings=settings)

    with pytest.raises(PlannerError) as exc:
        await planner.generate_candidates(ctx)
    assert "OPENROUTER_API_KEY is not configured" in str(exc.value)


@pytest.mark.asyncio
async def test_llm_planner_openrouter_http_client_success() -> None:
    ctx = _make_context()
    settings = Settings(
        secrets=SecretSettings(openrouter_api_key="sk-or-test-key"),
        openrouter_base_url="https://openrouter.ai/api/v1",
        llm_model="anthropic/claude-3.5-sonnet",
        timeouts=TimeoutSettings(incident_seconds=30),
    )

    valid_llm_content = """
    {
      "plans": [
        {
          "action": "restart_workload",
          "params": {"workload": "auth-service"},
          "declared_blast_set": ["auth-service"],
          "inverse": {
            "action": "restart_workload",
            "params": {"workload": "auth-service"},
            "declared_blast_set": ["auth-service"],
            "rationale": "Restart inverse"
          },
          "rationale": "Restart auth service"
        }
      ]
    }
    """
    mock_response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": valid_llm_content,
                }
            }
        ]
    }

    mock_resp = httpx.Response(200, json=mock_response_data)
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=mock_resp)

    planner = LLMPlanner(settings=settings, client=mock_client)
    plans = await planner.generate_candidates(ctx, count=1)

    assert len(plans) == 1
    assert plans[0].action == ActionType.RESTART_WORKLOAD
    assert mock_client.post.call_count == 1

    # Verify request arguments
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "https://openrouter.ai/api/v1/chat/completions"
    assert call_args[1]["headers"]["Authorization"] == "Bearer sk-or-test-key"
    assert call_args[1]["json"]["model"] == "anthropic/claude-3.5-sonnet"


@pytest.mark.asyncio
async def test_llm_planner_openrouter_http_errors() -> None:
    ctx = _make_context()
    settings = Settings(
        secrets=SecretSettings(openrouter_api_key="sk-or-test"),
        timeouts=TimeoutSettings(incident_seconds=30),
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    planner = LLMPlanner(settings=settings, client=mock_client)

    # 1. Non-200 HTTP error
    mock_client.post = AsyncMock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(PlannerError) as exc1:
        await planner.generate_candidates(ctx)
    assert "OpenRouter API returned error status 500" in str(exc1.value)

    # 2. Missing choices array
    mock_client.post = AsyncMock(return_value=httpx.Response(200, json={"error": "no choices"}))
    with pytest.raises(PlannerError) as exc2:
        await planner.generate_candidates(ctx)
    assert "missing choices array" in str(exc2.value)

    # 3. Choice item is not dict
    mock_client.post = AsyncMock(return_value=httpx.Response(200, json={"choices": ["not_a_dict"]}))
    with pytest.raises(PlannerError) as exc3:
        await planner.generate_candidates(ctx)
    assert "choice item is not an object" in str(exc3.value)

    # 4. Content is not string
    mock_client.post = AsyncMock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": 123}}]})
    )
    with pytest.raises(PlannerError) as exc4:
        await planner.generate_candidates(ctx)
    assert "content is not a string" in str(exc4.value)


@pytest.mark.asyncio
async def test_llm_planner_default_client_context_manager() -> None:
    ctx = _make_context()
    settings = Settings(
        secrets=SecretSettings(openrouter_api_key="sk-or-test"),
        timeouts=TimeoutSettings(incident_seconds=30),
    )
    planner = LLMPlanner(settings=settings)

    valid_json = """
    {
      "plans": [
        {
          "action": "no_action",
          "params": {"workload": "data-service"},
          "declared_blast_set": [],
          "inverse": null,
          "rationale": "No action"
        }
      ]
    }
    """
    mock_resp = httpx.Response(
        200,
        json={"choices": [{"message": {"content": valid_json}}]},
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        plans = await planner.generate_candidates(ctx)
        assert len(plans) == 1
        assert plans[0].action == ActionType.NO_ACTION
