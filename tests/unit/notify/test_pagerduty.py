"""Unit tests for PagerDuty escalation client, note formatting, and composite notifier.

Guarantees 100% line and branch coverage per AGENTS.md §6.1.
"""

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from understudy.common.config import SecretSettings, Settings
from understudy.common.errors import PagerDutyNotificationError
from understudy.contracts.enums import (
    ActionType,
    InvariantTier,
    KernelVerdictType,
    TournamentOutcome,
)
from understudy.contracts.evidence import (
    CandidateEvidence,
    CandidateScore,
    ProbeSample,
    TournamentResult,
)
from understudy.contracts.incident import (
    Alert,
    DeployRef,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.kernel import InvariantResult, KernelVerdict
from understudy.contracts.plan import ActionParams, RemediationPlan
from understudy.contracts.twin import MirrorStats
from understudy.notify.api import Notifier
from understudy.notify.composite import CompositeNotifier
from understudy.notify.fakes import FakeNotifier
from understudy.notify.pagerduty import (
    DEFAULT_FROM_EMAIL,
    PagerDutyNotifier,
    build_pagerduty_escalation_note,
    resolve_pagerduty_incident_id,
)
from understudy.notify.slack import SlackNotifier


def _make_sample_context(
    service: str = "data-service",
    raw_alert: dict[str, Any] | None = None,
    alert_id: str = "alt_pd_PD12345",
    p99: float | None = 142.5,
    error_rate: float | None = 0.045,
    with_deploys: bool = True,
) -> IncidentContext:
    """Helper to generate a realistic IncidentContext fixture."""
    now = datetime(2026, 9, 16, 22, 0, 0, tzinfo=UTC)
    alert = Alert(
        alert_id=alert_id,
        source="pagerduty",
        title="High error rate breach on data-service",
        service=service,
        severity="critical",
        fired_at=now,
        raw=raw_alert or {},
    )
    mw = MetricWindow(
        service=service,
        start_time=now,
        end_time=now,
        series=[],
        p99_latency_ms=p99,
        error_rate=error_rate,
        request_count=1250,
    )
    deploys = (
        [
            DeployRef(
                commit_sha="a1b2c3d4e5f67890",
                image_digests={"data-service": "sha256:abc"},
                deployed_at=now,
                contains_migration=True,
            ),
            DeployRef(
                commit_sha="f6e5d4c3b2a10987",
                image_digests={"data-service": "sha256:def"},
                deployed_at=now,
                contains_migration=False,
            ),
        ]
        if with_deploys
        else []
    )
    from understudy.contracts.incident import DependencyGraphSnapshot

    return IncidentContext(
        incident_id="inc_PD12345",
        alert=alert,
        signatures=[],
        metrics_window=mw,
        recent_deploys=deploys,
        dependency_graph=DependencyGraphSnapshot(nodes=["data-service"], edges=[], observed_at=now),
        gathered_at=now,
    )


def _make_sample_plan(
    plan_id: str, idx: int, action: ActionType = ActionType.ROLLBACK_DEPLOY
) -> RemediationPlan:
    """Helper to generate a realistic RemediationPlan."""
    return RemediationPlan(
        plan_id=plan_id,
        candidate_index=idx,
        action=action,
        params=ActionParams(workload="data-service", target_commit="a1b2c3d"),
        target_resources=[],
        declared_blast_set=["data-service"],
        inverse=None,
        rationale=f"Candidate {idx} rollback intervention",
        origin="planner",
    )


def _make_sample_evidence(
    plan_id: str,
    idx: int,
    recovered: bool = True,
    recovery_s: float | None = 42.5,
    blast_count: int = 1,
    drop_ratio: float = 0.012,
) -> CandidateEvidence:
    """Helper to generate CandidateEvidence."""
    now = datetime(2026, 9, 16, 22, 0, 0, tzinfo=UTC)
    twin_id = f"twin_{idx}"
    return CandidateEvidence(
        plan_id=plan_id,
        twin_id=twin_id,
        applied_at=now,
        probes=[
            ProbeSample(
                at=now,
                healthy=recovered,
                p99_latency_ms=95.0 if recovered else 800.0,
                error_rate=0.0,
            )
        ],
        recovered=recovered,
        recovery_seconds=recovery_s,
        observed_blast_set=[f"svc_{i}" for i in range(blast_count)],
        downstream_error_delta=0.0,
        invariant_violations=[],
        mirror_stats=MirrorStats(
            twin_id=twin_id,
            delivered=1000,
            dropped=int(1000 * drop_ratio),
        ),
        evidence_complete=True,
    )


def _make_sample_tournament(winner_id: str = "plan_0", margin: float = 0.22) -> TournamentResult:
    """Helper to generate TournamentResult."""
    now = datetime(2026, 9, 16, 22, 0, 0, tzinfo=UTC)
    return TournamentResult(
        incident_id="inc_001",
        decided_at=now,
        scores=[
            CandidateScore(
                plan_id="plan_0",
                composite=0.885,
                components={"recovery": 0.95, "blast": 1.0},
                disqualified=False,
            ),
            CandidateScore(
                plan_id="plan_1",
                composite=0.665,
                components={"recovery": 0.70, "blast": 0.8},
                disqualified=False,
            ),
        ],
        winner_plan_id=winner_id,
        runner_up_plan_id="plan_1",
        margin=margin,
        outcome=TournamentOutcome.DECIDED,
    )


_DEFAULT = object()


def _make_sample_verdict(
    verdict_type: KernelVerdictType = KernelVerdictType.VETO,
    invariant: str = "K3",
    unsat_core: list[str] | None = None,
    missing_facts: list[str] | None = None,
    satisfied: object = _DEFAULT,
) -> KernelVerdict:
    """Helper to generate KernelVerdict."""
    if satisfied is not _DEFAULT:
        sat_val = satisfied if isinstance(satisfied, bool) else None
    elif verdict_type == KernelVerdictType.PASS:
        sat_val = True
    elif verdict_type == KernelVerdictType.UNCERTAIN:
        sat_val = None
    else:
        sat_val = False
    return KernelVerdict(
        incident_id="inc_001",
        plan_id="plan_0",
        verdict=verdict_type,
        results=[
            InvariantResult(
                invariant_id=invariant,
                tier=InvariantTier.PROOF,
                satisfied=sat_val,
                unsat_core=unsat_core
                if unsat_core is not None
                else (["0002_user"] if sat_val is False else None),
                reason="Migration boundary crossed" if sat_val is not True else "All clear",
            )
        ],
        missing_facts=missing_facts or [],
        solver_ms=23.4,
        human_reason=(
            "Cannot roll back across schema migration 0002_user without database corruption."
        ),
    )


# ---------------------------------------------------------------------------
# ID Resolution Tests
# ---------------------------------------------------------------------------


def test_resolve_pagerduty_incident_id_explicit() -> None:
    assert resolve_pagerduty_incident_id("inc_123", pd_incident_id="PEXPLICIT") == "PEXPLICIT"
    assert resolve_pagerduty_incident_id("inc_123", pd_incident_id="  PEXPLICIT  ") == "PEXPLICIT"


def test_resolve_pagerduty_incident_id_v3_webhook() -> None:
    # 1. With event.data.id
    raw_v3 = {"event": {"id": "ev_001", "data": {"id": "PDV3DATA"}}}
    ctx = _make_sample_context(raw_alert=raw_v3)
    assert resolve_pagerduty_incident_id("inc_123", context=ctx) == "PDV3DATA"

    # 2. With event.id (when data has no id)
    raw_v3_fallback = {"event": {"id": "PDV3EVENT", "data": {}}}
    ctx2 = _make_sample_context(raw_alert=raw_v3_fallback)
    assert resolve_pagerduty_incident_id("inc_123", context=ctx2) == "PDV3EVENT"


def test_resolve_pagerduty_incident_id_v2_webhook() -> None:
    raw_v2 = {"messages": [{"incident": {"id": "PDV2INC"}}]}
    ctx = _make_sample_context(raw_alert=raw_v2)
    assert resolve_pagerduty_incident_id("inc_123", context=ctx) == "PDV2INC"

    # Malformed messages item
    raw_v2_malformed = {"messages": ["not_a_dict"]}
    ctx_bad = _make_sample_context(raw_alert=raw_v2_malformed, alert_id="alt_pd_FALLBACK")
    assert resolve_pagerduty_incident_id("inc_123", context=ctx_bad) == "FALLBACK"


def test_resolve_pagerduty_incident_id_direct_raw_fields() -> None:
    raw_direct = {"incident_id": "PDIRECTINC"}
    ctx = _make_sample_context(raw_alert=raw_direct)
    assert resolve_pagerduty_incident_id("inc_123", context=ctx) == "PDIRECTINC"

    raw_id = {"id": "PDIRECTID"}
    ctx2 = _make_sample_context(raw_alert=raw_id)
    assert resolve_pagerduty_incident_id("inc_123", context=ctx2) == "PDIRECTID"


def test_resolve_pagerduty_incident_id_alert_id_prefix() -> None:
    ctx_alt_pd = _make_sample_context(alert_id="alt_pd_P12345", raw_alert={})
    assert resolve_pagerduty_incident_id("inc_test", context=ctx_alt_pd) == "P12345"

    ctx_alt = _make_sample_context(alert_id="alt_P98765", raw_alert={})
    assert resolve_pagerduty_incident_id("inc_test", context=ctx_alt) == "P98765"


def test_resolve_pagerduty_incident_id_incident_id_prefix_fallback() -> None:
    assert resolve_pagerduty_incident_id("inc_alt_pd_Q111") == "Q111"
    assert resolve_pagerduty_incident_id("inc_alt_Q222") == "Q222"
    assert resolve_pagerduty_incident_id("inc_Q333") == "Q333"
    assert resolve_pagerduty_incident_id("Q444") == "Q444"
    assert resolve_pagerduty_incident_id("") == ""


# ---------------------------------------------------------------------------
# Note Formatting Tests
# ---------------------------------------------------------------------------


def test_build_pagerduty_escalation_note_veto_with_full_evidence() -> None:
    ctx = _make_sample_context()
    plans = [_make_sample_plan("plan_0", 0), _make_sample_plan("plan_1", 1)]
    ev = [_make_sample_evidence("plan_0", 0), _make_sample_evidence("plan_1", 1)]
    res = _make_sample_tournament("plan_0", 0.22)
    verdict = _make_sample_verdict(KernelVerdictType.VETO, "K3")

    note = build_pagerduty_escalation_note(
        incident_id="inc_PD12345",
        reason="K3 migration boundary invariant violation",
        verdict=verdict,
        plans=plans,
        result=res,
        evidence=ev,
        context=ctx,
        run_id="run_inc_PD12345",
    )

    assert "🚨 UNDERSTUDY AUTOMATED INCIDENT ESCALATION" in note
    assert "Incident: inc_PD12345" in note
    assert "Target Service: data-service" in note
    assert "Outcome: ESCALATED" in note
    assert "Production Status: UNTOUCHED" in note
    assert "Verdict:      VETO" in note
    assert "K3 (Migration boundary crossed)" in note
    assert "Unsat Core (K3): 0002_user" in note
    assert "CANDIDATE REMEDIATION SCOREBOARD" in note
    assert "Idx" in note
    assert "ROLLBACK_DEPLOY" in note
    assert "WINNER" in note
    assert "TOURNAMENT ARBITRATION & SAFETY REASONING" in note
    assert "actuation to production was vetoed by Safety Kernel invariant K3" in note
    assert "Telemetry Window: p99 latency: 142.5ms" in note
    assert "Recent Deploys:" in note
    assert "Immutable Run Record: run_inc_PD12345" in note


def test_build_pagerduty_escalation_note_uncertain_verdict() -> None:
    verdict = _make_sample_verdict(
        KernelVerdictType.UNCERTAIN,
        invariant="K8",
        missing_facts=["last_migration_commit_time"],
    )
    note = build_pagerduty_escalation_note(
        incident_id="inc_002",
        reason="Missing facts for safety verification",
        verdict=verdict,
    )
    assert "Verdict:      UNCERTAIN" in note
    assert "Missing Facts: last_migration_commit_time" in note


def test_build_pagerduty_escalation_note_no_verdict_no_context() -> None:
    note = build_pagerduty_escalation_note(
        incident_id="",
        reason="Uncaught pipeline failure",
        verdict=None,
        plans=None,
        result=None,
        evidence=None,
        context=None,
        run_id=None,
    )
    assert "Verdict:      NOT EVALUATED" in note
    assert "(No candidate plans evaluated)" in note
    assert "Immutable Run Record: run_unknown" in note


def test_build_pagerduty_escalation_note_verdict_without_invariant_or_counterexample() -> None:
    verdict = KernelVerdict(
        incident_id="inc_003",
        plan_id="plan_0",
        verdict=KernelVerdictType.PASS,
        results=[],
        missing_facts=[],
        solver_ms=5.0,
        human_reason="Plan passed all invariants",
    )
    note = build_pagerduty_escalation_note(
        incident_id="inc_003",
        reason="Manual escalation",
        verdict=verdict,
    )
    assert "Evaluation:   No invariant flagged" in note
    assert "Counterexample" not in note
    assert "Missing Facts" not in note


def test_build_pagerduty_escalation_note_metrics_window_none_values() -> None:
    ctx = _make_sample_context(p99=None, error_rate=None, with_deploys=False)
    note = build_pagerduty_escalation_note(
        incident_id="inc_004",
        reason="Ambiguous tournament result",
        context=ctx,
    )
    assert "p99 latency: N/A" in note
    assert "error rate: N/A" in note
    assert "Recent Deploys" not in note


# ---------------------------------------------------------------------------
# PagerDutyNotifier Client Tests
# ---------------------------------------------------------------------------


def test_pagerduty_notifier_protocol_conformance() -> None:
    notifier = PagerDutyNotifier(token="mock-token")
    assert isinstance(notifier, Notifier)


def test_pagerduty_notifier_credentials_resolution() -> None:
    # 1. From init parameters
    n1 = PagerDutyNotifier(token="tok-param", from_email="user@example.com")
    t, e = n1._resolve_credentials()
    assert t == "tok-param"
    assert e == "user@example.com"

    # 2. From settings
    fake_settings = Settings(
        secrets=SecretSettings(
            pagerduty_token="tok-env",
            pagerduty_from_email="env@example.com",
        )
    )
    with patch("understudy.notify.pagerduty.get_settings", return_value=fake_settings):
        n2 = PagerDutyNotifier()
        t2, e2 = n2._resolve_credentials()
        assert t2 == "tok-env"
        assert e2 == "env@example.com"

    # 3. Default from email fallback
    fake_settings_no_email = Settings(
        secrets=SecretSettings(
            pagerduty_token="tok-env",
            pagerduty_from_email=None,
        )
    )
    with patch("understudy.notify.pagerduty.get_settings", return_value=fake_settings_no_email):
        n3 = PagerDutyNotifier()
        t3, e3 = n3._resolve_credentials()
        assert t3 == "tok-env"
        assert e3 == DEFAULT_FROM_EMAIL

    # 4. Missing token error
    fake_settings_no_token = Settings(secrets=SecretSettings(pagerduty_token=None))
    with patch("understudy.notify.pagerduty.get_settings", return_value=fake_settings_no_token):
        n4 = PagerDutyNotifier()
        with pytest.raises(
            PagerDutyNotificationError, match="PagerDuty API token is not configured"
        ):
            n4._resolve_credentials()


def test_pagerduty_notifier_build_headers() -> None:
    n = PagerDutyNotifier(token="test")
    h1 = n._build_headers("tok", "dev@example.com")
    assert h1["Authorization"] == "Token token=tok"
    assert h1["From"] == "dev@example.com"
    assert h1["Accept"] == "application/vnd.pagerduty+json;version=2"

    h2 = n._build_headers("tok", None)
    assert "From" not in h2


@pytest.mark.asyncio
async def test_pagerduty_notifier_add_incident_note_success() -> None:
    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        assert request.url.path == "/incidents/P12345/notes"
        assert request.headers["Authorization"] == "Token token=mock-tok"
        assert request.headers["From"] == "dev@example.com"
        body = json.loads(request.content)
        assert body["note"]["content"] == "Test note content"
        return httpx.Response(201, json={"note": {"id": "NOTE123", "content": "Test note content"}})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = PagerDutyNotifier(
        token="mock-tok", from_email="dev@example.com", http_client=mock_client
    )

    result = await notifier.add_incident_note("P12345", "Test note content")
    assert result["note"]["id"] == "NOTE123"
    assert len(recorded_requests) == 1


@pytest.mark.asyncio
async def test_pagerduty_notifier_add_incident_note_with_default_client_context_manager() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"note": {"id": "NOTE200"}})

    mock_transport = httpx.MockTransport(handler)
    notifier = PagerDutyNotifier(token="mock-tok", from_email="dev@example.com")

    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=mock_transport)):
        result = await notifier.add_incident_note("P12345", "Test note content")
        assert result["note"]["id"] == "NOTE200"


@pytest.mark.asyncio
async def test_pagerduty_notifier_add_incident_note_network_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Connection timed out to api.pagerduty.com")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = PagerDutyNotifier(token="mock-tok", http_client=mock_client)

    with pytest.raises(PagerDutyNotificationError, match="Network failure adding PagerDuty note"):
        await notifier.add_incident_note("P12345", "Note")


@pytest.mark.asyncio
async def test_pagerduty_notifier_add_incident_note_http_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "Incident not found"}})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = PagerDutyNotifier(token="mock-tok", http_client=mock_client)

    with pytest.raises(PagerDutyNotificationError, match="PagerDuty add note failed with HTTP 404"):
        await notifier.add_incident_note("P99999", "Note")


@pytest.mark.asyncio
async def test_pagerduty_notifier_add_incident_note_invalid_json() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b'["not_a_dict"]', headers={"Content-Type": "application/json"}
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = PagerDutyNotifier(token="mock-tok", http_client=mock_client)

    with pytest.raises(PagerDutyNotificationError, match="expected JSON object"):
        await notifier.add_incident_note("P12345", "Note")


@pytest.mark.asyncio
async def test_pagerduty_notifier_set_incident_urgency_success() -> None:
    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        assert request.url.path == "/incidents/P12345"
        assert request.method == "PUT"
        body = json.loads(request.content)
        assert body["incident"]["urgency"] == "high"
        return httpx.Response(200, json={"incident": {"id": "P12345", "urgency": "high"}})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = PagerDutyNotifier(token="mock-tok", http_client=mock_client)

    result = await notifier.set_incident_urgency("P12345", urgency="high")
    assert result["incident"]["urgency"] == "high"
    assert len(recorded_requests) == 1


@pytest.mark.asyncio
async def test_pagerduty_notifier_set_incident_urgency_with_default_client() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"incident": {"id": "P12345", "urgency": "low"}})

    mock_transport = httpx.MockTransport(handler)
    notifier = PagerDutyNotifier(token="mock-tok")

    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=mock_transport)):
        result = await notifier.set_incident_urgency("P12345", urgency="low")
        assert result["incident"]["urgency"] == "low"


@pytest.mark.asyncio
async def test_pagerduty_notifier_set_incident_urgency_network_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Read timeout")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = PagerDutyNotifier(token="mock-tok", http_client=mock_client)

    with pytest.raises(
        PagerDutyNotificationError, match="Network failure setting PagerDuty urgency"
    ):
        await notifier.set_incident_urgency("P12345", urgency="high")


@pytest.mark.asyncio
async def test_pagerduty_notifier_set_incident_urgency_http_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Bad Request: invalid urgency value")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = PagerDutyNotifier(token="mock-tok", http_client=mock_client)

    with pytest.raises(
        PagerDutyNotificationError, match="PagerDuty set urgency failed with HTTP 400"
    ):
        await notifier.set_incident_urgency("P12345", urgency="invalid")


@pytest.mark.asyncio
async def test_pagerduty_notifier_set_incident_urgency_invalid_json() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"12345", headers={"Content-Type": "application/json"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = PagerDutyNotifier(token="mock-tok", http_client=mock_client)

    with pytest.raises(PagerDutyNotificationError, match="expected JSON object"):
        await notifier.set_incident_urgency("P12345", urgency="high")


@pytest.mark.asyncio
async def test_pagerduty_notifier_escalate_and_escalate_pagerduty() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            return httpx.Response(201, json={"note": {"id": "N1"}})
        if request.method == "PUT":
            return httpx.Response(200, json={"incident": {"id": "PD12345", "urgency": "high"}})
        return httpx.Response(404)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = PagerDutyNotifier(token="mock-tok", http_client=mock_client)

    ctx = _make_sample_context()
    plans = [_make_sample_plan("plan_0", 0)]
    ev = [_make_sample_evidence("plan_0", 0)]
    res = _make_sample_tournament("plan_0")
    verdict = _make_sample_verdict()

    # Call escalate directly
    resp = await notifier.escalate(
        incident_id="inc_PD12345",
        reason="K3 veto",
        verdict=verdict,
        plans=plans,
        result=res,
        evidence=ev,
        context=ctx,
        urgency="high",
        pd_incident_id="PD12345",
        run_id="run_inc_PD12345",
    )
    assert resp["pd_incident_id"] == "PD12345"
    assert resp["note"]["note"]["id"] == "N1"
    assert resp["incident"]["incident"]["urgency"] == "high"

    # Call escalate_pagerduty Protocol method
    await notifier.escalate_pagerduty(
        incident_id="inc_PD12345",
        reason="K3 veto",
        partial_evidence=ev,
        context=ctx,
        plans=plans,
        verdict=verdict,
        tournament=res,
        urgency="high",
        pd_incident_id="PD12345",
    )
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_pagerduty_notifier_notify_slack_raises() -> None:
    notifier = PagerDutyNotifier(token="mock-tok")
    with pytest.raises(NotImplementedError, match="Slack notification is handled by SlackNotifier"):
        await notifier.notify_slack("inc_001", "some message")


# ---------------------------------------------------------------------------
# Composite Notifier Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_notifier() -> None:
    fake_notifier = FakeNotifier()
    composite = CompositeNotifier(slack=fake_notifier, pagerduty=fake_notifier)

    assert isinstance(composite, Notifier)

    # Test Slack delegation
    await composite.notify_slack(incident_id="inc_comp", message="Slack message")
    assert len(fake_notifier.slack_posts) == 1
    assert fake_notifier.slack_posts[0]["incident_id"] == "inc_comp"

    # Test PagerDuty delegation
    await composite.escalate_pagerduty(
        incident_id="inc_comp",
        reason="PagerDuty escalation",
        urgency="high",
    )
    assert len(fake_notifier.pagerduty_escalations) == 1
    assert fake_notifier.pagerduty_escalations[0]["reason"] == "PagerDuty escalation"
    assert fake_notifier.pagerduty_escalations[0]["urgency"] == "high"


def test_composite_notifier_default_initialization() -> None:
    composite = CompositeNotifier()
    assert isinstance(composite.slack, SlackNotifier)
    assert isinstance(composite.pagerduty, PagerDutyNotifier)


def test_resolve_pagerduty_incident_id_edge_cases() -> None:
    # 1. v3 event with empty data and no event id (branch 62->66)
    raw_empty_v3: dict[str, Any] = {"event": {"data": {}, "id": None}}
    ctx_v3 = _make_sample_context(raw_alert=raw_empty_v3)
    assert resolve_pagerduty_incident_id("inc_123", context=ctx_v3) == "PD12345"

    # 2. v2 messages with empty incident dict (branch 71->75)
    raw_empty_v2: dict[str, Any] = {"messages": [{"incident": {}}]}
    ctx_v2 = _make_sample_context(raw_alert=raw_empty_v2)
    assert resolve_pagerduty_incident_id("inc_123", context=ctx_v2) == "PD12345"

    # 3. alert_id without alt_pd_ or alt_ prefix (branch 84->87)
    ctx_no_prefix = _make_sample_context(alert_id="raw_id_no_prefix", raw_alert={})
    assert resolve_pagerduty_incident_id("inc_fallback", context=ctx_no_prefix) == "fallback"


def test_build_pagerduty_escalation_note_all_satisfied_and_counterexamples() -> None:
    # 1. All invariants satisfied (PASS)
    v_pass = _make_sample_verdict(KernelVerdictType.PASS, "K1", satisfied=True)
    note_pass = build_pagerduty_escalation_note(
        incident_id="inc_pass",
        reason="Testing pass",
        verdict=v_pass,
    )
    assert "1 invariant(s) satisfied" in note_pass

    # 2. Counterexample dict and string
    v_ce_dict = _make_sample_verdict(KernelVerdictType.VETO, "K3")
    object.__setattr__(v_ce_dict, "counterexample", {"diff": "sha1..sha2"})
    note_ce = build_pagerduty_escalation_note(
        incident_id="inc_ce",
        reason="Testing ce dict",
        verdict=v_ce_dict,
    )
    assert "sha1..sha2" in note_ce

    v_ce_str = _make_sample_verdict(KernelVerdictType.VETO, "K3")
    object.__setattr__(v_ce_str, "counterexample", "simple_string_counterexample")
    note_ce_str = build_pagerduty_escalation_note(
        incident_id="inc_ce2",
        reason="Testing ce str",
        verdict=v_ce_str,
    )
    assert "simple_string_counterexample" in note_ce_str

    # 3. Veto with empty results list
    v_empty_res = KernelVerdict(
        incident_id="inc_empty",
        plan_id="plan_0",
        verdict=KernelVerdictType.VETO,
        results=[],
        missing_facts=[],
        solver_ms=2.0,
        human_reason="Vetoed externally",
    )
    note_empty_res = build_pagerduty_escalation_note(
        incident_id="inc_empty",
        reason="Testing empty res veto",
        verdict=v_empty_res,
    )
    assert "Safety Kernel invariant K" in note_empty_res

    # 4. Failed invariant without unsat_core
    v_no_core = _make_sample_verdict(KernelVerdictType.VETO, "K3", unsat_core=[])
    note_no_core = build_pagerduty_escalation_note(
        incident_id="inc_no_core",
        reason="Testing no unsat core",
        verdict=v_no_core,
    )
    assert "Unsat Core" not in note_no_core


@pytest.mark.asyncio
async def test_pagerduty_notifier_escalate_pagerduty_partial_evidence_fallback() -> None:
    notifier = PagerDutyNotifier(token="mock-tok")
    with patch.object(notifier, "escalate", return_value={}) as mock_escalate:
        ctx = _make_sample_context()
        await notifier.escalate_pagerduty(
            incident_id="inc_partial_test",
            reason="testing evidence fallback",
            partial_evidence=None,
            context=ctx,
        )
        assert mock_escalate.called
        _, kwargs = mock_escalate.call_args
        assert kwargs["evidence"] is None
