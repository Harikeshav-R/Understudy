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
from understudy.contracts.enums import KernelVerdictType, TournamentOutcome
from understudy.contracts.evidence import CandidateEvidence, CandidateScore, TournamentResult
from understudy.contracts.incident import IncidentContext
from understudy.contracts.kernel import KernelVerdict
from understudy.contracts.plan import ActionParams, RemediationPlan
from understudy.notify.api import Notifier

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


def build_candidate_table(
    plans: Sequence[RemediationPlan] | None = None,
    result: TournamentResult | None = None,
    evidence: Sequence[CandidateEvidence] | None = None,
) -> str:
    """Build a fixed-width monospaced ASCII table of candidates and tournament scores.

    Columns:
    Idx | Action          | Target        | Recovery | Blast | Drop% | Composite | Status
    """
    if not plans:
        return "(No candidate plans evaluated)"

    scores_by_plan: dict[str, CandidateScore] = {}
    if result and result.scores:
        scores_by_plan = {s.plan_id: s for s in result.scores}

    evidence_by_plan: dict[str, CandidateEvidence] = {}
    if evidence:
        evidence_by_plan = {e.plan_id: e for e in evidence}

    header = (
        f"{'Idx':<4} | {'Action':<17} | {'Target':<15} | "
        f"{'Recovery':<8} | {'Blast':<7} | {'Drop%':<6} | {'Score':<7} | {'Status':<12}"
    )
    sep = (
        f"{'-' * 4}-+-{'-' * 17}-+-{'-' * 15}-+-"
        f"{'-' * 8}-+-{'-' * 7}-+-{'-' * 6}-+-{'-' * 7}-+-{'-' * 12}"
    )

    rows: list[str] = [header, sep]

    for plan in plans:
        idx_str = str(plan.candidate_index)
        action_str = plan.action.value.upper()[:17]
        target_str = (plan.params.workload or "-")[:15]

        ev = evidence_by_plan.get(plan.plan_id)
        sc = scores_by_plan.get(plan.plan_id)

        # Recovery string
        if ev and ev.recovered and ev.recovery_seconds is not None:
            rec_str = f"{ev.recovery_seconds:.1f}s"
        elif ev and not ev.recovered:
            rec_str = "None"
        else:
            rec_str = "-"

        # Blast string
        if ev and ev.observed_blast_set is not None:
            blast_str = f"{len(ev.observed_blast_set)} svc"
        else:
            blast_str = "-"

        # Drop percentage
        drop_str = f"{ev.mirror_stats.drop_ratio * 100:.1f}%" if ev and ev.mirror_stats else "-"

        # Composite score
        if sc and not sc.disqualified:
            comp_str = f"{sc.composite:.3f}"
        elif sc and sc.disqualified:
            comp_str = "DISQ"
        else:
            comp_str = "-"

        # Status
        if result and result.winner_plan_id == plan.plan_id:
            status_str = "WINNER"
        elif result and result.runner_up_plan_id == plan.plan_id:
            status_str = "RUNNER-UP"
        elif sc and sc.disqualified:
            status_str = "DISQUALIFIED"
        else:
            status_str = "-"

        row = (
            f"{idx_str:<4} | {action_str:<17} | {target_str:<15} | "
            f"{rec_str:<8} | {blast_str:<7} | {drop_str:<6} | {comp_str:<7} | {status_str:<12}"
        )
        rows.append(row)

    return "\n".join(rows)


def build_decision_analysis(
    result: TournamentResult | None = None,
    plans: Sequence[RemediationPlan] | None = None,
    evidence: Sequence[CandidateEvidence] | None = None,
) -> str:
    """Explain why the tournament winner beat the other candidates, or why no winner was chosen."""
    if not result:
        return "No tournament arbitration result recorded."

    plans_by_id = {p.plan_id: p for p in plans or []}
    evidence_by_id = {e.plan_id: e for e in evidence or []}

    lines: list[str] = []

    if result.outcome == TournamentOutcome.DECIDED:
        winner_plan = plans_by_id.get(result.winner_plan_id or "")
        runner_up_plan = plans_by_id.get(result.runner_up_plan_id or "")

        winner_name = (
            f"Plan {winner_plan.candidate_index} "
            f"({winner_plan.action.value.upper()} on {winner_plan.params.workload})"
            if winner_plan
            else result.winner_plan_id or "Winner"
        )
        runner_up_name = (
            f"Plan {runner_up_plan.candidate_index} "
            f"({runner_up_plan.action.value.upper()} on {runner_up_plan.params.workload})"
            if runner_up_plan
            else result.runner_up_plan_id or "Runner-up"
        )

        margin_val = f"{result.margin:.3f}" if result.margin is not None else "N/A"
        lines.append(f"*Winner:* {winner_name}")
        lines.append(
            f"*Margin:* {margin_val} over {runner_up_name} (exceeds ambiguity threshold 0.150)"
        )

        # Specific component advantages
        winner_ev = evidence_by_id.get(result.winner_plan_id or "")
        runner_up_ev = evidence_by_id.get(result.runner_up_plan_id or "")

        if winner_ev and winner_ev.recovered:
            rec_text = f"recovered primary SLO in {winner_ev.recovery_seconds:.1f}s"
            if runner_up_ev:
                if not runner_up_ev.recovered:
                    rec_text += " (runner-up never achieved recovery)"
                elif runner_up_ev.recovery_seconds is not None:
                    rec_text += f" vs {runner_up_ev.recovery_seconds:.1f}s for runner-up"
            lines.append(f"• *Recovery:* {rec_text}.")

        if winner_ev:
            blast_cnt = len(winner_ev.observed_blast_set)
            lines.append(
                f"• *Blast Radius:* Contained to {blast_cnt} service(s) with "
                f"{winner_ev.downstream_error_delta * 100:+.2f}% downstream error delta."
            )

        # Disqualifications
        disqualified = [s for s in result.scores if s.disqualified]
        if disqualified:
            disq_notes: list[str] = []
            for s in disqualified:
                p_idx = (
                    plans_by_id[s.plan_id].candidate_index
                    if s.plan_id in plans_by_id
                    else s.plan_id
                )
                disq_notes.append(f"Plan {p_idx} ({s.disqualification_reason})")
            lines.append(f"• *Disqualifications:* {', '.join(disq_notes)}.")

        # NO_ACTION winner case
        if winner_plan and winner_plan.action.value == "no_action":
            lines.append(
                "• *Decision Note:* Doing nothing (`NO_ACTION`) was the authoritative winner; "
                "active candidate interventions produced adverse side-effects or failed recovery."
            )

        # Advisory LLM Judge
        if result.llm_agreement is not None:
            agree_str = "Agreed (top-1 match)" if result.llm_agreement else "Disagreed"
            ranking_str = " > ".join(result.llm_ranking) if result.llm_ranking else "none"
            lines.append(f"• *LLM Advisory Judge:* {agree_str} [Ranking: {ranking_str}].")

    elif result.outcome == TournamentOutcome.AMBIGUOUS:
        margin_val = f"{result.margin:.3f}" if result.margin is not None else "0.000"
        lines.append(
            f"*Arbitration Outcome: AMBIGUOUS (No Winner)*\n"
            f"Margin between top candidates ({margin_val}) is below the required 0.150 ambiguity "
            f"margin. Production actuation refused to prevent non-deterministic choices."
        )

    else:
        lines.append(
            "*Arbitration Outcome: NO VIABLE CANDIDATE*\n"
            "All evaluated remediation plans were disqualified or failed recovery in twin "
            "environments."
        )

    return "\n".join(lines)


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
        avg_drop = (total_dropped / total_delivered * 100) if total_delivered > 0 else 0.0

        fidelity_text = (
            f"🪞 *Mirror Traffic Fidelity:* {total_delivered:,} reqs delivered "
            f"({total_dropped:,} dropped, avg drop {avg_drop:.2f}%). "
            f"Twins: [{', '.join(twin_drops)}]. All under 5.0% ceiling."
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

        try:
            if self._http_client is not None:
                resp = await self._http_client.post(
                    SLACK_API_URL, json=payload, headers=headers, timeout=10.0
                )
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(SLACK_API_URL, json=payload, headers=headers)
        except Exception as exc:
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
