"""Deterministic fake playbook library implementation."""

from understudy.contracts.enums import ActionType
from understudy.contracts.incident import IncidentContext
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.playbook.api import PlaybookLibrary


class FakePlaybookLibrary(PlaybookLibrary):
    """Deterministic playbook retriever and outcome recorder."""

    def __init__(self, candidate: RemediationPlan | None = None) -> None:
        self._candidate = candidate or RemediationPlan(
            plan_id="plan_playbook_001",
            candidate_index=3,
            action=ActionType.REVERT_CONFIG,
            params=ActionParams(
                workload="data-service",
                config_key="MAX_POOL_SIZE",
                config_value="20",
            ),
            target_resources=[
                ResourceRef(namespace="ust-twin", kind="ConfigMap", name="data-config")
            ],
            declared_blast_set=["data-service"],
            inverse=RemediationPlan(
                plan_id="plan_playbook_inv_001",
                candidate_index=3,
                action=ActionType.REVERT_CONFIG,
                params=ActionParams(
                    workload="data-service",
                    config_key="MAX_POOL_SIZE",
                    config_value="10",
                ),
                target_resources=[
                    ResourceRef(namespace="ust-twin", kind="ConfigMap", name="data-config")
                ],
                declared_blast_set=["data-service"],
                inverse=None,
                rationale="Revert pool size change",
                origin="playbook",
            ),
            rationale="Revert connection pool configuration drift from prior incident playbook",
            origin="playbook",
            playbook_id="pb_known_drift_01",
        )
        self.recorded_outcomes: list[dict[str, object]] = []

    async def retrieve_candidate(self, incident: IncidentContext) -> RemediationPlan | None:
        """Return candidate playbook if available."""
        _ = incident
        return self._candidate

    async def record_outcome(self, playbook_id: str, success: bool, evidence_run_id: str) -> None:
        """Record outcome."""
        self.recorded_outcomes.append(
            {"playbook_id": playbook_id, "success": success, "evidence_run_id": evidence_run_id}
        )
