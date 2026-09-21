"""Slack notification client and reasoning post generator for Understudy.

Implements build-plan step 5.3 and ADR-026:
- Formats the 7 structured reasoning blocks:
  1. Incident Summary
  2. Candidate Table with Scores
  3. Decision Analysis (Why Winner Beat Others)
  4. Safety Kernel Verdict
  5. Action Taken (Production Actuation)
  6. Mirror Fidelity Line
  7. Link to Run Record
- Posts structured Block Kit messages via the Slack Web API (chat.postMessage).
"""

from collections.abc import Sequence
from typing import Any

import httpx

from understudy.common.config import get_settings
from understudy.common.errors import SlackNotificationError
from understudy.common.logging import get_logger
from understudy.contracts.enums import KernelVerdictType
from understudy.contracts.evidence import CandidateEvidence, TournamentResult
from understudy.contracts.incident import IncidentContext
from understudy.contracts.kernel import KernelVerdict
from understudy.contracts.plan import ActionParams, RemediationPlan
from understudy.notify.api import Notifier
from understudy.notify.formatting import build_candidate_table, build_decision_analysis

logger = get_logger(__name__)

SLACK_API_URL = "https://slack.com/api/chat.postMessage"


def _format_action_params(params: ActionParams) -> str:
    """Format ActionParams into a human-readable parameter string."""
    parts: list[str] = []
    if params.target_commit:
        short_sha = (
            params.target_commit[:7] if len(params.target_commit) >= 7 else params.target_commit
        )
        parts.append(f"commit={short_sha}")
    if params.replica_delta is not None:
        sign = "+" if params.replica_delta > 0 else ""
        parts.append(f"delta={sign}{params.replica_delta}")
    if params.flag_name:
        parts.append(f"flag={params.flag_name}")
    if params.config_key:
        val = params.config_value if params.config_value is not None else "revert"
        parts.append(f"config={params.config_key}:{val}")
    return ", ".join(parts) if parts else "default"


def build_slack_reasoning_blocks(
    incident_id: str,
    message: str = "",
    plan: RemediationPlan | None = None,
    result: TournamentResult | None = None,
    verdict: KernelVerdict | None = None,
    *,
    context: IncidentContext | None = None,
    plans: Sequence[RemediationPlan] | None = None,
    evidence: Sequence[CandidateEvidence] | None = None,
    prod_outcome: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Assemble the 7 Block Kit blocks for the Slack reasoning post.

    Blocks:
    1. Incident Summary
    2. Candidate Table with Scores
    3. Decision Analysis (Why Winner Beat Others)
    4. Safety Kernel Verdict
    5. Action Taken (Production Actuation)
    6. Mirror Fidelity Line
    7. Link to Run Record
    """
    blocks: list[dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # Block 1: Incident Summary
    # -------------------------------------------------------------------------
    service = "unknown-service"
    severity = "error"
    alert_title = message or f"Incident {incident_id}"
    failure_class = "unspecified"

    if context:
        service = context.alert.service
        severity = context.alert.severity.upper()
        alert_title = context.alert.title
        if context.inferred_failure_class:
            failure_class = context.inferred_failure_class.value
    elif plan and plan.params.workload:
        service = plan.params.workload

    status_str = (prod_outcome or "executed").upper()
    if prod_outcome == "resolved":
        header_text = f"✅ Understudy Incident Remediated: {service}"
    elif prod_outcome in ("not_resolved", "worsened"):
        header_text = f"❌ Understudy Remediation Failed: {service}"
    elif verdict and verdict.verdict == KernelVerdictType.VETO:
        header_text = f"🚫 Understudy Remediation Vetoed: {service}"
    else:
        header_text = f"⚠️ Understudy Incident Update: {service}"

    blocks.append(
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": header_text[:150],
                "emoji": True,
            },
        }
    )

    summary_fields = [
        {"type": "mrkdwn", "text": f"*Incident ID:*\n`{incident_id}`"},
        {"type": "mrkdwn", "text": f"*Service:*\n`{service}`"},
        {"type": "mrkdwn", "text": f"*Severity:*\n`{severity}`"},
        {"type": "mrkdwn", "text": f"*Failure Class:*\n`{failure_class}`"},
        {"type": "mrkdwn", "text": f"*Alert:*\n{alert_title[:100]}"},
        {"type": "mrkdwn", "text": f"*Production Status:*\n*{status_str}*"},
    ]
    blocks.append({"type": "section", "fields": summary_fields})
    blocks.append({"type": "divider"})

    # -------------------------------------------------------------------------
    # Block 2: Candidate Table with Scores
    # -------------------------------------------------------------------------
    table_text = build_candidate_table(plans=plans, result=result, evidence=evidence)
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Candidate Scoreboard:*\n```{table_text}```",
            },
        }
    )
    blocks.append({"type": "divider"})

    # -------------------------------------------------------------------------
    # Block 3: Decision Analysis (Why Winner Beat Others)
    # -------------------------------------------------------------------------
    analysis_text = build_decision_analysis(result=result, plans=plans, evidence=evidence)
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Decision Analysis:*\n{analysis_text}",
            },
        }
    )
    blocks.append({"type": "divider"})

    # -------------------------------------------------------------------------
    # Block 4: Safety Kernel Verdict
    # -------------------------------------------------------------------------
    if verdict:
        if verdict.verdict == KernelVerdictType.PASS:
            v_icon = "✅"
            v_title = "PASS"
        elif verdict.verdict == KernelVerdictType.VETO:
            v_icon = "🚫"
            v_title = "VETO"
        else:
            v_icon = "⚠️"
            v_title = "UNCERTAIN"

        satisfied_count = sum(1 for r in verdict.results if r.satisfied)
        total_count = len(verdict.results)
        solver_str = f"{verdict.solver_ms:.1f}ms"

        reason_snippet = (
            verdict.human_reason[:500]
            if verdict.human_reason
            else "No specific human explanation provided."
        )

        kernel_body = (
            f"{v_icon} *Safety Kernel Verdict: {v_title}*\n"
            f"• *Solver Latency:* {solver_str} (in-process Z3 SMT)\n"
            f"• *Invariants Verified:* {satisfied_count}/{total_count} satisfied\n"
            f"• *Reason:* {reason_snippet}"
        )
        if verdict.missing_facts:
            kernel_body += f"\n• *Missing Facts:* {', '.join(verdict.missing_facts)}"
    else:
        kernel_body = "⚖️ *Safety Kernel Verdict:* Not evaluated in this phase."

    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": kernel_body,
            },
        }
    )
    blocks.append({"type": "divider"})

    # -------------------------------------------------------------------------
    # Block 5: Action Taken (Production Actuation)
    # -------------------------------------------------------------------------
    if plan and verdict and verdict.verdict == KernelVerdictType.PASS:
        action_name = plan.action.value.upper()
        param_desc = _format_action_params(plan.params)
        inverse_name = plan.inverse.action.value.upper() if plan.inverse else "NONE"
        outcome_disp = prod_outcome or "executed"

        action_body = (
            f"⚡ *Production Actuation: {action_name} on `{plan.params.workload}`*\n"
            f"• *Parameters:* {param_desc}\n"
            f"• *Production Outcome:* `{outcome_disp}`\n"
            f"• *Inverse Plan:* `{inverse_name}` (synthesized & verified for instant reversion)"
        )
    elif plan and not verdict:
        # Dry-run or unverified execution
        action_body = (
            f"⚡ *Production Actuation: {plan.action.value.upper()} on `{plan.params.workload}`*\n"
            f"• *Outcome:* `{prod_outcome or 'executed'}`"
        )
    else:
        action_body = (
            "🛡️ *Production Actuation: NONE*\n"
            "Production (`ust-prod`) remained untouched because safety kernel vetoed or "
            "tournament was inconclusive."
        )

    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": action_body,
            },
        }
    )
    blocks.append({"type": "divider"})

    # -------------------------------------------------------------------------
    # Block 6: Mirror Fidelity Line
    # -------------------------------------------------------------------------
    if evidence:
        total_delivered = 0
        total_dropped = 0
        twin_drops: list[str] = []
        for e in evidence:
            if e.mirror_stats:
                total_delivered += e.mirror_stats.delivered
                total_dropped += e.mirror_stats.dropped
                twin_drops.append(f"{e.twin_id}: {e.mirror_stats.drop_ratio * 100:.2f}%")
        total_requests = total_delivered + total_dropped
        avg_drop = (total_dropped / total_requests * 100) if total_requests > 0 else 0.0

        all_under_ceiling = (
            all(e.mirror_stats.drop_ratio < 0.05 for e in evidence if e.mirror_stats)
            if any(e.mirror_stats for e in evidence)
            else True
        )
        ceiling_text = (
            "All under 5.0% ceiling." if all_under_ceiling else "Ceiling (5.0%) exceeded."
        )

        fidelity_text = (
            f"🪞 *Mirror Traffic Fidelity:* {total_delivered:,} reqs delivered "
            f"({total_dropped:,} dropped, avg drop {avg_drop:.2f}%). "
            f"Twins: [{', '.join(twin_drops)}]. {ceiling_text}"
        )
    else:
        fidelity_text = (
            "🪞 *Mirror Traffic Fidelity:* Baseline telemetry (no twin traffic recorded)."
        )

    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": fidelity_text}],
        }
    )

    # -------------------------------------------------------------------------
    # Block 7: Link to Run Record
    # -------------------------------------------------------------------------
    actual_run_id = run_id or f"run_{incident_id}"
    record_text = (
        f"📋 *Run Record:* `{actual_run_id}` | Append-only store (ADR-030) | "
        f"Inspect: `ust store get {actual_run_id}`"
    )
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": record_text}],
        }
    )

    return blocks


def build_slack_reasoning_text(
    incident_id: str,
    message: str = "",
    plan: RemediationPlan | None = None,
    result: TournamentResult | None = None,
    verdict: KernelVerdict | None = None,
    *,
    context: IncidentContext | None = None,
    prod_outcome: str | None = None,
    **kwargs: Any,
) -> str:
    """Build a concise fallback text summary for notifications and clients without Block Kit."""
    _ = (message, kwargs)
    service = context.alert.service if context else (plan.params.workload if plan else "service")
    status = prod_outcome or "executed"

    winner_desc = "none"
    if result and result.winner_plan_id:
        winner_desc = result.winner_plan_id

    verdict_desc = verdict.verdict.value.upper() if verdict else "N/A"
    return (
        f"[Understudy] Incident {incident_id} ({service}) - Status: {status.upper()} | "
        f"Winner: {winner_desc} | Kernel: {verdict_desc}"
    )


class SlackNotifier(Notifier):
    """Slack notification client for publishing incident reasoning posts."""

    def __init__(
        self,
        bot_token: str | None = None,
        channel_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._channel_id = channel_id
        self._http_client = http_client

    def _resolve_credentials(self) -> tuple[str, str]:
        """Resolve bot token and channel ID from arguments or settings."""
        settings = get_settings()
        token = self._bot_token or settings.secrets.slack_bot_token
        channel = self._channel_id or settings.secrets.slack_channel_id

        if not token:
            raise SlackNotificationError(
                "Slack bot token is not configured. Set SLACK_BOT_TOKEN in .env or Settings."
            )
        if not channel:
            raise SlackNotificationError(
                "Slack channel ID is not configured. Set SLACK_CHANNEL_ID in .env or Settings."
            )
        return token, channel

    async def post_reasoning(
        self,
        incident_id: str,
        message: str = "",
        plan: RemediationPlan | None = None,
        result: TournamentResult | None = None,
        verdict: KernelVerdict | None = None,
        *,
        context: IncidentContext | None = None,
        plans: Sequence[RemediationPlan] | None = None,
        evidence: Sequence[CandidateEvidence] | None = None,
        prod_outcome: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Format and deliver a 7-block reasoning post to the configured Slack channel."""
        token, channel = self._resolve_credentials()

        blocks = build_slack_reasoning_blocks(
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
        fallback_text = build_slack_reasoning_text(
            incident_id=incident_id,
            message=message,
            plan=plan,
            result=result,
            verdict=verdict,
            context=context,
            prod_outcome=prod_outcome,
        )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {
            "channel": channel,
            "text": fallback_text,
            "blocks": blocks,
        }

        settings = get_settings()
        timeout = settings.timeouts.slack_timeout_seconds
        try:
            if self._http_client is not None:
                resp = await self._http_client.post(
                    SLACK_API_URL, json=payload, headers=headers, timeout=timeout
                )
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(SLACK_API_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.error("slack_post_failed", incident_id=incident_id, error=str(exc))
            raise SlackNotificationError(f"Network failure posting to Slack: {exc}") from exc

        if resp.status_code != 200:
            logger.error(
                "slack_http_error",
                incident_id=incident_id,
                status_code=resp.status_code,
                body=resp.text,
            )
            raise SlackNotificationError(f"Slack HTTP {resp.status_code} error: {resp.text}")

        data_raw = resp.json()
        if not isinstance(data_raw, dict):
            raise SlackNotificationError("Invalid response from Slack: expected JSON object")
        data: dict[str, Any] = dict(data_raw)
        if not data.get("ok"):
            err_msg = str(data.get("error", "unknown_error"))
            logger.error("slack_api_error", incident_id=incident_id, error=err_msg)
            raise SlackNotificationError(f"Slack API error: {err_msg}")

        logger.info(
            "slack_reasoning_posted",
            incident_id=incident_id,
            channel=channel,
            ts=data.get("ts"),
        )
        return data

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
        """Post a structured incident update to Slack."""
        await self.post_reasoning(
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
        """Escalate an unresolvable or vetoed incident to PagerDuty on-call."""
        raise NotImplementedError(
            "PagerDuty escalation notifier is implemented in build-plan step 5.4. "
            "Use PagerDutyNotifier or CompositeNotifier."
        )


__all__ = [
    "SLACK_API_URL",
    "SlackNotifier",
    "build_candidate_table",
    "build_decision_analysis",
    "build_slack_reasoning_blocks",
    "build_slack_reasoning_text",
]
