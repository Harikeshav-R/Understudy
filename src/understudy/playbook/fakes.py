"""Deterministic fake playbook library implementation."""

from understudy.contracts.enums import ActionType
from understudy.contracts.incident import IncidentContext
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.playbook.api import PlaybookLibrary
from understudy.playbook.retriever import PlaybookMatchResult

_UNSET_CANDIDATE = object()


class FakePlaybookLibrary(PlaybookLibrary):
    """Deterministic playbook retriever and outcome recorder."""

    def __init__(self, candidate: RemediationPlan | object | None = _UNSET_CANDIDATE) -> None:
        if candidate is _UNSET_CANDIDATE:
            self._candidate: RemediationPlan | None = RemediationPlan(
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
                    playbook_id="pb_known_drift_01",
                ),
                rationale="Revert connection pool configuration drift from prior incident playbook",
                origin="playbook",
                playbook_id="pb_known_drift_01",
            )
        else:
            self._candidate = candidate if isinstance(candidate, RemediationPlan) else None
        self.recorded_outcomes: list[dict[str, object]] = []
        self.written_playbooks: list[dict[str, object]] = []

    async def retrieve_candidate(self, incident: IncidentContext) -> RemediationPlan | None:
        """Return candidate playbook if available."""
        _ = incident
        return self._candidate

    async def match_playbook(
        self,
        context: IncidentContext,
        top_k: int = 3,
    ) -> PlaybookMatchResult:
        """Return deterministic match result for CLI or inspection."""
        _ = top_k
        if self._candidate is None:
            return PlaybookMatchResult(
                matched=False,
                confirmation_reason="No candidate playbooks matched in fake library",
                signature_text="",
                candidates_evaluated=0,
            )

        fc_val = (
            context.inferred_failure_class.value
            if context.inferred_failure_class is not None
            else "unknown"
        )
        return PlaybookMatchResult(
            matched=True,
            plan=self._candidate,
            playbook_id=self._candidate.playbook_id or "pb_known_drift_01",
            similarity=0.92,
            confirmation_reason=(
                "Matches known incident pattern in fake playbook library; confirmed safe."
            ),
            confidence=0.95,
            signature_text=f"failure_class: {fc_val}",
            candidates_evaluated=1,
        )

    async def record_outcome(self, playbook_id: str, success: bool, evidence_run_id: str) -> None:
        """Record outcome."""
        self.recorded_outcomes.append(
            {"playbook_id": playbook_id, "success": success, "evidence_run_id": evidence_run_id}
        )

    async def record_resolved_run(
        self,
        context: IncidentContext,
        plan: RemediationPlan,
        run_id: str,
        origin: str = "incident",
    ) -> str:
        """Record resolved run in fake playbook library."""
        pb_id = plan.playbook_id or f"pb_fake_{len(self.written_playbooks) + 1}"
        self.written_playbooks.append(
            {
                "playbook_id": pb_id,
                "context": context,
                "plan": plan,
                "run_id": run_id,
                "origin": origin,
            }
        )
        return pb_id
