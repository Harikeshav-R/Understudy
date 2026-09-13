"""Playbook write-back and evidence recording implementation (build-plan B3.6).

Upserts a playbook keyed by incident signature upon successful resolution on production,
appending the run_id to evidence_refs and incrementing the successes counter.
"""

from understudy.common.clock import Clock, SystemClock
from understudy.common.ids import new_plan_id, new_playbook_id
from understudy.common.logging import get_logger
from understudy.contracts.enums import FailureClass
from understudy.contracts.incident import IncidentContext
from understudy.contracts.plan import RemediationPlan
from understudy.playbook.embeddings import OpenRouterEmbeddingClient
from understudy.playbook.signature import (
    build_signature_text,
    deterministic_signature_embedding,
)
from understudy.store.api import PlaybookStore


async def write_playbook(
    context: IncidentContext,
    plan: RemediationPlan,
    run_id: str,
    store: PlaybookStore,
    embedder: OpenRouterEmbeddingClient | None = None,
    origin: str = "incident",
    clock: Clock | None = None,
) -> str:
    """Upsert a playbook keyed by incident signature, appending run_id to evidence_refs.

    If a playbook matching the incident signature already exists, increments its successes
    counter and appends run_id to evidence_refs. If no matching playbook exists, creates
    a new playbook template with origin (default "incident"), evidence_refs=[run_id],
    and initial successes=1.
    """
    _ = clock
    logger = get_logger(incident_id=context.incident_id)
    sig_text = build_signature_text(context)

    # 1. If plan already specifies a known playbook_id that exists in the store:
    if plan.playbook_id:
        stored_plan = await store.get_playbook(plan.playbook_id)
        if stored_plan is not None:
            existing_id = plan.playbook_id
            await store.increment_success(existing_id, evidence_run_id=run_id)
            logger.info(
                "playbook_success_recorded",
                playbook_id=existing_id,
                run_id=run_id,
            )
            return existing_id

    # 2. Look up by exact signature text
    existing = await store.get_playbook_by_signature(sig_text)
    if existing is not None:
        target_id = existing.playbook_id
        await store.increment_success(target_id, evidence_run_id=run_id)
        logger.info(
            "playbook_updated_existing",
            playbook_id=target_id,
            run_id=run_id,
        )
        return target_id

    # 3. No existing playbook for signature: synthesize a new one
    new_id = new_playbook_id()
    if embedder is not None:
        try:
            embedding = await embedder.embed(sig_text)
        except Exception:
            embedding = deterministic_signature_embedding(sig_text)
    else:
        embedding = deterministic_signature_embedding(sig_text)

    # Prepare reusable remediation plan template with clean identifiers
    template_inverse = (
        plan.inverse.model_copy(
            update={
                "origin": "playbook",
                "playbook_id": new_id,
                "plan_id": new_plan_id(),
            }
        )
        if plan.inverse is not None
        else None
    )
    template_plan = plan.model_copy(
        update={
            "plan_id": new_plan_id(),
            "candidate_index": 0,
            "origin": "playbook",
            "playbook_id": new_id,
            "inverse": template_inverse,
            "rationale": f"{plan.rationale} (Learned from successful run {run_id})",
        }
    )

    fc = context.inferred_failure_class or FailureClass.BAD_DEPLOY
    await store.save_playbook(
        playbook_id=new_id,
        failure_class=fc,
        signature_text=sig_text,
        embedding=embedding,
        plan=template_plan,
        evidence_refs=[run_id],
        origin=origin,
        successes=1,
        failures=0,
    )
    logger.info(
        "playbook_created",
        playbook_id=new_id,
        failure_class=fc.value,
        run_id=run_id,
    )
    return new_id


class PlaybookWriter:
    """Manages playbook persistence and success write-backs."""

    def __init__(
        self,
        store: PlaybookStore,
        embedder: OpenRouterEmbeddingClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._clock = clock or SystemClock()

    async def write_playbook(
        self,
        context: IncidentContext,
        plan: RemediationPlan,
        run_id: str,
        origin: str = "incident",
    ) -> str:
        """Write back a playbook on successful remediation."""
        return await write_playbook(
            context=context,
            plan=plan,
            run_id=run_id,
            store=self._store,
            embedder=self._embedder,
            origin=origin,
            clock=self._clock,
        )


__all__ = [
    "PlaybookWriter",
    "write_playbook",
]
