"""Playbook library retriever implementing pgvector search and LLM confirmation."""

from pydantic import BaseModel, ConfigDict

from understudy.common.config import Settings, get_settings
from understudy.common.logging import get_logger
from understudy.contracts.incident import IncidentContext
from understudy.contracts.plan import RemediationPlan
from understudy.playbook.api import PlaybookLibrary
from understudy.playbook.confirmation import PlaybookConfirmer
from understudy.playbook.embeddings import OpenRouterEmbeddingClient
from understudy.playbook.signature import build_signature_text
from understudy.store.api import PlaybookStore


class PlaybookMatchResult(BaseModel):
    """Detailed result of playbook matching and confirmation."""

    model_config = ConfigDict(frozen=True)

    matched: bool
    plan: RemediationPlan | None = None
    playbook_id: str | None = None
    similarity: float = 0.0
    confirmation_reason: str = ""
    confidence: float = 0.0
    signature_text: str = ""
    candidates_evaluated: int = 0


class PlaybookRetriever(PlaybookLibrary):
    """Production PlaybookLibrary integrating signature text, embeddings, pgvector, and LLM."""

    def __init__(
        self,
        store: PlaybookStore,
        settings: Settings | None = None,
        embedder: OpenRouterEmbeddingClient | None = None,
        confirmer: PlaybookConfirmer | None = None,
    ) -> None:
        self._store = store
        self._settings = settings or get_settings()
        self._embedder = embedder or OpenRouterEmbeddingClient(settings=self._settings)
        self._confirmer = confirmer or PlaybookConfirmer(settings=self._settings)

    async def match_playbook(
        self,
        context: IncidentContext,
        top_k: int = 3,
    ) -> PlaybookMatchResult:
        """Retrieve top candidate playbooks from store and arbitrate with LLM confirmation."""
        logger = get_logger(incident_id=context.incident_id)
        sig_text = build_signature_text(context)
        logger.info(
            "playbook_matching_started",
            service=context.alert.service,
        )

        query_vector = await self._embedder.embed(sig_text)
        candidates = await self._store.search_playbooks_with_scores(query_vector, limit=top_k)

        if not candidates:
            logger.info("playbook_no_candidates_found", incident_id=context.incident_id)
            return PlaybookMatchResult(
                matched=False,
                confirmation_reason="No candidate playbooks found in library for this incident.",
                signature_text=sig_text,
                candidates_evaluated=0,
            )

        logger.info(
            "playbook_candidates_retrieved",
            count=len(candidates),
            top_similarity=candidates[0].similarity,
            top_id=candidates[0].playbook_id,
        )

        confirmation = await self._confirmer.confirm(context, candidates)

        if confirmation.retained_playbook_id is None:
            logger.info(
                "playbook_match_rejected_by_confirmation",
                reason=confirmation.reason,
            )
            return PlaybookMatchResult(
                matched=False,
                similarity=candidates[0].similarity,
                confirmation_reason=confirmation.reason,
                confidence=confirmation.confidence,
                signature_text=sig_text,
                candidates_evaluated=len(candidates),
            )

        # Retained confirmed playbook
        matched_cand = next(
            c for c in candidates if c.playbook_id == confirmation.retained_playbook_id
        )

        # Prepare candidate plan with origin="playbook" per ADR-019
        confirmed_plan = matched_cand.plan.model_copy(
            update={
                "origin": "playbook",
                "playbook_id": matched_cand.playbook_id,
                "rationale": (
                    f"{matched_cand.plan.rationale} "
                    f"(Playbook {matched_cand.playbook_id} confirmed: {confirmation.reason})"
                ),
            }
        )

        logger.info(
            "playbook_match_confirmed",
            playbook_id=matched_cand.playbook_id,
            action=confirmed_plan.action.value,
            similarity=matched_cand.similarity,
        )

        return PlaybookMatchResult(
            matched=True,
            plan=confirmed_plan,
            playbook_id=matched_cand.playbook_id,
            similarity=matched_cand.similarity,
            confirmation_reason=confirmation.reason,
            confidence=confirmation.confidence,
            signature_text=sig_text,
            candidates_evaluated=len(candidates),
        )

    async def retrieve_candidate(self, incident: IncidentContext) -> RemediationPlan | None:
        """Find and confirm a matching playbook candidate for the active incident."""
        match_result = await self.match_playbook(incident)
        return match_result.plan

    async def record_outcome(
        self,
        playbook_id: str,
        success: bool,
        evidence_run_id: str,
    ) -> None:
        """Record the rehearsal or actuation outcome for a matched playbook."""
        if success:
            await self._store.increment_success(playbook_id, evidence_run_id=evidence_run_id)
        else:
            await self._store.increment_failure(playbook_id, evidence_run_id=evidence_run_id)

    async def record_resolved_run(
        self,
        context: IncidentContext,
        plan: RemediationPlan,
        run_id: str,
        origin: str = "incident",
    ) -> str:
        """Upsert a playbook on successful run resolution, keyed by incident signature."""
        from understudy.playbook.write import write_playbook

        return await write_playbook(
            context=context,
            plan=plan,
            run_id=run_id,
            store=self._store,
            embedder=self._embedder,
            origin=origin,
        )


__all__ = [
    "PlaybookMatchResult",
    "PlaybookRetriever",
]
