"""Composite notifier combining Slack posts and PagerDuty escalations."""

from understudy.contracts.evidence import CandidateEvidence, TournamentResult
from understudy.contracts.incident import IncidentContext
from understudy.contracts.kernel import KernelVerdict
from understudy.contracts.plan import RemediationPlan
from understudy.notify.api import Notifier
from understudy.notify.pagerduty import PagerDutyNotifier
from understudy.notify.slack import SlackNotifier


class CompositeNotifier(Notifier):
    """Unified notifier routing Slack updates and PagerDuty escalations."""

    def __init__(
        self,
        slack: Notifier | None = None,
        pagerduty: Notifier | None = None,
    ) -> None:
        self.slack = slack or SlackNotifier()
        self.pagerduty = pagerduty or PagerDutyNotifier()

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
        """Route Slack post to the configured Slack notifier."""
        await self.slack.notify_slack(
            incident_id=incident_id,
            message=message,
            plan=plan,
            result=result,
            verdict=verdict,
            context=context,
            plans=plans,
            evidence=evidence,
            prod_outcome=prod_outcome,
            run_id=run_id,
        )

    async def escalate_pagerduty(
        self,
        incident_id: str,
        reason: str,
        partial_evidence: list[CandidateEvidence] | None = None,
        *,
        context: IncidentContext | None = None,
        plans: list[RemediationPlan] | None = None,
        verdict: KernelVerdict | None = None,
        tournament: TournamentResult | None = None,
        urgency: str = "high",
        pd_incident_id: str | None = None,
    ) -> None:
        """Route escalation to the configured PagerDuty notifier."""
        await self.pagerduty.escalate_pagerduty(
            incident_id=incident_id,
            reason=reason,
            partial_evidence=partial_evidence,
            context=context,
            plans=plans,
            verdict=verdict,
            tournament=tournament,
            urgency=urgency,
            pd_incident_id=pd_incident_id,
        )


__all__ = ["CompositeNotifier"]
