"""Unit tests for playbook write-back and evidence recording (build-plan B3.6)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from understudy.common.errors import PlaybookEmbeddingError
from understudy.contracts.enums import ActionType, FailureClass
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    ErrorSignature,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.playbook.embeddings import OpenRouterEmbeddingClient
from understudy.playbook.write import PlaybookWriter, write_playbook
from understudy.store.fakes import FakePlaybookStore


def _make_context(service: str = "data-service") -> IncidentContext:
    now = datetime(2026, 9, 13, 14, 0, 0, tzinfo=UTC)
    return IncidentContext(
        incident_id="inc_test_write_01",
        alert=Alert(
            alert_id="alt_test_01",
            source="synthetic",
            service=service,
            title="High Latency Alert",
            severity="critical",
            fired_at=now,
            raw={},
        ),
        signatures=[
            ErrorSignature(
                fingerprint="sig_n_plus_one",
                message="N+1 query execution detected",
                count=100,
                service=service,
                first_seen=now,
                last_seen=now,
            )
        ],
        metrics_window=MetricWindow(
            service=service,
            start_time=now,
            end_time=now,
            series=[],
            p99_latency_ms=1000.0,
            error_rate=0.04,
            request_count=200,
        ),
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(
            nodes=[service],
            edges=[],
            observed_at=now,
        ),
        inferred_failure_class=FailureClass.BAD_DEPLOY,
        gathered_at=now,
    )


def _make_plan(has_inverse: bool = True, playbook_id: str | None = None) -> RemediationPlan:
    inverse = (
        RemediationPlan(
            plan_id="plan_inv_01",
            candidate_index=1,
            action=ActionType.REVERT_CONFIG,
            params=ActionParams(workload="data-service", config_key="POOL_SIZE", config_value="10"),
            target_resources=[ResourceRef(namespace="ust-twin", kind="ConfigMap", name="cfg")],
            declared_blast_set=["data-service"],
            inverse=None,
            rationale="Re-apply pool size",
            origin="planner",
        )
        if has_inverse
        else None
    )
    return RemediationPlan(
        plan_id="plan_orig_01",
        candidate_index=1,
        action=ActionType.REVERT_CONFIG,
        params=ActionParams(workload="data-service", config_key="POOL_SIZE", config_value="20"),
        target_resources=[ResourceRef(namespace="ust-twin", kind="ConfigMap", name="cfg")],
        declared_blast_set=["data-service"],
        inverse=inverse,
        rationale="Revert pool size drift",
        origin="planner" if playbook_id is None else "playbook",
        playbook_id=playbook_id,
    )


@pytest.mark.asyncio
async def test_write_playbook_new_playbook() -> None:
    store = FakePlaybookStore()
    ctx = _make_context()
    plan = _make_plan(has_inverse=True)

    pb_id = await write_playbook(
        context=ctx,
        plan=plan,
        run_id="run_success_001",
        store=store,
        origin="incident",
    )

    assert pb_id.startswith("pb_")

    record = await store.get_playbook_by_signature((await store.list_playbooks())[0].signature_text)
    assert record is not None
    assert record.playbook_id == pb_id
    assert record.successes == 1
    assert record.failures == 0
    assert record.evidence_refs == ["run_success_001"]
    assert record.origin == "incident"
    assert record.plan.origin == "playbook"
    assert record.plan.playbook_id == pb_id
    assert record.plan.candidate_index == 0
    assert "Learned from successful run run_success_001" in record.plan.rationale

    # Verify template inverse
    assert record.plan.inverse is not None
    assert record.plan.inverse.origin == "playbook"
    assert record.plan.inverse.playbook_id == pb_id


@pytest.mark.asyncio
async def test_write_playbook_new_without_inverse_and_inferred_class_none() -> None:
    store = FakePlaybookStore()
    ctx = _make_context()
    ctx = ctx.model_copy(update={"inferred_failure_class": None})
    plan = _make_plan(has_inverse=False)

    pb_id = await write_playbook(
        context=ctx,
        plan=plan,
        run_id="run_success_002",
        store=store,
        origin="shadow",
    )

    record = (await store.list_playbooks())[0]
    assert record.playbook_id == pb_id
    assert record.origin == "shadow"
    assert record.failure_class == FailureClass.BAD_DEPLOY.value
    assert record.plan.inverse is None


@pytest.mark.asyncio
async def test_write_playbook_existing_by_signature_increments_and_appends() -> None:
    store = FakePlaybookStore()
    ctx = _make_context()
    plan = _make_plan()

    # 1. First write creates the playbook
    pb_id_1 = await write_playbook(
        context=ctx,
        plan=plan,
        run_id="run_001",
        store=store,
    )

    # 2. Second write with same signature matches existing
    pb_id_2 = await write_playbook(
        context=ctx,
        plan=plan,
        run_id="run_002",
        store=store,
    )

    assert pb_id_1 == pb_id_2

    record = (await store.list_playbooks())[0]
    assert record.successes == 2
    assert record.evidence_refs == ["run_001", "run_002"]

    # 3. Third write with duplicate run_id does not duplicate in evidence_refs
    pb_id_3 = await write_playbook(
        context=ctx,
        plan=plan,
        run_id="run_002",
        store=store,
    )
    assert pb_id_3 == pb_id_1
    record = (await store.list_playbooks())[0]
    assert record.successes == 3
    assert record.evidence_refs == ["run_001", "run_002"]


@pytest.mark.asyncio
async def test_write_playbook_existing_by_plan_playbook_id() -> None:
    store = FakePlaybookStore()
    ctx = _make_context()
    plan_with_pb = _make_plan(playbook_id="pb_known_001")

    # Seed the playbook first
    await store.save_playbook(
        playbook_id="pb_known_001",
        failure_class=FailureClass.BAD_DEPLOY,
        signature_text="different signature",
        embedding=[0.0] * 1024,
        plan=plan_with_pb,
        evidence_refs=["run_prior"],
        origin="seed",
    )

    # Write-back should recognize existing plan.playbook_id
    pb_id = await write_playbook(
        context=ctx,
        plan=plan_with_pb,
        run_id="run_new_evidence",
        store=store,
    )

    assert pb_id == "pb_known_001"
    records = await store.list_playbooks()
    rec = next(r for r in records if r.playbook_id == "pb_known_001")
    assert rec.successes == 1
    assert "run_new_evidence" in rec.evidence_refs


@pytest.mark.asyncio
async def test_write_playbook_with_embedder_success_and_failure() -> None:
    store = FakePlaybookStore()
    ctx = _make_context()
    plan = _make_plan()

    # Custom embedder success
    embedder = OpenRouterEmbeddingClient(embed_caller=AsyncMock(return_value=[0.42] * 1024))
    pb_id = await write_playbook(
        context=ctx,
        plan=plan,
        run_id="run_emb_01",
        store=store,
        embedder=embedder,
    )
    assert pb_id.startswith("pb_")

    # Embedder failure raises exception rather than silently falling back
    ctx2 = _make_context(service="auth-service")
    failing_embedder = OpenRouterEmbeddingClient(
        embed_caller=AsyncMock(side_effect=RuntimeError("Embedding API down"))
    )
    with pytest.raises(RuntimeError, match="Embedding API down"):
        await write_playbook(
            context=ctx2,
            plan=plan,
            run_id="run_emb_02",
            store=store,
            embedder=failing_embedder,
        )

    # Missing API key / PlaybookEmbeddingError falls back to deterministic signature embedding
    ctx3 = _make_context(service="worker-service")
    key_missing_embedder = OpenRouterEmbeddingClient(
        embed_caller=AsyncMock(side_effect=PlaybookEmbeddingError("API key missing"))
    )
    pb_id_fallback = await write_playbook(
        context=ctx3,
        plan=plan,
        run_id="run_emb_03",
        store=store,
        embedder=key_missing_embedder,
    )
    assert pb_id_fallback.startswith("pb_")


@pytest.mark.asyncio
async def test_playbook_writer_class() -> None:
    store = FakePlaybookStore()
    writer = PlaybookWriter(store=store)
    ctx = _make_context()
    plan = _make_plan()

    pb_id = await writer.write_playbook(
        context=ctx,
        plan=plan,
        run_id="run_writer_01",
        origin="incident",
    )
    assert pb_id.startswith("pb_")


@pytest.mark.asyncio
async def test_write_playbook_plan_playbook_id_not_found_in_store() -> None:
    store = FakePlaybookStore()
    ctx = _make_context()
    plan = _make_plan(playbook_id="pb_nonexistent_id")

    # plan.playbook_id is set, but get_playbook returns None -> falls through
    # to signature lookup or creation of a new playbook.
    pb_id = await write_playbook(
        context=ctx,
        plan=plan,
        run_id="run_fallback_01",
        store=store,
    )
    assert pb_id.startswith("pb_")
    assert pb_id != "pb_nonexistent_id"
    stored = await store.get_playbook(pb_id)
    assert stored is not None
