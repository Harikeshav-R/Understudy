"""Deterministic fake remediation planner implementation."""

from understudy.contracts.enums import ActionType
from understudy.contracts.incident import IncidentContext
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.planner.api import Planner


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
        plan_0 = RemediationPlan(
            plan_id="plan_cand_0",
            candidate_index=0,
            action=ActionType.ROLLBACK_DEPLOY,
            params=ActionParams(workload=service, target_commit="c0ffee0"),
            target_resources=[ResourceRef(namespace="ust-twin", kind="Deployment", name=service)],
            declared_blast_set=[service],
            inverse=RemediationPlan(
                plan_id="plan_cand_0_inv",
                candidate_index=0,
                action=ActionType.ROLLBACK_DEPLOY,
                params=ActionParams(workload=service, target_commit="c0ffee1"),
                target_resources=[
                    ResourceRef(namespace="ust-twin", kind="Deployment", name=service)
                ],
                declared_blast_set=[service],
                inverse=None,
                rationale="Roll forward to prior commit",
                origin="planner",
            ),
            rationale="Rollback recent deployment to last known stable commit",
            origin="planner",
        )

        # Candidate 1: Scale workload
        plan_1 = RemediationPlan(
            plan_id="plan_cand_1",
            candidate_index=1,
            action=ActionType.SCALE_WORKLOAD,
            params=ActionParams(workload=service, replica_delta=1),
            target_resources=[ResourceRef(namespace="ust-twin", kind="Deployment", name=service)],
            declared_blast_set=[service],
            inverse=RemediationPlan(
                plan_id="plan_cand_1_inv",
                candidate_index=1,
                action=ActionType.SCALE_WORKLOAD,
                params=ActionParams(workload=service, replica_delta=-1),
                target_resources=[
                    ResourceRef(namespace="ust-twin", kind="Deployment", name=service)
                ],
                declared_blast_set=[service],
                inverse=None,
                rationale="Scale back down",
                origin="planner",
            ),
            rationale="Scale up workload replicas to handle traffic load",
            origin="planner",
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
        return all_candidates[:count]
