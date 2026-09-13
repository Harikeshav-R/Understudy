"""LLM confirmation arbiter for candidate playbook matches."""

import json
import re
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field

from understudy.common.config import Settings, get_settings
from understudy.common.errors import PlaybookConfirmationError
from understudy.common.logging import get_logger
from understudy.contracts.incident import IncidentContext
from understudy.store.api import PlaybookSearchResult

logger = get_logger(__name__)


class ConfirmationResult(BaseModel):
    """Parsed result of the LLM playbook confirmation decision."""

    model_config = ConfigDict(frozen=True)

    retained_playbook_id: str | None = None
    reason: str = Field(description="Explanation of confirmation or rejection decision")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class _ConfirmationRawSchema(BaseModel):
    retained_playbook_id: str | None = None
    confidence: float = 1.0
    reason: str = ""


CONFIRMATION_SYSTEM_PROMPT = """You are the Understudy Playbook Confirmation Arbiter.
Your role is to evaluate candidate remediation playbooks retrieved via pgvector similarity
against active incident diagnostics.

A playbook match is a candidate, NEVER an automatic shortcut (ADR-019). It must be verified
for semantic applicability before rehearsal.

Evaluate whether any candidate playbook accurately addresses the current incident root cause,
failure class, and affected workload.

CRITICAL DECISION RULES:
1. You must answer with AT MOST ONE retained playbook_id, or null if none are appropriate.
2. NEVER invent or hallucinate a playbook_id that is not in the provided candidates list.
3. If no playbook is suitable (e.g. wrong failure class, mismatched service, or irrelevant action),
   set retained_playbook_id to null and explain why in reason.
4. Output MUST be valid JSON adhering to this schema:
{
  "retained_playbook_id": "<playbook_id>" or null,
  "confidence": 0.95,
  "reason": "Detailed explanation of why this playbook was confirmed or why all were rejected"
}
"""


def format_candidate_playbooks_for_prompt(candidates: list[PlaybookSearchResult]) -> str:
    """Format candidate playbooks into structured prompt section."""
    sections: list[str] = []
    for idx, cand in enumerate(candidates, start=1):
        params_str = json.dumps(cand.plan.params.model_dump(exclude_none=True), sort_keys=True)
        blast_str = ", ".join(cand.plan.declared_blast_set) or "none"
        sections.append(
            f"Candidate #{idx}:\n"
            f"  Playbook ID: {cand.playbook_id}\n"
            f"  Cosine Similarity: {cand.similarity:.4f}\n"
            f"  Failure Class: {cand.failure_class}\n"
            f"  Action: {cand.plan.action.value}\n"
            f"  Target Workload: {cand.plan.params.workload}\n"
            f"  Parameters: {params_str}\n"
            f"  Declared Blast Set: {blast_str}\n"
            f"  Historical Stats: {cand.successes} successes, {cand.failures} failures\n"
            f"  Plan Rationale: {cand.plan.rationale}\n"
            f"  Signature Text:\n    {cand.signature_text.replace('\n', '\n    ')}"
        )
    return "\n\n".join(sections)


def build_confirmation_prompt(
    context: IncidentContext,
    candidates: list[PlaybookSearchResult],
) -> list[dict[str, str]]:
    """Build OpenRouter chat messages payload for playbook confirmation."""
    fc = (
        context.inferred_failure_class.value
        if context.inferred_failure_class is not None
        else "unknown"
    )
    sig_lines = [
        f"- {s.fingerprint} (count: {s.count}): {s.message}" for s in context.signatures[:3]
    ] or ["- none"]

    recent_deploys_lines = [
        f"- {d.commit_sha[:7]} at {d.deployed_at.isoformat()} (migration: {d.contains_migration})"
        for d in context.recent_deploys[:2]
    ] or ["- none"]

    candidates_formatted = format_candidate_playbooks_for_prompt(candidates)

    user_content = f"""ACTIVE INCIDENT DIAGNOSTICS:
- Incident ID: {context.incident_id}
- Affected Service: {context.alert.service}
- Alert Title: {context.alert.title} (severity: {context.alert.severity})
- Inferred Failure Class: {fc}
- Metrics: p99={context.metrics_window.p99_latency_ms}ms,
  error_rate={context.metrics_window.error_rate}
- Top Error Signatures:
{chr(10).join(sig_lines)}
- Recent Deployments:
{chr(10).join(recent_deploys_lines)}

RETRIEVED PLAYBOOK CANDIDATES (Top-{len(candidates)} by pgvector cosine similarity):
{candidates_formatted}

INSTRUCTION:
Evaluate the candidate playbooks against the incident diagnostics.
Output a single JSON object with 'retained_playbook_id', 'confidence', and 'reason'.
"""

    return [
        {"role": "system", "content": CONFIRMATION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def parse_confirmation_response(
    raw_response: str,
    valid_playbook_ids: set[str],
) -> ConfirmationResult:
    """Parse JSON response from confirmation LLM and validate retained ID."""
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", cleaned)
        cleaned = re.sub(r"\n```$", "", cleaned)
        cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        parsed = _ConfirmationRawSchema.model_validate(data)
    except Exception as exc:
        raise PlaybookConfirmationError(
            f"Failed to parse LLM confirmation response: {exc}\nRaw: {raw_response[:300]}",
            details={"raw_response": raw_response[:500]},
        ) from exc

    retained_id = parsed.retained_playbook_id
    reason = parsed.reason.strip() or "No confirmation reason provided by LLM."
    confidence = max(0.0, min(1.0, float(parsed.confidence)))

    if retained_id is not None and retained_id not in valid_playbook_ids:
        logger.warning(
            "playbook_confirmation_hallucinated_id",
            hallucinated_id=retained_id,
            valid_ids=list(valid_playbook_ids),
        )
        return ConfirmationResult(
            retained_playbook_id=None,
            reason=(
                f"LLM proposed unknown playbook ID '{retained_id}' not in candidate set. "
                "Rejected for safety."
            ),
            confidence=confidence,
        )

    return ConfirmationResult(
        retained_playbook_id=retained_id,
        reason=reason,
        confidence=confidence,
    )


class PlaybookConfirmer:
    """Invokes LLM to arbitrate and confirm candidate playbook applicability."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        llm_caller: Callable[[list[dict[str, str]]], Awaitable[str]] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._custom_caller = llm_caller

    async def confirm(
        self,
        context: IncidentContext,
        candidates: list[PlaybookSearchResult],
    ) -> ConfirmationResult:
        """Evaluate candidates via LLM and return confirmed playbook ID or None."""
        if not candidates:
            return ConfirmationResult(
                retained_playbook_id=None,
                reason="No candidate playbooks found in library for this incident signature.",
                confidence=1.0,
            )

        messages = build_confirmation_prompt(context, candidates)
        valid_ids = {c.playbook_id for c in candidates}

        if self._custom_caller is not None:
            raw_text = await self._custom_caller(messages)
            return parse_confirmation_response(raw_text, valid_ids)

        api_key = self.settings.secrets.openrouter_api_key
        if not api_key:
            raise PlaybookConfirmationError(
                "OPENROUTER_API_KEY is not configured in settings or environment",
                details={"setting": "secrets.openrouter_api_key"},
            )

        url = f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        timeout = float(self.settings.timeouts.incident_seconds)

        if self._client is not None:
            resp = await self._client.post(url, headers=headers, json=payload, timeout=timeout)
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=payload, timeout=timeout)

        if resp.status_code != 200:
            raise PlaybookConfirmationError(
                f"OpenRouter confirmation API returned status {resp.status_code}: {resp.text}",
                details={"status_code": resp.status_code, "response": resp.text[:500]},
            )

        data = resp.json()
        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            raise PlaybookConfirmationError(
                "OpenRouter confirmation response missing choices array",
                details={"response": data},
            )
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise PlaybookConfirmationError(
                "OpenRouter confirmation choice item is not an object",
                details={"choice": first_choice},
            )
        content = first_choice.get("message", {}).get("content", "")
        if not isinstance(content, str):
            raise PlaybookConfirmationError(
                "OpenRouter confirmation content is not a string",
                details={"content": content},
            )

        return parse_confirmation_response(content, valid_ids)


__all__ = [
    "ConfirmationResult",
    "PlaybookConfirmer",
    "build_confirmation_prompt",
    "format_candidate_playbooks_for_prompt",
    "parse_confirmation_response",
]
