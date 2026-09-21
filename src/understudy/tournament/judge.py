"""Advisory LLM judge evaluating rehearsal tournament evidence without score leakage.

Implements build-plan step A4.4, ADR-017, and architecture §2.8:
- Evaluates candidate remediation plans and rehearsal evidence via OpenRouter.
- Operates strictly in parallel with DeterministicScorer; has zero access to
  deterministic scores, weights, composites, or disqualification states.
- Produces an independent, advisory ranked list of plan IDs with per-candidate
  justifications and an overall comparative rationale.
- Computes top-1 agreement against deterministic scoring for Metric 5.
"""

import json
import re
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from understudy.common.config import get_settings
from understudy.common.errors import ConfigError, UnderstudyError
from understudy.common.logging import get_logger
from understudy.contracts.evidence import CandidateEvidence, CandidateScore
from understudy.contracts.plan import RemediationPlan

logger = get_logger(__name__)


class JudgeError(UnderstudyError):
    """Base exception for LLM advisory judge failures."""


class JudgeConfigError(JudgeError, ConfigError):
    """Raised when judge configuration or credentials are missing or invalid."""


class JudgeAPIError(JudgeError):
    """Raised when OpenRouter API returns an error HTTP status or invalid payload."""


class JudgeTimeoutError(JudgeError):
    """Raised when OpenRouter API request times out."""


class JudgeParseError(JudgeError):
    """Raised when LLM completion cannot be parsed into a valid JudgeEvaluation."""


class JudgeConfig(BaseModel):
    """Configuration parameters for the Advisory LLM Judge."""

    model_config = ConfigDict(frozen=True)

    model: str = "anthropic/claude-3.5-sonnet"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str | None = None
    timeout_seconds: float = 30.0
    temperature: float = 0.0
    max_tokens: int = 1024

    @classmethod
    def from_settings(cls) -> "JudgeConfig":
        """Load JudgeConfig from global system settings."""
        settings = get_settings()
        return cls(
            model=settings.llm_model,
            base_url=settings.openrouter_base_url,
            api_key=settings.secrets.openrouter_api_key,
        )


class JudgeEvaluation(BaseModel):
    """Advisory evaluation produced by the LLM judge."""

    model_config = ConfigDict(frozen=True)

    ranking: list[str] = Field(default_factory=list)
    reasons: dict[str, str] = Field(default_factory=dict)
    rationale: str = ""
    model: str = ""
    raw_response: str | None = None


def serialize_evidence_for_judge(
    evidence: Sequence[CandidateEvidence],
    plans: Sequence[RemediationPlan] | None = None,
) -> list[dict[str, Any]]:
    """Serialize rehearsal evidence for the LLM judge.

    Per ADR-017 and architecture §2.8, the LLM judge has ZERO access to
    deterministic scores, composite weights, subscore breakdowns, or disqualification
    flags. It receives only raw empirical telemetry and declarative plan metadata.
    """
    plans_by_id: dict[str, RemediationPlan] = (
        {p.plan_id: p for p in plans} if plans is not None else {}
    )

    serialized: list[dict[str, Any]] = []
    for ev in evidence:
        plan = plans_by_id.get(ev.plan_id)

        # Probe statistics summary
        probe_count = len(ev.probes)
        healthy_count = sum(1 for p in ev.probes if p.healthy)
        avg_latency = (
            round(sum(p.p99_latency_ms for p in ev.probes) / probe_count, 2)
            if probe_count > 0
            else None
        )
        last_latency = round(ev.probes[-1].p99_latency_ms, 2) if probe_count > 0 else None
        avg_error_rate = (
            round(sum(p.error_rate for p in ev.probes) / probe_count, 4)
            if probe_count > 0
            else None
        )
        last_error_rate = round(ev.probes[-1].error_rate, 4) if probe_count > 0 else None

        item: dict[str, Any] = {
            "plan_id": ev.plan_id,
            "twin_id": ev.twin_id,
            "applied_at": ev.applied_at.isoformat(),
            "telemetry": {
                "recovered": ev.recovered,
                "recovery_seconds": ev.recovery_seconds,
                "observed_blast_set": sorted(ev.observed_blast_set),
                "downstream_error_delta": round(ev.downstream_error_delta, 4),
                "runtime_invariant_violations": sorted(ev.invariant_violations),
                "probes": {
                    "total_samples": probe_count,
                    "healthy_samples": healthy_count,
                    "avg_p99_latency_ms": avg_latency,
                    "last_p99_latency_ms": last_latency,
                    "avg_error_rate": avg_error_rate,
                    "last_error_rate": last_error_rate,
                },
                "mirror_fidelity": {
                    "delivered_requests": ev.mirror_stats.delivered,
                    "dropped_requests": ev.mirror_stats.dropped,
                    "drop_ratio": round(ev.mirror_stats.drop_ratio, 4),
                },
                "evidence_complete": ev.evidence_complete,
            },
        }

        if plan is not None:
            plan_meta: dict[str, Any] = {
                "action": plan.action.value,
                "target_workload": plan.params.workload,
                "target_resources": [
                    {"namespace": r.namespace, "kind": r.kind, "name": r.name}
                    for r in plan.target_resources
                ],
                "declared_blast_set": sorted(plan.declared_blast_set),
                "origin": plan.origin,
                "plan_rationale": plan.rationale,
            }
            if plan.params.target_commit is not None:
                plan_meta["target_commit"] = plan.params.target_commit
            if plan.params.replica_delta is not None:
                plan_meta["replica_delta"] = plan.params.replica_delta
            if plan.params.flag_name is not None:
                plan_meta["flag_name"] = plan.params.flag_name
            if plan.params.config_key is not None:
                plan_meta["config_key"] = plan.params.config_key
            if plan.params.config_value is not None:
                plan_meta["config_value"] = plan.params.config_value
            item["plan"] = plan_meta

        serialized.append(item)

    return serialized


def build_judge_system_prompt() -> str:
    """Construct the system prompt for the Advisory LLM Judge."""
    return (
        "You are an impartial, senior Site Reliability Engineering (SRE) advisory judge evaluating "
        "remediation candidate plans rehearsed on live production digital twins under mirrored "
        "traffic.\n\n"
        "Your task is to rank the candidates from best to worst based purely on empirical "
        "rehearsal evidence and provide concise, rigorous justifications.\n\n"
        "Evaluation Principles:\n"
        "1. Recovery: Primary objective is restoring service SLOs (recovered = true with minimal "
        "recovery_seconds).\n"
        "2. Blast Radius: Candidates causing fewer service disruptions and minimal "
        "downstream_error_delta are preferred.\n"
        "3. Safety: Invariant violations or side effects in the twin indicate dangerous "
        "interventions.\n"
        "4. Observability: High mirror drop ratios (> 0.05) or incomplete evidence indicate "
        "untrustworthy rehearsal data.\n"
        "5. Doing Nothing: If all active interventions cause damage or fail to recover, doing "
        "nothing (no_action) may be the safest choice.\n\n"
        "You MUST respond ONLY with a valid JSON object matching this exact schema:\n"
        "{\n"
        '  "ranking": ["<best_plan_id>", "<second_best_plan_id>", ...],\n'
        '  "reasons": {\n'
        '    "<plan_id>": "<specific technical reason for this candidate\'s ranking>"\n'
        "  },\n"
        '  "rationale": "<brief comparative analysis explaining why top candidate was chosen>"\n'
        "}"
    )


def build_judge_user_prompt(serialized_candidates: list[dict[str, Any]]) -> str:
    """Construct the user prompt containing serialized candidate evidence."""
    payload = json.dumps(serialized_candidates, indent=2)
    return (
        "Evaluate the following rehearsed remediation candidates and provide your advisory "
        "ranking:\n\n"
        f"```json\n{payload}\n```\n\n"
        "Produce an ordered ranking containing every candidate's plan_id from best to worst, "
        "with specific technical reasons in strict JSON format."
    )


def parse_judge_response(
    raw_text: str,
    candidate_plan_ids: Sequence[str],
    model: str = "",
) -> JudgeEvaluation:
    """Parse, sanitize, and validate the LLM judge completion text.

    Guarantees:
    - Hallucinated or non-existent plan IDs are filtered out.
    - Duplicate plan IDs are deduplicated preserving first position.
    - Any candidate plan IDs omitted by the LLM are appended at the end.
    - The returned ranking is a complete permutation of candidate_plan_ids.
    """
    valid_candidates = set(candidate_plan_ids)
    cleaned = raw_text.strip()

    # Strip reasoning / think tags if present
    cleaned = re.sub(r"<(think|thought)>.*?</\1>", "", cleaned, flags=re.DOTALL).strip()

    # Extract JSON if embedded in markdown code blocks
    if "```" in cleaned:
        parts = cleaned.split("```")
        for i in range(1, len(parts), 2):
            block = parts[i].strip()
            if block.startswith("json"):
                block = block[4:].strip()
            start_idx = block.find("{")
            end_idx = block.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                cleaned = block[start_idx : end_idx + 1]
                break
    else:
        start_idx = cleaned.find("{")
        end_idx = cleaned.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            cleaned = cleaned[start_idx : end_idx + 1]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise JudgeParseError(
            f"Failed to parse LLM response as JSON: {exc}",
            details={"raw_response": raw_text},
        ) from exc

    if not isinstance(data, dict):
        raise JudgeParseError(
            "Expected JSON object in LLM judge response",
            details={"parsed_type": type(data).__name__},
        )

    raw_ranking = data.get("ranking", [])
    raw_reasons = data.get("reasons", {})
    rationale = str(data.get("rationale", ""))

    if not isinstance(raw_ranking, list):
        raise JudgeParseError(
            "LLM judge ranking must be a list of plan IDs",
            details={"ranking_type": type(raw_ranking).__name__},
        )
    if not isinstance(raw_reasons, dict):
        raise JudgeParseError(
            "LLM judge reasons must be a dictionary",
            details={"reasons_type": type(raw_reasons).__name__},
        )

    # Sanitize ranking: keep only valid candidates, deduplicate
    sanitized_ranking: list[str] = []
    seen: set[str] = set()

    for pid in raw_ranking:
        if isinstance(pid, str) and pid in valid_candidates and pid not in seen:
            sanitized_ranking.append(pid)
            seen.add(pid)

    # Append any omitted candidate IDs preserving candidate_plan_ids order
    for pid in candidate_plan_ids:
        if pid not in seen:
            sanitized_ranking.append(pid)
            seen.add(pid)

    # Build sanitized reasons
    sanitized_reasons: dict[str, str] = {}
    for pid in candidate_plan_ids:
        reason_val = raw_reasons.get(pid)
        if isinstance(reason_val, str) and reason_val.strip():
            sanitized_reasons[pid] = reason_val.strip()
        else:
            sanitized_reasons[pid] = f"Candidate {pid} ranked by advisory judge."

    return JudgeEvaluation(
        ranking=sanitized_ranking,
        reasons=sanitized_reasons,
        rationale=rationale,
        model=model,
        raw_response=raw_text,
    )


def compute_judge_agreement(
    llm_ranking: Sequence[str] | None,
    deterministic_target: str | Sequence[CandidateScore] | None,
) -> bool | None:
    """Compute top-1 agreement between advisory LLM ranking and deterministic scorer.

    Implements Metric 5 per docs/05-evaluation.md §5.6:
    judge_agreement = P(LLM top-1 = deterministic top-1)

    Args:
        llm_ranking: Ordered candidate plan IDs produced by LLM judge.
        deterministic_target: Either the winning plan_id string, or a sequence of
                              CandidateScore objects from DeterministicCandidateScorer.

    Returns:
        True if the LLM's top-1 choice matches the deterministic top-1 choice,
        False if they disagree,
        None if either ranking or deterministic choice cannot be determined.
    """
    if not llm_ranking:
        return None

    top_llm_plan_id = llm_ranking[0]

    if isinstance(deterministic_target, str):
        return top_llm_plan_id == deterministic_target

    if isinstance(deterministic_target, Sequence):
        scores = [s for s in deterministic_target if isinstance(s, CandidateScore)]
        if not scores:
            return None
        best_score = min(scores, key=lambda s: s.composite)
        return bool(top_llm_plan_id == best_score.plan_id)

    return None


class AdvisoryLLMJudge:
    """Advisory LLM judge evaluating candidate rehearsal evidence via OpenRouter API."""

    def __init__(
        self,
        config: JudgeConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or JudgeConfig.from_settings()
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None or self._owned_client.is_closed:
            self._owned_client = httpx.AsyncClient(timeout=self.config.timeout_seconds)
        return self._owned_client

    async def aclose(self) -> None:
        """Close the owned HTTP client if created."""
        if self._owned_client is not None and not self._owned_client.is_closed:
            await self._owned_client.aclose()
            self._owned_client = None

    async def __aenter__(self) -> "AdvisoryLLMJudge":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    async def evaluate(
        self,
        evidence: Sequence[CandidateEvidence],
    ) -> JudgeEvaluation:
        """Evaluate candidate rehearsal evidence and return an advisory ranking.

        Args:
            evidence: Rehearsal evidence gathered across digital twins.

        Returns:
            JudgeEvaluation with ordered ranking and justifications.
        """
        if not evidence:
            return JudgeEvaluation(model=self.config.model)

        candidate_ids = [e.plan_id for e in evidence]

        if len(evidence) == 1:
            sole_id = candidate_ids[0]
            return JudgeEvaluation(
                ranking=[sole_id],
                reasons={sole_id: "Sole candidate evaluated in tournament rehearsal."},
                rationale=f"Candidate {sole_id} was the only plan rehearsed.",
                model=self.config.model,
            )

        if not self.config.api_key:
            raise JudgeConfigError("OPENROUTER_API_KEY is required for AdvisoryLLMJudge")

        serialized = serialize_evidence_for_judge(evidence)
        system_prompt = build_judge_system_prompt()
        user_prompt = build_judge_user_prompt(serialized)

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "HTTP-Referer": "https://github.com/Harikeshav-R/Understudy",
            "X-Title": "Understudy-Incident-Response",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "response_format": {"type": "json_object"},
        }

        client = await self._get_client()

        try:
            resp = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise JudgeTimeoutError(f"Request to OpenRouter timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise JudgeAPIError(f"Network request to OpenRouter failed: {exc}") from exc

        if resp.status_code != 200:
            raise JudgeAPIError(
                f"OpenRouter returned HTTP {resp.status_code}: {resp.text}",
                details={"status_code": resp.status_code, "body": resp.text},
            )

        try:
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError, TypeError) as exc:
            raise JudgeParseError(
                f"Invalid chat completion envelope from OpenRouter: {exc}",
                details={"response_text": resp.text},
            ) from exc

        logger.info(
            "llm_judge_evaluated",
            model=self.config.model,
            candidate_count=len(candidate_ids),
        )

        return parse_judge_response(content, candidate_ids, model=self.config.model)
