"""Deterministic fake notifier implementation."""

from typing import Any

from understudy.contracts.evidence import CandidateEvidence, TournamentResult
from understudy.contracts.incident import IncidentContext
from understudy.contracts.kernel import KernelVerdict
from understudy.contracts.plan import RemediationPlan
from understudy.notify.api import Notifier


class FakeNotifier(Notifier):
    """Deterministic in-memory notification recorder."""

    def __init__(self) -> None:
        self.slack_posts: list[dict[str, Any]] = []
        self.pagerduty_escalations: list[dict[str, Any]] = []

    async def notify_slack(
        self,
        incident_id: str,
        message: str,
        plan: RemediationPlan | None = None,
        result: TournamentResult | None = None,
        verdict: KernelVerdict | None = None,
        *,
        context: IncidentContext | None = None,
        plans: list[RemediationPlan] | None = None,
        evidence: list[CandidateEvidence] | None = None,
        prod_outcome: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Record Slack post."""
        self.slack_posts.append(
            {
                "incident_id": incident_id,
                "message": message,
                "plan": plan,
                "result": result,
                "verdict": verdict,
                "context": context,
                "plans": plans,
                "evidence": evidence,
                "prod_outcome": prod_outcome,
                "run_id": run_id,
            }
        )

    async def escalate_pagerduty(
        self,
        incident_id: str,
        reason: str,
        partial_evidence: list[CandidateEvidence] | None = None,
    ) -> None:
        """Record PagerDuty escalation."""
        self.pagerduty_escalations.append(
            {
                "incident_id": incident_id,
                "reason": reason,
                "partial_evidence": partial_evidence or [],
            }
        )
