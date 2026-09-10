"""Notification component protocol interfaces."""

from typing import Protocol, runtime_checkable

from understudy.contracts.evidence import CandidateEvidence, TournamentResult
from understudy.contracts.kernel import KernelVerdict
from understudy.contracts.plan import RemediationPlan


@runtime_checkable
class Notifier(Protocol):
    """External notification channels (Slack, PagerDuty)."""

    async def notify_slack(
        self,
        incident_id: str,
        message: str,
        plan: RemediationPlan | None = None,
        result: TournamentResult | None = None,
        verdict: KernelVerdict | None = None,
    ) -> None:
        """Post a structured incident update to Slack."""
        raise NotImplementedError

    async def escalate_pagerduty(
        self,
        incident_id: str,
        reason: str,
        partial_evidence: list[CandidateEvidence] | None = None,
    ) -> None:
        """Escalate an unresolvable or vetoed incident to PagerDuty on-call."""
        raise NotImplementedError
