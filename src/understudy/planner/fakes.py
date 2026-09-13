"""Deterministic fake remediation planner implementation."""

from understudy.contracts.enums import ActionType
from understudy.contracts.incident import IncidentContext
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.planner.api import Planner
from understudy.planner.inverse import synthesize_inverse
from understudy.planner.validate import ensure_no_action_candidate


class FakePlanner(Planner):
    """Deterministic candidate plan generator."""

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed

    async def generate_candidates(
        self, context: IncidentContext, count: int = 3
    ) -> list[RemediationPlan]:
        """Generate deterministic candidate plans."""
        service = context.alert.service or "data-service"

        # Candidate 0: Rollback deploy
        plan_0_base = RemediationPlan(
            plan_id="plan_cand_0",
            candidate_index=0,
            action=ActionType.ROLLBACK_DEPLOY,
            params=ActionParams(workload=service, target_commit="c0ffee0"),
            target_resources=[ResourceRef(namespace="ust-twin", kind="Deployment", name=service)],
            declared_blast_set=[service],
            inverse=None,
            rationale="Rollback recent deployment to last known stable commit",
            origin="planner",
        )
        plan_0 = plan_0_base.model_copy(
            update={
                "inverse": synthesize_inverse(
                    plan_0_base, context=context, current_commit="c0ffee1"
                )
            }
        )

        # Candidate 1: Scale workload
        replica_delta = 1 + (self.seed % 3)
        plan_1_base = RemediationPlan(
            plan_id="plan_cand_1",
            candidate_index=1,
            action=ActionType.SCALE_WORKLOAD,
            params=ActionParams(workload=service, replica_delta=replica_delta),
            target_resources=[ResourceRef(namespace="ust-twin", kind="Deployment", name=service)],
            declared_blast_set=[service],
            inverse=None,
            rationale="Scale up workload replicas to handle traffic load",
            origin="planner",
        )
        plan_1 = plan_1_base.model_copy(
            update={"inverse": synthesize_inverse(plan_1_base, context=context)}
        )

        # Candidate 2: No action
        plan_2 = RemediationPlan(
            plan_id="plan_cand_2",
            candidate_index=2,
            action=ActionType.NO_ACTION,
            params=ActionParams(workload=service),
            target_resources=[],
            declared_blast_set=[],
            inverse=None,
            rationale="Maintain current state without automated mutation",
            origin="planner",
        )

        all_candidates = [plan_0, plan_1, plan_2]
        selected = all_candidates[:count]
        return ensure_no_action_candidate(selected, context=context)
