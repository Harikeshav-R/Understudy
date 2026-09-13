"""Unit tests for PlaybookRetriever integrating search and confirmation."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from understudy.contracts.enums import ActionType, FailureClass
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    DeployRef,
    ErrorSignature,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.playbook.confirmation import PlaybookConfirmer
from understudy.playbook.embeddings import OpenRouterEmbeddingClient
from understudy.playbook.retriever import PlaybookRetriever
from understudy.store.api import PlaybookSearchResult
from understudy.store.fakes import FakePlaybookStore


def _make_context() -> IncidentContext:
    fired = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    return IncidentContext(
        incident_id="inc_retriever_001",
        alert=Alert(
            alert_id="alt_001",
            source="synthetic",
            title="SLO breach",
            service="data-service",
            severity="critical",
            fired_at=fired,
            raw={},
        ),
        signatures=[
            ErrorSignature(
                fingerprint="sig_n_plus_one",
                message="N+1 query",
                service="data-service",
                count=50,
                first_seen=fired,
                last_seen=fired,
            )
        ],
        metrics_window=MetricWindow(
            service="data-service",
            start_time=fired,
            end_time=fired,
            series=[],
            p99_latency_ms=1000.0,
            error_rate=0.04,
            request_count=200,
        ),
        recent_deploys=[
            DeployRef(
                commit_sha="c0ffee0000000000000000000000000000000000",
                image_digests={"data-service": "sha256:data000"},
                deployed_at=fired,
                pr_number=102,
                contains_migration=False,
            )
        ],
        dependency_graph=DependencyGraphSnapshot(
            nodes=["data-service"],
            edges=[],
            observed_at=fired,
        ),
        inferred_failure_class=FailureClass.BAD_DEPLOY,
        gathered_at=fired,
    )


def _make_plan() -> RemediationPlan:
    return RemediationPlan(
        plan_id="plan_raw_001",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service", target_commit="deadbeef000"),
        target_resources=[
            ResourceRef(namespace="ust-twin", kind="Deployment", name="data-service")
        ],
        declared_blast_set=["data-service"],
        inverse=None,
        rationale="Rollback to known good commit",
        origin="planner",
    )


@pytest.mark.asyncio
async def test_retriever_match_playbook_confirmed() -> None:
    ctx = _make_context()
    raw_plan = _make_plan()

    cand = PlaybookSearchResult(
        playbook_id="pb_rollback_01",
        failure_class="bad_deploy",
        signature_text="sig text",
        similarity=0.94,
        plan=raw_plan,
        evidence_refs=["run_01"],
        successes=1,
        failures=0,
        origin="seed",
    )

    store = FakePlaybookStore()
    store.search_playbooks_with_scores = AsyncMock(return_value=[cand])  # type: ignore[method-assign]
    store.increment_success = AsyncMock()  # type: ignore[method-assign]

    embedder = OpenRouterEmbeddingClient(embed_caller=AsyncMock(return_value=[0.1] * 1024))
    confirmer = PlaybookConfirmer(
        llm_caller=AsyncMock(
            return_value=(
                '{"retained_playbook_id": "pb_rollback_01", "confidence": 0.95, '
                '"reason": "Safe rollback match"}'
            )
        )
    )

    retriever = PlaybookRetriever(
        store=store,
        embedder=embedder,
        confirmer=confirmer,
    )

    res = await retriever.match_playbook(ctx, top_k=3)
    assert res.matched is True
    assert res.similarity == 0.94
    assert res.playbook_id == "pb_rollback_01"
    assert "Safe rollback match" in res.confirmation_reason
    assert res.plan is not None
    assert res.plan.origin == "playbook"
    assert res.plan.playbook_id == "pb_rollback_01"
    assert "Playbook pb_rollback_01 confirmed" in res.plan.rationale

    # Test retrieve_candidate wrapper
    plan = await retriever.retrieve_candidate(ctx)
    assert plan is not None
    assert plan.origin == "playbook"

    # Test record_outcome (success)
    await retriever.record_outcome("pb_rollback_01", success=True, evidence_run_id="run_100")
    store.increment_success.assert_awaited_once_with("pb_rollback_01")


@pytest.mark.asyncio
async def test_retriever_match_playbook_no_candidates() -> None:
    ctx = _make_context()
    store = FakePlaybookStore()
    store.search_playbooks_with_scores = AsyncMock(return_value=[])  # type: ignore[method-assign]
    embedder = OpenRouterEmbeddingClient(embed_caller=AsyncMock(return_value=[0.1] * 1024))

    retriever = PlaybookRetriever(store=store, embedder=embedder)
    res = await retriever.match_playbook(ctx)

    assert res.matched is False
    assert "No candidate playbooks found" in res.confirmation_reason
    assert res.plan is None

    plan = await retriever.retrieve_candidate(ctx)
    assert plan is None


@pytest.mark.asyncio
async def test_retriever_match_playbook_rejected_by_confirmer() -> None:
    ctx = _make_context()
    raw_plan = _make_plan()
    cand = PlaybookSearchResult(
        playbook_id="pb_rollback_01",
        failure_class="bad_deploy",
        signature_text="sig text",
        similarity=0.75,
        plan=raw_plan,
        evidence_refs=["run_01"],
        origin="seed",
    )

    store = FakePlaybookStore()
    store.search_playbooks_with_scores = AsyncMock(return_value=[cand])  # type: ignore[method-assign]
    store.increment_failure = AsyncMock()  # type: ignore[method-assign]

    embedder = OpenRouterEmbeddingClient(embed_caller=AsyncMock(return_value=[0.1] * 1024))
    confirmer = PlaybookConfirmer(
        llm_caller=AsyncMock(
            return_value=(
                '{"retained_playbook_id": null, "confidence": 0.8, '
                '"reason": "Root cause does not match"}'
            )
        )
    )

    retriever = PlaybookRetriever(store=store, embedder=embedder, confirmer=confirmer)
    res = await retriever.match_playbook(ctx)

    assert res.matched is False
    assert res.similarity == 0.75
    assert "Root cause does not match" in res.confirmation_reason
    assert res.plan is None

    # Test record_outcome (failure)
    await retriever.record_outcome("pb_rollback_01", success=False, evidence_run_id="run_200")
    store.increment_failure.assert_awaited_once_with("pb_rollback_01")
