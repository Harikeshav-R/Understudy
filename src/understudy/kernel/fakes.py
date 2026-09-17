"""Deterministic fake safety kernel and K8s fact source implementations."""

from collections.abc import Mapping

from understudy.common.clock import Clock, resolve_clock
from understudy.contracts.enums import InvariantTier, KernelVerdictType
from understudy.contracts.kernel import Fact, InvariantResult, KernelVerdict
from understudy.contracts.plan import RemediationPlan
from understudy.kernel.api import K8sFactSource, SafetyKernel


class FakeK8sFactSource(K8sFactSource):
    """Deterministic in-memory K8s fact source for testing and local runs."""

    def __init__(
        self,
        replicas: Mapping[str, int] | None = None,
        healthy_replicas: Mapping[str, int] | None = None,
        egress_policy_present: bool = True,
    ) -> None:
        self.replicas: dict[str, int] = (
            dict(replicas)
            if replicas is not None
            else {
                "data-service": 2,
                "auth-service": 2,
                "edge-gateway": 2,
                "worker": 1,
            }
        )
        self.healthy_replicas: dict[str, int] = (
            dict(healthy_replicas) if healthy_replicas is not None else dict(self.replicas)
        )
        self.egress_policy_present = egress_policy_present

    async def get_workload_replicas(self, namespace: str, service: str) -> tuple[int, int] | None:
        _ = namespace
        if service not in self.replicas:
            return None
        return self.replicas[service], self.healthy_replicas.get(service, self.replicas[service])

    async def check_egress_policy_present(self, namespace: str) -> bool:
        _ = namespace
        return self.egress_policy_present


class FakeSafetyKernel(SafetyKernel):
    """Deterministic formal safety kernel fake."""

    def __init__(
        self,
        force_verdict: KernelVerdictType | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.force_verdict = force_verdict
        self.clock: Clock = resolve_clock(clock)

    async def verify(self, plan: RemediationPlan, facts: list[Fact]) -> KernelVerdict:
        """Evaluate invariants for a plan against facts."""
        _ = facts
        verdict_type = self.force_verdict or KernelVerdictType.PASS
        now = self.clock.now()

        if verdict_type == KernelVerdictType.PASS:
            results = [
                InvariantResult(
                    invariant_id="K1",
                    tier=InvariantTier.PROOF,
                    satisfied=True,
                    reason="Proof discharged unsat by solver",
                ),
                InvariantResult(
                    invariant_id="K2",
                    tier=InvariantTier.PROOF,
                    satisfied=True,
                    reason="All targets within authorized namespace",
                ),
            ]
            missing: list[str] = []
            human_reason = "All PROOF invariants verified by safety kernel."
        elif verdict_type == KernelVerdictType.VETO:
            results = [
                InvariantResult(
                    invariant_id="K3",
                    tier=InvariantTier.PROOF,
                    satisfied=False,
                    unsat_core=["target_commit < last_migration"],
                    reason="Rollback traverses schema migration boundary",
                )
            ]
            missing = []
            human_reason = "VETO: Invariant K3 violated (migration boundary)."
        else:
            results = []
            missing = ["replicas[data-service]"]
            human_reason = "UNCERTAIN: Missing required facts for verification."

        return KernelVerdict(
            incident_id="inc_fake",
            plan_id=plan.plan_id,
            verdict=verdict_type,
            results=results,
            missing_facts=missing,
            solver_ms=15.0,
            human_reason=human_reason,
            evaluated_at=now,
        )


__all__ = [
    "FakeK8sFactSource",
    "FakeSafetyKernel",
]
