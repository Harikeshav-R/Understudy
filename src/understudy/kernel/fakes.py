"""Deterministic fake safety kernel implementation."""

from understudy.contracts.enums import InvariantTier, KernelVerdictType
from understudy.contracts.kernel import Fact, InvariantResult, KernelVerdict
from understudy.contracts.plan import RemediationPlan
from understudy.kernel.api import SafetyKernel


class FakeSafetyKernel(SafetyKernel):
    """Deterministic formal safety kernel fake."""

    def __init__(self, force_verdict: KernelVerdictType | None = None) -> None:
        self.force_verdict = force_verdict

    async def verify(self, plan: RemediationPlan, facts: list[Fact]) -> KernelVerdict:
        """Evaluate invariants for a plan against facts."""
        _ = facts
        verdict_type = self.force_verdict or KernelVerdictType.PASS

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
        )
