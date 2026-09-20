"""Shared text and table formatting utilities for notifications and escalations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from understudy.contracts.enums import TournamentOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from understudy.contracts.evidence import CandidateEvidence, CandidateScore, TournamentResult
    from understudy.contracts.plan import RemediationPlan


def format_action_summary(plan: RemediationPlan) -> str:
    """Format short summary of remediation plan action and arguments."""
    params = plan.params
    parts: list[str] = []
    if params.workload:
        parts.append(f"workload={params.workload}")
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


__all__ = [
    "build_candidate_table",
    "build_decision_analysis",
    "format_action_summary",
]
