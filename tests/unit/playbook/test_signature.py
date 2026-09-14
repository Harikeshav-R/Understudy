"""Unit tests for playbook signature construction and deterministic embeddings."""

import math
from datetime import UTC, datetime

from understudy.contracts.enums import FailureClass
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    DeployRef,
    ErrorSignature,
    IncidentContext,
    MetricWindow,
)
from understudy.playbook.signature import (
    build_signature_text,
    deterministic_signature_embedding,
)


def _make_context(
    inferred_fc: FailureClass | None = FailureClass.BAD_DEPLOY,
    signatures: list[ErrorSignature] | None = None,
    deploys: list[DeployRef] | None = None,
    service: str = "data-service",
) -> IncidentContext:
    fired = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    if signatures is None:
        signatures = [
            ErrorSignature(
                fingerprint="sig_n_plus_one_query",
                message="N+1 query execution detected on /api/items endpoint",
                service="data-service",
                count=142,
                first_seen=fired,
                last_seen=fired,
            ),
            ErrorSignature(
                fingerprint="sig_pool_timeout",
                message="Connection pool timeout",
                service="data-service",
                count=35,
                first_seen=fired,
                last_seen=fired,
            ),
        ]
    if deploys is None:
        deploys = [
            DeployRef(
                commit_sha="c0ffee0000000000000000000000000000000000",
                image_digests={"data-service": "sha256:data000"},
                deployed_at=fired,
                pr_number=102,
                contains_migration=False,
            )
        ]

    return IncidentContext(
        incident_id="inc_test_001",
        alert=Alert(
            alert_id="alt_test_001",
            source="synthetic",
            title="SLO breach",
            service=service,
            severity="critical",
            fired_at=fired,
            raw={},
        ),
        signatures=signatures,
        metrics_window=MetricWindow(
            service=service,
            start_time=fired,
            end_time=fired,
            series=[],
            p99_latency_ms=1200.0,
            error_rate=0.05,
            request_count=500,
        ),
        recent_deploys=deploys,
        dependency_graph=DependencyGraphSnapshot(
            nodes=[service],
            edges=[],
            observed_at=fired,
        ),
        inferred_failure_class=inferred_fc,
        gathered_at=fired,
    )


def test_build_signature_text_full_context() -> None:
    ctx = _make_context()
    sig = build_signature_text(ctx)

    assert "failure_class: bad_deploy" in sig
    assert "affected_service: data-service" in sig
    assert "sig_n_plus_one_query (count: 142" in sig
    assert "sig_pool_timeout (count: 35" in sig
    assert "deploy_proximity: 0s (commit: c0ffee0)" in sig


def test_build_signature_text_edge_cases() -> None:
    # 1. No failure class, empty signatures, empty deploys, empty service
    ctx_empty = _make_context(
        inferred_fc=None,
        signatures=[],
        deploys=[],
        service="",
    )
    sig_empty = build_signature_text(ctx_empty)

    assert "failure_class: unknown" in sig_empty
    assert "affected_service: unknown" in sig_empty
    assert "error_fingerprints: none" in sig_empty
    assert "deploy_proximity: none" in sig_empty

    # 2. Signature ranking by count descending
    fired = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    sig_low = ErrorSignature(
        fingerprint="sig_low",
        message="Low count error",
        service="data-service",
        count=5,
        first_seen=fired,
        last_seen=fired,
    )
    sig_high = ErrorSignature(
        fingerprint="sig_high",
        message="High count error",
        service="data-service",
        count=100,
        first_seen=fired,
        last_seen=fired,
    )
    ctx_ranked = _make_context(signatures=[sig_low, sig_high])
    sig_text_ranked = build_signature_text(ctx_ranked)
    # sig_high should appear before sig_low
    pos_high = sig_text_ranked.find("sig_high")
    pos_low = sig_text_ranked.find("sig_low")
    assert pos_high != -1 and pos_low != -1
    assert pos_high < pos_low


def test_deterministic_signature_embedding() -> None:
    text1 = "failure_class: bad_deploy\naffected_service: data-service"
    text2 = "failure_class: config_drift\naffected_service: edge-gateway"

    emb1 = deterministic_signature_embedding(text1)
    emb1_repeat = deterministic_signature_embedding(text1)
    emb2 = deterministic_signature_embedding(text2)

    assert len(emb1) == 1024
    assert len(emb2) == 1024

    # Exact determinism
    assert emb1 == emb1_repeat

    # Unit norm (L2)
    norm1 = math.sqrt(sum(x * x for x in emb1))
    norm2 = math.sqrt(sum(x * x for x in emb2))
    assert abs(norm1 - 1.0) < 1e-6
    assert abs(norm2 - 1.0) < 1e-6

    # Distinct text produces distinct vector
    assert emb1 != emb2
