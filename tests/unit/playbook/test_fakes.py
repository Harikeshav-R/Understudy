"""Unit tests for FakePlaybookLibrary."""

from datetime import UTC, datetime

import pytest

from understudy.contracts.enums import ActionType, FailureClass
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    ErrorSignature,
    IncidentContext,
    MetricWindow,
)
from understudy.playbook.fakes import FakePlaybookLibrary


def _make_context(inferred_fc: FailureClass | None = FailureClass.CONFIG_DRIFT) -> IncidentContext:
    fired = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    return IncidentContext(
        incident_id="inc_fake_001",
        alert=Alert(
            alert_id="alt_001",
            source="synthetic",
            title="SLO breach",
            service="edge-gateway",
            severity="critical",
            fired_at=fired,
            raw={},
        ),
        signatures=[
            ErrorSignature(
                fingerprint="sig_timeout",
                message="timeout",
                service="edge-gateway",
                count=10,
                first_seen=fired,
                last_seen=fired,
            )
        ],
        metrics_window=MetricWindow(
            service="edge-gateway",
            start_time=fired,
            end_time=fired,
            series=[],
            p99_latency_ms=1000.0,
            error_rate=0.05,
            request_count=100,
        ),
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(
            nodes=["edge-gateway"],
            edges=[],
            observed_at=fired,
        ),
        inferred_failure_class=inferred_fc,
        gathered_at=fired,
    )


@pytest.mark.asyncio
async def test_fake_playbook_library_default_candidate() -> None:
    ctx = _make_context()
    lib = FakePlaybookLibrary()

    cand = await lib.retrieve_candidate(ctx)
    assert cand is not None
    assert cand.action == ActionType.REVERT_CONFIG
    assert cand.origin == "playbook"

    match_res = await lib.match_playbook(ctx)
    assert match_res.matched is True
    assert match_res.similarity == 0.92
    assert "fake playbook library" in match_res.confirmation_reason
    assert match_res.plan is not None

    # Test record_outcome
    await lib.record_outcome("pb_known_drift_01", success=True, evidence_run_id="run_999")
    assert len(lib.recorded_outcomes) == 1
    assert lib.recorded_outcomes[0]["playbook_id"] == "pb_known_drift_01"
    assert lib.recorded_outcomes[0]["success"] is True


@pytest.mark.asyncio
async def test_fake_playbook_library_empty_candidate() -> None:
    ctx = _make_context(inferred_fc=None)
    lib = FakePlaybookLibrary(candidate=None)

    cand = await lib.retrieve_candidate(ctx)
    assert cand is None

    match_res = await lib.match_playbook(ctx)
    assert match_res.matched is False
    assert "No candidate playbooks matched" in match_res.confirmation_reason
    assert match_res.plan is None
