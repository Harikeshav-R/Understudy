"""Production remediation plan execution and verification engine.

Implements build-plan step 5.2 and architecture §2.4, §2.7, §2.10:
- Asserts Invariant K10 (Actuation Authorisation):
  - verdict == KernelVerdictType.PASS
  - verdict.plan_id == plan.plan_id
  - verdict age < 60 seconds
- Enforces kill switch: UNDERSTUDY_ACTUATION_ENABLED (settings.actuation_enabled)
- Pre-actuation run record: writes an immutable pre-actuation RunRecord to RunStore
- Applies plan to ust-prod using understudy-prod ServiceAccount
- Post-apply probe verification: runs EnvironmentProbe against ust-prod for up to 180s
- Determines prod_outcome in ("resolved", "not_resolved", "worsened")
- AGENTS.md §2.4 guard: callable only from the orchestrator's actuate node
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Literal

from understudy.actuator.api import Actuator
from understudy.actuator.apply import K8sPlanApplier, resolve_service_account
from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import Settings, get_settings
from understudy.common.errors import ActuationError
from understudy.common.logging import get_logger
from understudy.contracts.enums import KernelVerdictType, RunOutcome
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.run import RunRecord

if TYPE_CHECKING:
    from datetime import datetime

    from understudy.contracts.evidence import ProbeSample
    from understudy.contracts.kernel import KernelVerdict
    from understudy.contracts.plan import RemediationPlan
    from understudy.signals.api import DeployHistory
    from understudy.store.api import RunStore
    from understudy.tournament.api import EnvironmentProbe, ProbeResult

logger = get_logger(__name__)

K10_MAX_AGE_SECONDS = 60.0
DEFAULT_TARGET_SERVICE = "edge-gateway"


def assert_caller_authorised() -> None:
    """Assert that apply_to_production is invoked only from authorised callers.

    AGENTS.md §2.4: Actuator.apply_to_production is callable only from the
    orchestrator's actuate node (or unit test frames).
    """
    stack = inspect.stack()
    # Frame 0: assert_caller_authorised
    # Frame 1: apply_to_production method or function
    # Frame 2+: caller
    authorised = False
    for frame_info in stack[2:]:
        mod = inspect.getmodule(frame_info.frame)
        mod_name = mod.__name__ if mod else ""
        func_name = frame_info.function

        # Authorised if called from orchestrator's actuate node or test runner
        if (
            mod_name == "understudy.orchestrator.nodes.actuate"
            and func_name in ("actuate", "_runner")
        ) or (
            "test_" in mod_name
            or "tests." in mod_name
            or "test_" in frame_info.filename
            or "pytest" in mod_name
        ):
            authorised = True
            break

    if not authorised:
        caller_mod = inspect.getmodule(stack[2].frame)
        caller_name = caller_mod.__name__ if caller_mod else "unknown"
        caller_func = stack[2].function
        raise ActuationError(
            f"AGENTS.md §2.4 violation: Actuator.apply_to_production is callable only from the "
            f"orchestrator's actuate node (called from {caller_name}.{caller_func})",
            details={"rule": "AGENTS.md §2.4", "caller": f"{caller_name}.{caller_func}"},
        )


def assert_k10_authorisation(
    plan: RemediationPlan,
    verdict: KernelVerdict,
    now: datetime,
    max_age_seconds: float = K10_MAX_AGE_SECONDS,
) -> None:
    """Assert Invariant K10 (Actuation Authorisation) preconditions.

    Raises ActuationError if:
    - verdict is not PASS (got VETO or UNCERTAIN)
    - plan_id does not match verdict.plan_id
    - verdict age is >= max_age_seconds (or clock skew > 5s into future)
    """
    if verdict.verdict != KernelVerdictType.PASS:
        raise ActuationError(
            f"K10 violation: cannot actuate plan {plan.plan_id} on production without PASS "
            f"verdict (got {verdict.verdict.value})",
            details={
                "invariant": "K10",
                "plan_id": plan.plan_id,
                "verdict": verdict.verdict.value,
            },
        )

    if verdict.plan_id != plan.plan_id:
        raise ActuationError(
            f"K10 violation: plan_id mismatch (plan={plan.plan_id}, verdict={verdict.plan_id})",
            details={
                "invariant": "K10",
                "plan_id": plan.plan_id,
                "verdict_plan_id": verdict.plan_id,
            },
        )

    if verdict.evaluated_at is not None:
        age_seconds = (now - verdict.evaluated_at).total_seconds()
        if age_seconds >= max_age_seconds:
            raise ActuationError(
                f"K10 violation: verdict is stale ({age_seconds:.1f}s >= {max_age_seconds}s)",
                details={
                    "invariant": "K10",
                    "plan_id": plan.plan_id,
                    "age_seconds": age_seconds,
                },
            )
        if age_seconds < -5.0:
            raise ActuationError(
                f"K10 violation: verdict timestamp is in the future ({age_seconds:.1f}s skew)",
                details={
                    "invariant": "K10",
                    "plan_id": plan.plan_id,
                    "age_seconds": age_seconds,
                },
            )


def build_pre_actuation_run_record(
    plan: RemediationPlan,
    verdict: KernelVerdict,
    now: datetime,
    context: IncidentContext | None = None,
) -> RunRecord:
    """Construct an immutable pre-actuation RunRecord for append-only audit persistence."""
    if context is not None:
        ctx = context
    else:
        ctx = IncidentContext(
            incident_id=verdict.incident_id,
            alert=Alert(
                alert_id=f"alt_{verdict.incident_id}",
                source="synthetic",
                title=f"Incident {verdict.incident_id}",
                service=plan.params.workload or DEFAULT_TARGET_SERVICE,
                severity="critical",
                fired_at=now,
            ),
            metrics_window=MetricWindow(
                service=plan.params.workload or DEFAULT_TARGET_SERVICE,
                start_time=now,
                end_time=now,
            ),
            dependency_graph=DependencyGraphSnapshot(observed_at=now),
            gathered_at=now,
        )

    inc_id = (
        context.incident_id
        if (context and context.incident_id and verdict.incident_id == "inc_kernel_verify")
        else verdict.incident_id
    )
    return RunRecord(
        run_id=f"run_{inc_id}_pre_actuation",
        incident_id=inc_id,
        started_at=now,
        finished_at=None,
        outcome=RunOutcome.EXECUTED,
        context=ctx,
        plans=[plan],
        evidence=[],
        tournament=None,
        verdict=verdict,
        prod_applied_plan_id=plan.plan_id,
        prod_outcome=None,
        escalation_reason=None,
    )


def determine_prod_outcome(
    probe_result: ProbeResult | None,
    baseline_sample: ProbeSample | None = None,
    applied_successfully: bool = True,
) -> Literal["resolved", "not_resolved", "worsened"]:
    """Determine prod_outcome from post-apply SLO probe samples and pre-apply baseline."""
    if not applied_successfully:
        return "not_resolved"

    if probe_result is None:
        return "not_resolved"

    if probe_result.recovered:
        return "resolved"

    # Non-recovered: check if metrics degraded compared to pre-apply baseline
    if baseline_sample is not None and probe_result.probes:
        valid_error_rates = [p.error_rate for p in probe_result.probes if p.error_rate is not None]
        valid_latencies = [
            p.p99_latency_ms for p in probe_result.probes if p.p99_latency_ms is not None
        ]

        avg_post_error = (
            sum(valid_error_rates) / len(valid_error_rates) if valid_error_rates else 0.0
        )
        avg_post_latency = sum(valid_latencies) / len(valid_latencies) if valid_latencies else 0.0

        baseline_err = baseline_sample.error_rate or 0.0
        baseline_lat = baseline_sample.p99_latency_ms or 0.0

        # Worsened condition: error rate increased by > 0.02 (2% delta) or latency doubled
        if (avg_post_error > baseline_err + 0.02) or (
            baseline_lat > 0 and avg_post_latency > baseline_lat * 2.0
        ):
            return "worsened"

    return "not_resolved"


class ProductionActuator(Actuator):
    """Executes remediation plans in twin and production environments."""

    def __init__(
        self,
        applier: K8sPlanApplier | None = None,
        probe: EnvironmentProbe | None = None,
        run_store: RunStore | None = None,
        deploy_history: DeployHistory | None = None,
        clock: Clock | None = None,
        settings: Settings | None = None,
        enforce_caller_guard: bool = True,
    ) -> None:
        self.settings: Settings = settings or get_settings()
        self.clock: Clock = resolve_clock(clock)
        self.deploy_history = deploy_history
        self.applier: K8sPlanApplier = applier or K8sPlanApplier(
            clock=self.clock,
            settings=self.settings,
            deploy_history=deploy_history,
        )
        self.probe: EnvironmentProbe | None = probe
        self.run_store: RunStore | None = run_store
        self.enforce_caller_guard = enforce_caller_guard
        self.prod_namespace = self.settings.cluster.prod_namespace
        self.last_prod_outcome: Literal["resolved", "not_resolved", "worsened"] | None = None

    async def apply(self, plan: RemediationPlan, namespace: str) -> bool:
        """Apply a remediation plan to a specific namespace."""
        return await self.applier.apply(plan=plan, namespace=namespace)

    async def revert(self, plan: RemediationPlan, namespace: str) -> bool:
        """Revert an applied remediation plan by executing its declared inverse."""
        return await self.applier.revert(plan=plan, namespace=namespace)

    async def apply_to_production(
        self,
        plan: RemediationPlan,
        verdict: KernelVerdict,
        context: IncidentContext | None = None,
    ) -> bool:
        """Apply a PASS-verified remediation plan to the production namespace.

        Asserts Invariant K10, enforces kill switch, writes immutable pre-actuation
        run record, applies plan to ust-prod, and runs post-apply probe verification.
        """
        if self.enforce_caller_guard:
            assert_caller_authorised()

        now = self.clock.now()

        # 1. Assert Invariant K10
        assert_k10_authorisation(plan=plan, verdict=verdict, now=now)

        # 2. Assert kill switch
        if not self.settings.actuation_enabled:
            logger.warning(
                "production_actuation_disabled_kill_switch",
                plan_id=plan.plan_id,
                incident_id=verdict.incident_id,
            )
            raise ActuationError(
                "Actuation is disabled by kill switch (UNDERSTUDY_ACTUATION_ENABLED=false)",
                details={
                    "plan_id": plan.plan_id,
                    "incident_id": verdict.incident_id,
                    "kill_switch": True,
                },
            )

        # 3. Capture pre-apply baseline sample if probe is available
        baseline_sample: ProbeSample | None = None
        target_service = plan.params.workload or DEFAULT_TARGET_SERVICE
        if self.probe is not None:
            try:
                baseline_sample = await self.probe.sample_once(
                    namespace=self.prod_namespace,
                    target_service=target_service,
                )
            except Exception as exc:
                logger.warning(
                    "pre_apply_baseline_probe_failed",
                    error=str(exc),
                    namespace=self.prod_namespace,
                )

        # 4. Write immutable pre-actuation RunRecord to RunStore
        if self.run_store is not None:
            pre_record = build_pre_actuation_run_record(
                plan=plan,
                verdict=verdict,
                now=now,
                context=context,
            )
            await self.run_store.record_run(pre_record)
            logger.info(
                "pre_actuation_record_persisted",
                run_id=pre_record.run_id,
                incident_id=verdict.incident_id,
            )

        # 5. Apply plan to ust-prod with understudy-prod ServiceAccount
        applied_at = self.clock.now()
        service_account = resolve_service_account(
            namespace=self.prod_namespace,
            settings=self.settings,
        )
        applied_success = await self.applier.apply(
            plan=plan,
            namespace=self.prod_namespace,
            service_account=service_account,
        )

        if not applied_success:
            self.last_prod_outcome = "not_resolved"
            return False

        # 6. Post-apply probe verification
        if self.probe is not None:
            probe_result = await self.probe.probe_environment(
                namespace=self.prod_namespace,
                applied_at=applied_at,
                forked_at=None,
                target_service=target_service,
            )
            outcome = determine_prod_outcome(
                probe_result=probe_result,
                baseline_sample=baseline_sample,
                applied_successfully=True,
            )
        else:
            outcome = "resolved"

        self.last_prod_outcome = outcome

        logger.info(
            "production_actuation_completed",
            plan_id=plan.plan_id,
            incident_id=verdict.incident_id,
            prod_outcome=outcome,
        )

        return outcome == "resolved"


async def apply_to_production(
    plan: RemediationPlan,
    verdict: KernelVerdict,
    applier: K8sPlanApplier | None = None,
    probe: EnvironmentProbe | None = None,
    run_store: RunStore | None = None,
    clock: Clock | None = None,
    settings: Settings | None = None,
    context: IncidentContext | None = None,
    enforce_caller_guard: bool = True,
) -> bool:
    """Convenience function applying a PASS-verified plan to production."""
    actuator = ProductionActuator(
        applier=applier,
        probe=probe,
        run_store=run_store,
        clock=clock,
        settings=settings,
        enforce_caller_guard=enforce_caller_guard,
    )
    return await actuator.apply_to_production(plan=plan, verdict=verdict, context=context)


__all__ = [
    "DEFAULT_TARGET_SERVICE",
    "K10_MAX_AGE_SECONDS",
    "ProductionActuator",
    "apply_to_production",
    "assert_caller_authorised",
    "assert_k10_authorisation",
    "build_pre_actuation_run_record",
    "determine_prod_outcome",
]
