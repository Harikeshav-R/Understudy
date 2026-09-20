"""PagerDuty incident escalation client and note generator for Understudy.

Implements build-plan step 5.4, ADR-026, and architecture §2.3, §2.4:
- Annotates the triggering PagerDuty incident with full comparative evidence:
  1. Escalation banner & production status (production untouched)
  2. Safety kernel verdict details (invariant, reason, counterexample)
  3. ASCII candidate scoreboard table with scores and mirror drop ratios
  4. Decision analysis (why winner won tournament, why kernel vetoed)
  5. Diagnostic context (service, alert, metrics window, deploys)
  6. Append-only run record identifier
- Sets incident urgency to high
- Interacts with PagerDuty REST API v2 using async HTTP with explicit timeouts
"""

import json
from collections.abc import Sequence
from typing import Any

import httpx

from understudy.common.config import get_settings
from understudy.common.errors import PagerDutyNotificationError
from understudy.common.logging import get_logger
from understudy.contracts.enums import KernelVerdictType
from understudy.contracts.evidence import CandidateEvidence, TournamentResult
from understudy.contracts.incident import IncidentContext
from understudy.contracts.kernel import KernelVerdict
from understudy.contracts.plan import RemediationPlan
from understudy.notify.api import Notifier
from understudy.notify.slack import build_candidate_table, build_decision_analysis

logger = get_logger(__name__)

PAGERDUTY_API_BASE = "https://api.pagerduty.com"
DEFAULT_FROM_EMAIL = "agent@understudy.dev"


def resolve_pagerduty_incident_id(
    incident_id: str,
    context: IncidentContext | None = None,
    pd_incident_id: str | None = None,
) -> str:
    """Resolve the external PagerDuty incident identifier from available metadata.

    Order of precedence:
    1. Explicit pd_incident_id if provided.
    2. IncidentContext alert raw payload (v3 Webhook event.data.id or v2 messages incident.id).
    3. Alert alert_id stripped of alt_pd_ / alt_ prefixes.
    4. incident_id stripped of inc_alt_pd_ / inc_ prefixes.
    """
    if pd_incident_id and pd_incident_id.strip():
        return pd_incident_id.strip()

    if context and context.alert and context.alert.raw:
        raw = context.alert.raw
        # Webhooks v3 format
        evt = raw.get("event")
        if isinstance(evt, dict):
            data = evt.get("data")
            if isinstance(data, dict) and data.get("id"):
                return str(data["id"]).strip()
            if evt.get("id"):
                return str(evt["id"]).strip()

        # Webhooks v2 format
        msgs = raw.get("messages")
        if isinstance(msgs, list) and msgs:
            first_msg = msgs[0]
            if isinstance(first_msg, dict):
                inc = first_msg.get("incident")
                if isinstance(inc, dict) and inc.get("id"):
                    return str(inc["id"]).strip()

        # Direct raw id or incident_id field
        for key in ("id", "incident_id"):
            val = raw.get(key)
            if val and isinstance(val, str) and val.strip():
                return val.strip()

    if context and context.alert and context.alert.alert_id:
        aid = context.alert.alert_id
        if aid.startswith("alt_pd_"):
            return aid.removeprefix("alt_pd_")
        if aid.startswith("alt_"):
            return aid.removeprefix("alt_")

    cleaned = incident_id
    for prefix in ("inc_alt_pd_", "inc_alt_", "inc_"):
        if cleaned.startswith(prefix):
            cleaned = cleaned.removeprefix(prefix)
            break

    return cleaned or incident_id


def build_pagerduty_escalation_note(
    incident_id: str,
    reason: str,
    *,
    verdict: KernelVerdict | None = None,
    plans: Sequence[RemediationPlan] | None = None,
    result: TournamentResult | None = None,
    evidence: Sequence[CandidateEvidence] | None = None,
    context: IncidentContext | None = None,
    run_id: str | None = None,
) -> str:
    """Format full comparative evidence and safety reason into a structured PagerDuty note.

    Sections:
    1. Incident Escalation Banner & Production Status
    2. Safety Kernel Verdict & Counterexample
    3. Candidate Remediation Scoreboard (ASCII Table)
    4. Decision & Tournament Arbitration Analysis
    5. Diagnostic Telemetry Context
    6. Immutable Run Record Identifier
    """
    service = context.alert.service if context is not None else "edge-gateway"
    rec_run_id = run_id or (f"run_{incident_id}" if incident_id else "run_unknown")

    lines: list[str] = [
        "🚨 UNDERSTUDY AUTOMATED INCIDENT ESCALATION",
        f"Incident: {incident_id} | Target Service: {service}",
        "Outcome: ESCALATED | Production Status: UNTOUCHED (actuation halted)",
        f"Primary Reason: {reason}",
        "",
    ]

    # Section 2: Safety Kernel Verdict
    lines.append("═════════════════════════════════════════════════════════════════════════")
    lines.append("SAFETY KERNEL VERIFICATION")
    lines.append("═════════════════════════════════════════════════════════════════════════")
    if verdict is not None:
        v_type = verdict.verdict.value.upper()
        failed_invs = [r for r in verdict.results if r.satisfied is False]
        uncertain_invs = [r for r in verdict.results if r.satisfied is None]

        if failed_invs:
            inv_str = ", ".join(f"{r.invariant_id} ({r.reason})" for r in failed_invs)
        elif uncertain_invs:
            inv_str = ", ".join(f"{r.invariant_id} ({r.reason})" for r in uncertain_invs)
        elif verdict.results:
            inv_str = f"{len(verdict.results)} invariant(s) satisfied"
        else:
            explicit_inv = getattr(verdict, "invariant", None)
            inv_str = f"Invariant {explicit_inv}" if explicit_inv else "No invariant flagged"

        lines.append(f"Verdict:      {v_type}")
        lines.append(f"Evaluation:   {inv_str}")
        lines.append(f"Explanation:  {verdict.human_reason}")

        # Counterexample or unsat cores
        ce = getattr(verdict, "counterexample", None)
        if ce:
            ce_str = json.dumps(ce, indent=2) if isinstance(ce, (dict, list)) else str(ce)
            lines.append(f"Counterexample:\n{ce_str}")
        for r in failed_invs:
            if r.unsat_core:
                lines.append(f"Unsat Core ({r.invariant_id}): {', '.join(r.unsat_core)}")

        if verdict.missing_facts:
            lines.append(f"Missing Facts: {', '.join(verdict.missing_facts)}")
        lines.append(f"Solver Time:  {verdict.solver_ms:.1f}ms")
    else:
        lines.append(
            "Verdict:      NOT EVALUATED (incident escalated before or during kernel verification)"
        )
    lines.append("")

    # Section 3: Candidate Scoreboard Table
    lines.append("═════════════════════════════════════════════════════════════════════════")
    lines.append("CANDIDATE REMEDIATION SCOREBOARD (TWIN REHEARSALS)")
    lines.append("═════════════════════════════════════════════════════════════════════════")
    table = build_candidate_table(plans=plans, result=result, evidence=evidence)
    lines.append(table)
    lines.append("")

    # Section 4: Decision Analysis
    lines.append("═════════════════════════════════════════════════════════════════════════")
    lines.append("TOURNAMENT ARBITRATION & SAFETY REASONING")
    lines.append("═════════════════════════════════════════════════════════════════════════")
    decision_text = build_decision_analysis(result=result, plans=plans, evidence=evidence)
    lines.append(decision_text)
    if verdict is not None and verdict.verdict == KernelVerdictType.VETO:
        failed_names = [r.invariant_id for r in verdict.results if r.satisfied is False]
        inv_flag = ", ".join(failed_names) if failed_names else getattr(verdict, "invariant", "K")
        lines.append(
            f"NOTE: Although a candidate may have achieved a superior tournament score, "
            f"actuation to production was vetoed by Safety Kernel invariant {inv_flag}. "
            f"Production was NOT modified."
        )
    lines.append("")

    # Section 5: Diagnostic Telemetry Context
    if context is not None:
        lines.append("═════════════════════════════════════════════════════════════════════════")
        lines.append("DIAGNOSTIC CONTEXT & TELEMETRY")
        lines.append("═════════════════════════════════════════════════════════════════════════")
        lines.append(
            f"Triggering Alert: {context.alert.title} (Severity: {context.alert.severity.upper()})"
        )
        mw = context.metrics_window
        p99_str = f"{mw.p99_latency_ms:.1f}ms" if mw.p99_latency_ms is not None else "N/A"
        err_str = f"{mw.error_rate * 100:.2f}%" if mw.error_rate is not None else "N/A"
        lines.append(
            f"Telemetry Window: p99 latency: {p99_str} | "
            f"error rate: {err_str} | requests: {mw.request_count}"
        )
        if context.recent_deploys:
            dep_lines = []
            for d in context.recent_deploys[:3]:
                mig_flag = " [contains migration]" if d.contains_migration else ""
                dep_lines.append(
                    f"  - {d.commit_sha[:7]} (deployed {d.deployed_at.isoformat()}{mig_flag})"
                )
            lines.append("Recent Deploys:\n" + "\n".join(dep_lines))
        lines.append("")

    # Section 6: Run Record & Guarantee
    lines.append("─────────────────────────────────────────────────────────────────────────")
    lines.append(f"Immutable Run Record: {rec_run_id}")
    lines.append(
        "Safety Guarantee: Actuator require PASS verdict. Production namespace ust-prod intact."
    )

    return "\n".join(lines)


class PagerDutyNotifier(Notifier):
    """PagerDuty escalation client delivering comparative evidence notes and setting urgency."""

    def __init__(
        self,
        token: str | None = None,
        from_email: str | None = None,
        base_url: str = PAGERDUTY_API_BASE,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._from_email = from_email
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client

    def _resolve_credentials(self) -> tuple[str, str]:
        """Resolve API token and from-email from constructor arguments or settings."""
        settings = get_settings()
        token = self._token or settings.secrets.pagerduty_token
        email = self._from_email or settings.secrets.pagerduty_from_email or DEFAULT_FROM_EMAIL

        if not token:
            raise PagerDutyNotificationError(
                "PagerDuty API token is not configured. Set PAGERDUTY_TOKEN in .env or Settings."
            )
        return token, email

    def _build_headers(self, token: str, from_email: str | None) -> dict[str, str]:
        """Construct standard PagerDuty REST API v2 request headers."""
        headers = {
            "Authorization": f"Token token={token}",
            "Accept": "application/vnd.pagerduty+json;version=2",
            "Content-Type": "application/json",
        }
        if from_email:
            headers["From"] = from_email
        return headers

    async def add_incident_note(
        self,
        pd_incident_id: str,
        content: str,
    ) -> dict[str, Any]:
        """Deliver a structured note to the specified PagerDuty incident."""
        token, email = self._resolve_credentials()
        url = f"{self._base_url}/incidents/{pd_incident_id}/notes"
        headers = self._build_headers(token, email)
        payload = {"note": {"content": content}}

        try:
            if self._http_client is not None:
                resp = await self._http_client.post(
                    url, json=payload, headers=headers, timeout=10.0
                )
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url, json=payload, headers=headers)
        except Exception as exc:
            logger.error("pagerduty_add_note_failed", pd_incident_id=pd_incident_id, error=str(exc))
            raise PagerDutyNotificationError(
                f"Network failure adding PagerDuty note: {exc}"
            ) from exc

        if resp.status_code not in (200, 201):
            logger.error(
                "pagerduty_add_note_error",
                pd_incident_id=pd_incident_id,
                status_code=resp.status_code,
                body=resp.text,
            )
            raise PagerDutyNotificationError(
                f"PagerDuty add note failed with HTTP {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        if not isinstance(data, dict):
            raise PagerDutyNotificationError(
                "Invalid response from PagerDuty: expected JSON object"
            )

        logger.info(
            "pagerduty_note_added",
            pd_incident_id=pd_incident_id,
            note_id=data.get("note", {}).get("id") if isinstance(data.get("note"), dict) else None,
        )
        return dict(data)

    async def set_incident_urgency(
        self,
        pd_incident_id: str,
        urgency: str = "high",
    ) -> dict[str, Any]:
        """Update the urgency level of the specified PagerDuty incident."""
        token, email = self._resolve_credentials()
        url = f"{self._base_url}/incidents/{pd_incident_id}"
        headers = self._build_headers(token, email)
        payload = {
            "incident": {
                "type": "incident",
                "urgency": urgency,
            }
        }

        try:
            if self._http_client is not None:
                resp = await self._http_client.put(url, json=payload, headers=headers, timeout=10.0)
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.put(url, json=payload, headers=headers)
        except Exception as exc:
            logger.error(
                "pagerduty_set_urgency_failed", pd_incident_id=pd_incident_id, error=str(exc)
            )
            raise PagerDutyNotificationError(
                f"Network failure setting PagerDuty urgency: {exc}"
            ) from exc

        if resp.status_code != 200:
            logger.error(
                "pagerduty_set_urgency_error",
                pd_incident_id=pd_incident_id,
                status_code=resp.status_code,
                body=resp.text,
            )
            raise PagerDutyNotificationError(
                f"PagerDuty set urgency failed with HTTP {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        if not isinstance(data, dict):
            raise PagerDutyNotificationError(
                "Invalid response from PagerDuty: expected JSON object"
            )

        logger.info("pagerduty_urgency_updated", pd_incident_id=pd_incident_id, urgency=urgency)
        return dict(data)

    async def escalate(
        self,
        incident_id: str,
        reason: str,
        *,
        verdict: KernelVerdict | None = None,
        plans: Sequence[RemediationPlan] | None = None,
        result: TournamentResult | None = None,
        evidence: Sequence[CandidateEvidence] | None = None,
        context: IncidentContext | None = None,
        urgency: str = "high",
        pd_incident_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Escalate incident by delivering comparative evidence note and updating urgency."""
        target_pd_id = resolve_pagerduty_incident_id(
            incident_id=incident_id,
            context=context,
            pd_incident_id=pd_incident_id,
        )

        note_content = build_pagerduty_escalation_note(
            incident_id=incident_id,
            reason=reason,
            verdict=verdict,
            plans=plans,
            result=result,
            evidence=evidence,
            context=context,
            run_id=run_id,
        )

        logger.info(
            "pagerduty_escalating",
            incident_id=incident_id,
            pd_incident_id=target_pd_id,
            urgency=urgency,
        )

        try:
            note_resp = await self.add_incident_note(target_pd_id, note_content)
        except PagerDutyNotificationError as exc:
            if "404" in str(exc):
                logger.warning(
                    "pagerduty_incident_not_found",
                    pd_incident_id=target_pd_id,
                    incident_id=incident_id,
                )
                note_resp = {"status": "incident_not_found", "pd_incident_id": target_pd_id}
            else:
                raise

        try:
            urgency_resp = await self.set_incident_urgency(target_pd_id, urgency=urgency)
        except PagerDutyNotificationError as exc:
            if "404" in str(exc):
                urgency_resp = {"status": "incident_not_found", "pd_incident_id": target_pd_id}
            else:
                raise

        return {
            "pd_incident_id": target_pd_id,
            "note": note_resp,
            "incident": urgency_resp,
            "note_content": note_content,
        }

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
        evidence = partial_evidence
        await self.escalate(
            incident_id=incident_id,
            reason=reason,
            verdict=verdict,
            plans=plans,
            result=tournament,
            evidence=evidence,
            context=context,
            urgency=urgency,
            pd_incident_id=pd_incident_id,
            run_id=f"run_{incident_id}" if incident_id else None,
        )

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
        """Raise NotImplementedError as Slack notification is owned by SlackNotifier."""
        raise NotImplementedError("Slack notification is handled by SlackNotifier.")


__all__ = [
    "DEFAULT_FROM_EMAIL",
    "PAGERDUTY_API_BASE",
    "PagerDutyNotifier",
    "build_pagerduty_escalation_note",
    "resolve_pagerduty_incident_id",
]
