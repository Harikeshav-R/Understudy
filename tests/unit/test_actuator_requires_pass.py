"""Unit tests for Invariant K10 (Actuation Authorisation) runtime invariant.

Enforces:
- docs/03-invariants.md §3.4 K10
- AGENTS.md §6.4 test_actuator_requires_pass_verdict (must never be deleted or weakened)
- The actuator applies to ust-prod only when holding a KernelVerdict with verdict == PASS
  whose plan_id matches the plan it is applying and whose age is under 60 seconds.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from understudy.actuator.production import ProductionActuator
from understudy.common.clock import FrozenClock
from understudy.common.errors import ActuationError
from understudy.contracts.enums import ActionType, InvariantTier, KernelVerdictType
from understudy.contracts.kernel import InvariantResult, KernelVerdict
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef

FIXED_NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


def _make_plan(plan_id: str = "plan_k10_test") -> RemediationPlan:
    return RemediationPlan(
        plan_id=plan_id,
        candidate_index=0,
        action=ActionType.RESTART_WORKLOAD,
        params=ActionParams(workload="data-service"),
        target_resources=[
            ResourceRef(namespace="ust-prod", kind="Deployment", name="data-service")
        ],
        declared_blast_set=["data-service"],
        rationale="test restart",
        origin="planner",
    )


def _make_verdict(
    plan_id: str = "plan_k10_test",
    verdict: KernelVerdictType = KernelVerdictType.PASS,
    evaluated_at: datetime | None = FIXED_NOW,
    human_reason: str = "ok",
) -> KernelVerdict:
    return KernelVerdict(
        incident_id="inc_k10_test",
        plan_id=plan_id,
        verdict=verdict,
        results=[
            InvariantResult(
                invariant_id="K1",
                tier=InvariantTier.PROOF,
                satisfied=True,
                reason="Discharged unsat by solver",
            )
        ],
        missing_facts=[],
        solver_ms=20.0,
        human_reason=human_reason,
        evaluated_at=evaluated_at,
    )


@pytest.mark.asyncio
async def test_actuator_requires_pass_verdict() -> None:
    """Assert Invariant K10: production actuation requires valid, fresh PASS verdict."""
    clock = FrozenClock(initial_time=FIXED_NOW)
    mock_applier = AsyncMock()
    mock_applier.apply.return_value = True

    actuator = ProductionActuator(
        applier=mock_applier,
        probe=None,
        run_store=None,
        clock=clock,
        enforce_caller_guard=False,
    )

    plan = _make_plan("plan_k10_test")

    # 1. Positive case: valid PASS verdict within 60 seconds succeeds
    pass_verdict = _make_verdict("plan_k10_test", KernelVerdictType.PASS, evaluated_at=FIXED_NOW)
    success = await actuator.apply_to_production(plan, pass_verdict)
    assert success is True
    assert mock_applier.apply.called

    # 2. Negative case: VETO verdict raises ActuationError asserting K10
    veto_verdict = _make_verdict(
        "plan_k10_test",
        KernelVerdictType.VETO,
        evaluated_at=FIXED_NOW,
        human_reason="VETO: Invariant K3 violated",
    )
    with pytest.raises(ActuationError) as exc_info:
        await actuator.apply_to_production(plan, veto_verdict)
    assert "K10" in str(exc_info.value)
    assert "PASS" in str(exc_info.value)
    assert exc_info.value.details.get("invariant") == "K10"

    # 3. Negative case: UNCERTAIN verdict raises ActuationError asserting K10
    uncertain_verdict = _make_verdict(
        "plan_k10_test",
        KernelVerdictType.UNCERTAIN,
        evaluated_at=FIXED_NOW,
        human_reason="UNCERTAIN: missing facts",
    )
    with pytest.raises(ActuationError) as exc_info:
        await actuator.apply_to_production(plan, uncertain_verdict)
    assert "K10" in str(exc_info.value)
    assert "PASS" in str(exc_info.value)
    assert exc_info.value.details.get("invariant") == "K10"

    # 4. Negative case: Mismatched plan_id raises ActuationError asserting K10
    mismatch_verdict = _make_verdict(
        "plan_different_id",
        KernelVerdictType.PASS,
        evaluated_at=FIXED_NOW,
    )
    with pytest.raises(ActuationError) as exc_info:
        await actuator.apply_to_production(plan, mismatch_verdict)
    assert "K10" in str(exc_info.value)
    assert "plan_id mismatch" in str(exc_info.value)
    assert exc_info.value.details.get("invariant") == "K10"

    # 5. Negative case: Stale verdict (>= 60 seconds) raises ActuationError asserting K10
    stale_evaluated_at = FIXED_NOW - timedelta(seconds=61)
    stale_verdict = _make_verdict(
        "plan_k10_test",
        KernelVerdictType.PASS,
        evaluated_at=stale_evaluated_at,
    )
    with pytest.raises(ActuationError) as exc_info:
        await actuator.apply_to_production(plan, stale_verdict)
    assert "K10" in str(exc_info.value)
    assert "stale" in str(exc_info.value)
    assert exc_info.value.details.get("invariant") == "K10"

    # 6. Boundary case: Verdict at exactly 59.9 seconds succeeds
    fresh_evaluated_at = FIXED_NOW - timedelta(seconds=59.9)
    fresh_verdict = _make_verdict(
        "plan_k10_test",
        KernelVerdictType.PASS,
        evaluated_at=fresh_evaluated_at,
    )
    assert await actuator.apply_to_production(plan, fresh_verdict) is True
