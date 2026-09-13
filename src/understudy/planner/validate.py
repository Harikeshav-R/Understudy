"""Candidate remediation plan schema validation, retry coordination, and normalisation.

Implements build-plan step B3.2:
- Parse candidate plans to RemediationPlan.
- Reject and re-ask on schema failure (max 2 retries) with error feedback.
- Normalise surviving plans against the live environment:
  - Resolve commit refs to real SHAs and verify workload image digests.
  - Resolve workload names against cluster workloads (drop nonexistent).
  - Compute declared blast set validity against the graph (drop invalid).
- Drop, never repair: any plan referencing a nonexistent workload, commit, or
  digest, or having an invalid blast set, is dropped completely. Surviving plans
  are re-indexed.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from understudy.common.config import Settings, get_settings
from understudy.common.errors import PlannerError
from understudy.common.logging import get_logger
from understudy.contracts.enums import ActionType
from understudy.contracts.incident import (
    DependencyGraphSnapshot,
    DeployRef,
    IncidentContext,
)
from understudy.contracts.plan import ActionParams, RemediationPlan
from understudy.graph.api import DependencyGraph
from understudy.planner.api import Planner
from understudy.planner.inverse import synthesize_inverse
from understudy.planner.prompt import (
    PlannerPromptResponse,
    PromptCandidatePlan,
    parse_raw_planner_json,
    prompt_candidate_to_remediation_plan,
    render_candidate_generation_prompt,
)

logger = get_logger(__name__)


class PlannerSchemaValidationError(PlannerError):
    """Raised when candidate remediation plans violate structural schema constraints."""


def validate_and_parse_plans(
    raw_payload: str | dict[str, Any] | PlannerPromptResponse,
    require_inverse: bool = True,
) -> list[RemediationPlan]:
    """Parse raw payload into candidate RemediationPlans, strictly validating schema.

    Raises PlannerSchemaValidationError on invalid JSON, malformed schema, or rule violations.
    """
    if isinstance(raw_payload, str):
        try:
            parsed_response = parse_raw_planner_json(raw_payload)
        except PlannerError as exc:
            raise PlannerSchemaValidationError(
                f"Schema validation failed during JSON parsing: {exc}",
                details=exc.details,
            ) from exc
    elif isinstance(raw_payload, dict):
        try:
            parsed_response = PlannerPromptResponse.model_validate(raw_payload)
        except ValidationError as exc:
            raise PlannerSchemaValidationError(
                f"Schema validation failed on dictionary payload: {exc}",
                details={"errors": exc.errors()},
            ) from exc
    elif isinstance(raw_payload, PlannerPromptResponse):
        parsed_response = raw_payload
    else:
        raise PlannerSchemaValidationError(
            f"Unsupported payload type for planner validation: {type(raw_payload).__name__}",
            details={"type": type(raw_payload).__name__},
        )

    if not parsed_response.plans:
        raise PlannerSchemaValidationError(
            "Planner response contained empty plans array",
            details={"plans_count": 0},
        )

    plans: list[RemediationPlan] = []
    for idx, candidate in enumerate(parsed_response.plans):
        _validate_candidate_schema(candidate, candidate_index=idx, require_inverse=require_inverse)
        plan = prompt_candidate_to_remediation_plan(candidate, candidate_index=idx)
        plans.append(plan)

    return plans


def _validate_candidate_schema(
    candidate: PromptCandidatePlan,
    candidate_index: int,
    require_inverse: bool = True,
) -> None:
    """Validate action-specific constraints and invariant prerequisites."""
    if not candidate.rationale.strip():
        raise PlannerSchemaValidationError(
            f"Candidate at index {candidate_index} has empty rationale",
            details={"candidate_index": candidate_index},
        )

    # Action specific rules
    if candidate.action == ActionType.NO_ACTION:
        if candidate.inverse is not None:
            raise PlannerSchemaValidationError(
                (
                    f"Candidate at index {candidate_index} is no_action "
                    "but declares non-null inverse"
                ),
                details={"candidate_index": candidate_index},
            )
        if candidate.declared_blast_set:
            raise PlannerSchemaValidationError(
                (
                    f"Candidate at index {candidate_index} is no_action "
                    "but declares non-empty blast set"
                ),
                details={
                    "candidate_index": candidate_index,
                    "declared_blast_set": candidate.declared_blast_set,
                },
            )
    else:
        if require_inverse and candidate.inverse is None:
            raise PlannerSchemaValidationError(
                (
                    f"Candidate at index {candidate_index} ({candidate.action.value}) "
                    "requires a non-null inverse plan"
                ),
                details={"candidate_index": candidate_index, "action": candidate.action.value},
            )
        if not candidate.params.workload:
            raise PlannerSchemaValidationError(
                (
                    f"Candidate at index {candidate_index} ({candidate.action.value}) "
                    "requires non-empty workload"
                ),
                details={"candidate_index": candidate_index, "action": candidate.action.value},
            )

    if candidate.action == ActionType.ROLLBACK_DEPLOY:
        if not candidate.params.target_commit:
            raise PlannerSchemaValidationError(
                f"Candidate at index {candidate_index} (rollback_deploy) requires target_commit",
                details={"candidate_index": candidate_index},
            )

    elif candidate.action == ActionType.SCALE_WORKLOAD:
        if candidate.params.replica_delta is None or candidate.params.replica_delta == 0:
            raise PlannerSchemaValidationError(
                (
                    f"Candidate at index {candidate_index} (scale_workload) "
                    "requires non-zero replica_delta"
                ),
                details={
                    "candidate_index": candidate_index,
                    "replica_delta": candidate.params.replica_delta,
                },
            )

    elif candidate.action == ActionType.DISABLE_FLAG and not candidate.params.flag_name:
        raise PlannerSchemaValidationError(
            f"Candidate at index {candidate_index} (disable_flag) requires flag_name",
            details={"candidate_index": candidate_index},
        )

    elif candidate.action == ActionType.REVERT_CONFIG and (
        not candidate.params.config_key or candidate.params.config_value is None
    ):
        raise PlannerSchemaValidationError(
            (
                f"Candidate at index {candidate_index} (revert_config) "
                "requires config_key and config_value"
            ),
            details={
                "candidate_index": candidate_index,
                "config_key": candidate.params.config_key,
            },
        )


def resolve_commit_ref(
    commit_ref: str | None,
    recent_deploys: list[DeployRef],
    workload: str | None = None,
) -> str | None:
    """Resolve a short or full commit SHA against recent deploys.

    Also verifies that the targeted workload has an image digest recorded in that deploy.
    Returns the resolved full commit SHA if found and valid, or None if nonexistent.
    """
    if not commit_ref:
        return None

    ref = commit_ref.strip()
    if not ref:
        return None

    matching_deploy: DeployRef | None = None

    for deploy in recent_deploys:
        full_sha = deploy.commit_sha
        if full_sha == ref or full_sha.startswith(ref) or ref.startswith(full_sha):
            matching_deploy = deploy
            break

    if matching_deploy is None:
        return None

    if workload is not None:
        if workload not in matching_deploy.image_digests:
            return None
        if not matching_deploy.image_digests[workload]:
            return None

    return matching_deploy.commit_sha


def resolve_workload_name(workload: str, valid_workloads: set[str]) -> str | None:
    """Verify that a workload exists in the active cluster or declared topology."""
    if not workload:
        return None
    normalized = workload.strip()
    if normalized in valid_workloads:
        return normalized
    return None


def compute_blast_set_validity(
    declared_blast_set: list[str],
    target_workload: str,
    graph: DependencyGraphSnapshot | DependencyGraph,
) -> bool:
    """Check that declared blast set is a valid subset of target and its reachable dependents."""
    if isinstance(graph, DependencyGraphSnapshot):
        graph_nodes = set(graph.nodes)
        if target_workload and target_workload not in graph_nodes:
            return False

        # Build target -> callers mapping: edge(source=caller, target=callee)
        target_to_callers: dict[str, set[str]] = {n: set() for n in graph_nodes}
        for edge in graph.edges:
            if edge.target in target_to_callers:
                target_to_callers[edge.target].add(edge.source)

        visited: set[str] = set()
        queue = list(target_to_callers.get(target_workload, set()))
        while queue:
            caller = queue.pop(0)
            if caller not in visited:
                visited.add(caller)
                queue.extend(target_to_callers.get(caller, set()))

        allowable = {target_workload} | visited if target_workload else set()
    else:
        # DependencyGraph protocol instance
        graph_nodes = set(graph.snapshot().nodes)
        if target_workload and target_workload not in graph_nodes:
            return False
        try:
            reachable = graph.reachable_set(target_workload) if target_workload else set()
        except Exception:
            return False
        allowable = {target_workload} | reachable if target_workload else set()

    declared_set = set(declared_blast_set)
    if not declared_set.issubset(graph_nodes):
        return False

    return declared_set.issubset(allowable)


def normalise_plan(
    plan: RemediationPlan,
    context: IncidentContext,
    cluster_workloads: set[str] | None = None,
) -> RemediationPlan | None:
    """Normalise a single candidate plan against the environment.

    Performs environmental resolution:
    - Resolves workload against cluster/graph workloads (drops if nonexistent).
    - Resolves commit refs to real SHAs and digests for rollback (drops if nonexistent).
    - Checks declared blast set against graph reachable closure (drops if invalid).

    Returns the normalised plan or None if the plan must be dropped (never repaired).
    """
    valid_workloads = (
        cluster_workloads if cluster_workloads is not None else set(context.dependency_graph.nodes)
    )

    workload = plan.params.workload

    if plan.action == ActionType.NO_ACTION:
        if plan.declared_blast_set:
            logger.warning(
                "planner_drop_no_action_non_empty_blast",
                plan_id=plan.plan_id,
                declared_blast_set=plan.declared_blast_set,
            )
            return None
        if workload and workload not in valid_workloads:
            logger.warning(
                "planner_drop_nonexistent_workload",
                plan_id=plan.plan_id,
                workload=workload,
            )
            return None
        return plan

    # 1. Workload resolution against cluster/graph
    resolved_workload = resolve_workload_name(workload, valid_workloads)
    if resolved_workload is None:
        logger.warning(
            "planner_drop_nonexistent_workload",
            plan_id=plan.plan_id,
            workload=workload,
        )
        return None

    # Verify target resources
    for res in plan.target_resources:
        if res.name not in valid_workloads:
            logger.warning(
                "planner_drop_nonexistent_target_resource",
                plan_id=plan.plan_id,
                resource_name=res.name,
            )
            return None

    # 2. Commit ref and digest resolution for rollback_deploy
    resolved_commit = plan.params.target_commit
    if plan.action == ActionType.ROLLBACK_DEPLOY:
        resolved_commit = resolve_commit_ref(
            plan.params.target_commit,
            recent_deploys=context.recent_deploys,
            workload=resolved_workload,
        )
        if resolved_commit is None:
            logger.warning(
                "planner_drop_unresolved_commit",
                plan_id=plan.plan_id,
                target_commit=plan.params.target_commit,
                workload=resolved_workload,
            )
            return None

    # 3. Declared blast set validity against graph
    is_blast_valid = compute_blast_set_validity(
        declared_blast_set=plan.declared_blast_set,
        target_workload=resolved_workload,
        graph=context.dependency_graph,
    )
    if not is_blast_valid:
        logger.warning(
            "planner_drop_invalid_blast_set",
            plan_id=plan.plan_id,
            declared_blast_set=plan.declared_blast_set,
            workload=resolved_workload,
        )
        return None

    # 4. Resolve inverse plan if applicable
    normalised_inverse = plan.inverse
    if plan.inverse is not None:
        inv_workload = plan.inverse.params.workload
        if inv_workload and inv_workload not in valid_workloads:
            logger.warning(
                "planner_drop_invalid_inverse_workload",
                plan_id=plan.plan_id,
                inverse_workload=inv_workload,
            )
            return None

        inv_commit = plan.inverse.params.target_commit
        if plan.inverse.action == ActionType.ROLLBACK_DEPLOY and inv_commit:
            resolved_inv_commit = resolve_commit_ref(
                inv_commit,
                recent_deploys=context.recent_deploys,
                workload=inv_workload,
            )
            if resolved_inv_commit is None:
                logger.warning(
                    "planner_drop_unresolved_inverse_commit",
                    plan_id=plan.plan_id,
                    inverse_commit=inv_commit,
                )
                return None
            inv_params = plan.inverse.params.model_copy(
                update={"target_commit": resolved_inv_commit}
            )
            normalised_inverse = plan.inverse.model_copy(update={"params": inv_params})

    updated_params = plan.params.model_copy(
        update={
            "workload": resolved_workload,
            "target_commit": resolved_commit,
        }
    )

    # Synthesize deterministic inverse per action type (B3.4)
    # Never ask the LLM for the inverse or trust an unverified LLM inverse.
    try:
        normalised_inverse = synthesize_inverse(
            plan=plan.model_copy(update={"params": updated_params, "inverse": normalised_inverse}),
            context=context,
            cluster_workloads=valid_workloads,
        )
    except PlannerError as exc:
        logger.warning(
            "planner_drop_inverse_synthesis_failed",
            plan_id=plan.plan_id,
            error=str(exc),
        )
        return None

    return plan.model_copy(
        update={
            "params": updated_params,
            "inverse": normalised_inverse,
        }
    )


def create_no_action_plan(
    context: IncidentContext,
    candidate_index: int = 0,
    plan_id: str | None = None,
    origin: Literal["planner", "playbook", "shadow"] = "planner",
    cluster_workloads: set[str] | None = None,
    rationale: str = "Maintain current state without automated mutation",
) -> RemediationPlan:
    """Synthesize a compliant NO_ACTION remediation candidate."""
    valid_workloads = (
        cluster_workloads if cluster_workloads is not None else set(context.dependency_graph.nodes)
    )
    # Prefer alert service if valid, else first sorted valid workload, else alert service
    workload = "data-service"
    if context.alert and context.alert.service in valid_workloads:
        workload = context.alert.service
    elif valid_workloads:
        workload = sorted(valid_workloads)[0]
    elif context.alert and context.alert.service:
        workload = context.alert.service

    assigned_plan_id = plan_id or f"plan_cand_{candidate_index}"

    return RemediationPlan(
        plan_id=assigned_plan_id,
        candidate_index=candidate_index,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload=workload),
        target_resources=[],
        declared_blast_set=[],
        inverse=None,
        rationale=rationale,
        origin=origin,
    )


def ensure_no_action_candidate(
    plans: list[RemediationPlan],
    context: IncidentContext,
    cluster_workloads: set[str] | None = None,
    origin: Literal["planner", "playbook", "shadow"] = "planner",
) -> list[RemediationPlan]:
    """Ensure a NO_ACTION plan is present in the candidate ballot, appending one if absent."""
    has_no_action = any(plan.action == ActionType.NO_ACTION for plan in plans)
    if has_no_action:
        return list(plans)

    next_index = len(plans)
    no_action_plan = create_no_action_plan(
        context=context,
        candidate_index=next_index,
        origin=origin,
        cluster_workloads=cluster_workloads,
    )
    logger.info(
        "planner_no_action_appended",
        plan_id=no_action_plan.plan_id,
        candidate_index=no_action_plan.candidate_index,
        total_candidates=next_index + 1,
    )
    return [*plans, no_action_plan]


def normalise_plans(
    plans: list[RemediationPlan],
    context: IncidentContext,
    cluster_workloads: set[str] | None = None,
    ensure_no_action: bool = False,
) -> list[RemediationPlan]:
    """Normalise candidate plans against cluster and repo, dropping invalid ones.

    Surviving plans are re-indexed with consecutive candidate_index and aligned plan_ids.
    If ensure_no_action is True, guarantees NO_ACTION is present at the end if absent.
    """
    surviving_plans: list[RemediationPlan] = []
    for plan in plans:
        normalised = normalise_plan(
            plan=plan,
            context=context,
            cluster_workloads=cluster_workloads,
        )
        if normalised is not None:
            surviving_plans.append(normalised)

    # Re-index survivors consecutively (0..k)
    reindexed: list[RemediationPlan] = []
    for idx, plan in enumerate(surviving_plans):
        new_plan_id = f"plan_cand_{idx}"
        reindexed_inv: RemediationPlan | None = None
        if plan.inverse is not None:
            reindexed_inv = plan.inverse.model_copy(
                update={
                    "plan_id": f"{new_plan_id}_inv",
                    "candidate_index": idx,
                }
            )
        reindexed.append(
            plan.model_copy(
                update={
                    "plan_id": new_plan_id,
                    "candidate_index": idx,
                    "inverse": reindexed_inv,
                }
            )
        )

    logger.info(
        "plans_normalised",
        received_count=len(plans),
        retained_count=len(reindexed),
        dropped_count=len(plans) - len(reindexed),
    )
    if ensure_no_action:
        return ensure_no_action_candidate(
            reindexed,
            context=context,
            cluster_workloads=cluster_workloads,
        )
    return reindexed


async def generate_candidates_with_retry(
    llm_caller: Callable[[list[dict[str, str]]], Awaitable[str]],
    context: IncidentContext,
    count: int = 3,
    max_retries: int = 2,
    cluster_workloads: set[str] | None = None,
) -> list[RemediationPlan]:
    """Generate candidate plans with retry on schema failure, then normalise.

    Retries up to max_retries times upon schema failure, supplying explicit error
    feedback. Raises PlannerError if all retries are exhausted.
    Guarantees a NO_ACTION candidate is always appended if absent from surviving plans.
    """
    messages = render_candidate_generation_prompt(context, count=count)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        raw_text = await llm_caller(messages)
        try:
            unnormalised_plans = validate_and_parse_plans(raw_text, require_inverse=False)
            logger.info(
                "planner_schema_validated",
                attempt=attempt,
                count=len(unnormalised_plans),
            )
            normalised = normalise_plans(
                plans=unnormalised_plans,
                context=context,
                cluster_workloads=cluster_workloads,
            )
            return ensure_no_action_candidate(
                plans=normalised,
                context=context,
                cluster_workloads=cluster_workloads,
            )
        except (PlannerSchemaValidationError, PlannerError) as exc:
            last_error = exc
            logger.warning(
                "planner_schema_retry",
                attempt=attempt,
                max_retries=max_retries,
                error=str(exc),
            )
            if attempt < max_retries:
                messages = list(messages)
                messages.append({"role": "assistant", "content": raw_text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Your previous response had schema validation errors:\n{exc}\n"
                            "Please correct the errors and output ONLY valid JSON conforming "
                            "strictly to the schema."
                        ),
                    }
                )

    raise PlannerError(
        f"Planner failed schema validation after {max_retries} retries: {last_error}",
        details={"max_retries": max_retries, "last_error": str(last_error)},
    ) from last_error


class LLMPlanner(Planner):
    """Concrete candidate remediation planner with OpenRouter integration."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        llm_caller: Callable[[list[dict[str, str]]], Awaitable[str]] | None = None,
        cluster_workloads: set[str] | None = None,
        max_retries: int = 2,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._custom_caller = llm_caller
        self.cluster_workloads = cluster_workloads
        self.max_retries = max_retries

    async def _call_llm(self, messages: list[dict[str, str]]) -> str:
        """Call OpenRouter or injected LLM caller with explicit timeout."""
        if self._custom_caller is not None:
            return await self._custom_caller(messages)

        api_key = self.settings.secrets.openrouter_api_key
        if not api_key:
            raise PlannerError(
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
        }
        timeout = float(self.settings.timeouts.incident_seconds)

        if self._client is not None:
            resp = await self._client.post(url, headers=headers, json=payload, timeout=timeout)
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=payload, timeout=timeout)

        if resp.status_code != 200:
            raise PlannerError(
                f"OpenRouter API returned error status {resp.status_code}: {resp.text}",
                details={"status_code": resp.status_code, "response": resp.text[:500]},
            )

        data = resp.json()
        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            raise PlannerError(
                "OpenRouter response missing choices array",
                details={"response": data},
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise PlannerError(
                "OpenRouter choice item is not an object",
                details={"choice": choice},
            )
        content = choice.get("message", {}).get("content", "")
        if not isinstance(content, str):
            raise PlannerError(
                "OpenRouter response message content is not a string",
                details={"content": content},
            )
        return content

    async def generate_candidates(
        self, context: IncidentContext, count: int = 3
    ) -> list[RemediationPlan]:
        """Generate, validate, retry, and normalise candidate plans."""
        return await generate_candidates_with_retry(
            llm_caller=self._call_llm,
            context=context,
            count=count,
            max_retries=self.max_retries,
            cluster_workloads=self.cluster_workloads,
        )


__all__ = [
    "LLMPlanner",
    "PlannerSchemaValidationError",
    "compute_blast_set_validity",
    "create_no_action_plan",
    "ensure_no_action_candidate",
    "generate_candidates_with_retry",
    "normalise_plan",
    "normalise_plans",
    "resolve_commit_ref",
    "resolve_workload_name",
    "synthesize_inverse",
    "validate_and_parse_plans",
]
