"""Unit tests for Slack reasoning post formatting and notification delivery."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from understudy.common.config import SecretSettings, Settings
from understudy.common.errors import SlackNotificationError
from understudy.contracts.enums import (
    ActionType,
    FailureClass,
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
from understudy.contracts.incident import Alert, IncidentContext
from understudy.contracts.kernel import InvariantResult, KernelVerdict
from understudy.contracts.plan import ActionParams, RemediationPlan
from understudy.contracts.twin import MirrorStats
from understudy.notify.api import Notifier
from understudy.notify.slack import (
    SlackNotifier,
    _format_action_params,
    build_candidate_table,
    build_decision_analysis,
    build_slack_reasoning_blocks,
    build_slack_reasoning_text,
)


def _make_sample_alert() -> Alert:
    return Alert(
        alert_id="alt_001",
        source="synthetic",
        title="p99 latency breach on data-service",
        service="data-service",
        severity="critical",
        fired_at=datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC),
        raw={},
    )


def _make_sample_context() -> IncidentContext:
    from understudy.contracts.incident import DependencyGraphSnapshot, MetricWindow

    return IncidentContext(
        incident_id="inc_001",
        alert=_make_sample_alert(),
        signatures=[],
        metrics_window=MetricWindow(
            service="data-service",
            start_time=datetime(2026, 9, 16, 11, 55, 0, tzinfo=UTC),
            end_time=datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC),
            series=[],
            p99_latency_ms=1200.0,
            error_rate=0.05,
            request_count=1000,
        ),
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(
            nodes=["data-service"],
            edges=[],
            observed_at=datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC),
        ),
        inferred_failure_class=FailureClass.BAD_DEPLOY,
        gathered_at=datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC),
    )


def _make_sample_plan(
    plan_id: str,
    idx: int,
    action: ActionType = ActionType.ROLLBACK_DEPLOY,
    workload: str = "data-service",
    target_commit: str | None = "c0ffee0000000000",
    replica_delta: int | None = None,
    flag_name: str | None = None,
    config_key: str | None = None,
    config_value: str | None = None,
    with_inverse: bool = True,
) -> RemediationPlan:
    inverse = None
    if with_inverse and action != ActionType.NO_ACTION:
        inverse = RemediationPlan(
            plan_id=f"{plan_id}_inv",
            candidate_index=idx,
            action=action,
            params=ActionParams(workload=workload),
            target_resources=[],
            declared_blast_set=[],
            inverse=None,
            rationale="Inverse plan",
            origin="planner",
        )

    return RemediationPlan(
        plan_id=plan_id,
        candidate_index=idx,
        action=action,
        params=ActionParams(
            workload=workload,
            target_commit=target_commit,
            replica_delta=replica_delta,
            flag_name=flag_name,
            config_key=config_key,
            config_value=config_value,
        ),
        target_resources=[],
        declared_blast_set=[],
        inverse=inverse,
        rationale=f"Rationale for candidate {idx}",
        origin="planner",
    )


def _make_sample_evidence(
    plan_id: str,
    twin_id: str,
    recovered: bool = True,
    recovery_seconds: float | None = 14.2,
    blast_services: list[str] | None = None,
    downstream_delta: float = 0.0,
    delivered: int = 1000,
    dropped: int = 2,
) -> CandidateEvidence:
    return CandidateEvidence(
        plan_id=plan_id,
        twin_id=twin_id,
        applied_at=datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC),
        probes=[
            ProbeSample(
                at=datetime(2026, 9, 16, 12, 1, 0, tzinfo=UTC),
                healthy=recovered,
                p99_latency_ms=95.0 if recovered else 800.0,
                error_rate=0.0,
            )
        ],
        recovered=recovered,
        recovery_seconds=recovery_seconds,
        observed_blast_set=blast_services or [],
        downstream_error_delta=downstream_delta,
        invariant_violations=[],
        mirror_stats=MirrorStats(twin_id=twin_id, delivered=delivered, dropped=dropped),
        evidence_complete=True,
    )


def test_format_action_params() -> None:
    # All fields
    p1 = ActionParams(
        workload="data-service",
        target_commit="1234567890abcdef",
        replica_delta=2,
        flag_name="canary",
        config_key="TIMEOUT",
        config_value="30s",
    )
    res1 = _format_action_params(p1)
    assert "commit=1234567" in res1
    assert "delta=+2" in res1
    assert "flag=canary" in res1
    assert "config=TIMEOUT:30s" in res1

    # Short commit SHA and negative replica delta and None config_value
    p2 = ActionParams(
        workload="data-service",
        target_commit="abc",
        replica_delta=-1,
        config_key="MAX_CONN",
        config_value=None,
    )
    res2 = _format_action_params(p2)
    assert "commit=abc" in res2
    assert "delta=-1" in res2
    assert "config=MAX_CONN:revert" in res2

    # Empty params
    p3 = ActionParams(workload="data-service")
    assert _format_action_params(p3) == "default"


def test_build_candidate_table_empty() -> None:
    assert build_candidate_table([]) == "(No candidate plans evaluated)"
    assert build_candidate_table(None) == "(No candidate plans evaluated)"


def test_build_candidate_table_rich() -> None:
    p0 = _make_sample_plan("plan_0", 0, ActionType.ROLLBACK_DEPLOY)
    p1 = _make_sample_plan("plan_1", 1, ActionType.RESTART_WORKLOAD)
    p2 = _make_sample_plan("plan_2", 2, ActionType.SCALE_WORKLOAD, replica_delta=2)
    p3 = _make_sample_plan("plan_3", 3, ActionType.NO_ACTION, with_inverse=False)

    e0 = _make_sample_evidence("plan_0", "twin_0", recovered=True, recovery_seconds=14.2)
    e1 = _make_sample_evidence("plan_1", "twin_1", recovered=False, recovery_seconds=None)
    e2 = _make_sample_evidence(
        "plan_2", "twin_2", recovered=True, recovery_seconds=42.1, blast_services=["auth-service"]
    )
    # e3 has no mirror stats or is missing
    plans = [p0, p1, p2, p3]
    evidences = [e0, e1, e2]

    scores = [
        CandidateScore(
            plan_id="plan_0",
            composite=0.142,
            components={},
            disqualified=False,
            disqualification_reason=None,
        ),
        CandidateScore(
            plan_id="plan_1",
            composite=1.0,
            components={},
            disqualified=True,
            disqualification_reason="twin_unhealthy",
        ),
        CandidateScore(
            plan_id="plan_2",
            composite=0.520,
            components={},
            disqualified=False,
            disqualification_reason=None,
        ),
        CandidateScore(
            plan_id="plan_3",
            composite=0.950,
            components={},
            disqualified=False,
            disqualification_reason=None,
        ),
    ]
    result = TournamentResult(
        incident_id="inc_001",
        outcome=TournamentOutcome.DECIDED,
        scores=scores,
        winner_plan_id="plan_0",
        runner_up_plan_id="plan_2",
        margin=0.378,
        decided_at=datetime.now(UTC),
    )

    table = build_candidate_table(plans=plans, result=result, evidence=evidences)
    assert "Idx  | Action" in table
    assert "0    | ROLLBACK_DEPLOY" in table
    assert "WINNER" in table
    assert "14.2s" in table
    assert "1    | RESTART_WORKLOAD" in table
    assert "None" in table
    assert "DISQUALIFIED" in table
    assert "2    | SCALE_WORKLOAD" in table
    assert "42.1s" in table
    assert "RUNNER-UP" in table
    assert "3    | NO_ACTION" in table


def test_build_decision_analysis() -> None:
    # 1. Result is None
    assert build_decision_analysis(None) == "No tournament arbitration result recorded."

    # 2. DECIDED with margin and LLM agreement
    p0 = _make_sample_plan("plan_0", 0, ActionType.ROLLBACK_DEPLOY)
    p1 = _make_sample_plan("plan_1", 1, ActionType.SCALE_WORKLOAD)
    e0 = _make_sample_evidence("plan_0", "twin_0", recovered=True, recovery_seconds=15.0)
    e1 = _make_sample_evidence("plan_1", "twin_1", recovered=True, recovery_seconds=45.0)

    res_decided = TournamentResult(
        incident_id="inc_001",
        outcome=TournamentOutcome.DECIDED,
        scores=[
            CandidateScore(
                plan_id="plan_0",
                composite=0.15,
                components={},
                disqualified=False,
                disqualification_reason=None,
            ),
            CandidateScore(
                plan_id="plan_1",
                composite=0.55,
                components={},
                disqualified=False,
                disqualification_reason=None,
            ),
        ],
        winner_plan_id="plan_0",
        runner_up_plan_id="plan_1",
        margin=0.40,
        llm_agreement=True,
        llm_ranking=["plan_0", "plan_1"],
        decided_at=datetime.now(UTC),
    )
    analysis = build_decision_analysis(res_decided, plans=[p0, p1], evidence=[e0, e1])
    assert "*Winner:* Plan 0" in analysis
    assert "*Margin:* 0.400 over Plan 1" in analysis
    assert "recovered primary SLO in 15.0s vs 45.0s" in analysis
    assert "*LLM Advisory Judge:* Agreed (top-1 match)" in analysis

    # 3. DECIDED with runner-up never recovered and disqualified candidate
    e1_unrec = _make_sample_evidence("plan_1", "twin_1", recovered=False, recovery_seconds=None)
    res_disq = TournamentResult(
        incident_id="inc_001",
        outcome=TournamentOutcome.DECIDED,
        scores=[
            CandidateScore(
                plan_id="plan_0",
                composite=0.15,
                components={},
                disqualified=False,
                disqualification_reason=None,
            ),
            CandidateScore(
                plan_id="plan_1",
                composite=1.0,
                components={},
                disqualified=True,
                disqualification_reason="high_drop",
            ),
        ],
        winner_plan_id="plan_0",
        runner_up_plan_id="plan_1",
        margin=0.85,
        decided_at=datetime.now(UTC),
    )
    analysis_disq = build_decision_analysis(res_disq, plans=[p0, p1], evidence=[e0, e1_unrec])
    assert "runner-up never achieved recovery" in analysis_disq
    assert "*Disqualifications:* Plan 1 (high_drop)" in analysis_disq

    # 4. DECIDED where winner is NO_ACTION
    p_no_action = _make_sample_plan("plan_na", 0, ActionType.NO_ACTION, with_inverse=False)
    res_na = TournamentResult(
        incident_id="inc_001",
        outcome=TournamentOutcome.DECIDED,
        scores=[
            CandidateScore(
                plan_id="plan_na",
                composite=0.2,
                components={},
                disqualified=False,
                disqualification_reason=None,
            )
        ],
        winner_plan_id="plan_na",
        runner_up_plan_id=None,
        margin=None,
        decided_at=datetime.now(UTC),
    )
    analysis_na = build_decision_analysis(res_na, plans=[p_no_action])
    assert "Doing nothing (`NO_ACTION`) was the authoritative winner" in analysis_na

    # 5. AMBIGUOUS
    res_ambig = TournamentResult(
        incident_id="inc_001",
        outcome=TournamentOutcome.AMBIGUOUS,
        scores=[],
        margin=0.08,
        decided_at=datetime.now(UTC),
    )
    analysis_ambig = build_decision_analysis(res_ambig)
    assert "Arbitration Outcome: AMBIGUOUS (No Winner)" in analysis_ambig
    assert "0.080" in analysis_ambig

    # 6. NO_VIABLE_CANDIDATE
    res_noviab = TournamentResult(
        incident_id="inc_001",
        outcome=TournamentOutcome.NO_VIABLE_CANDIDATE,
        scores=[],
        decided_at=datetime.now(UTC),
    )
    analysis_noviab = build_decision_analysis(res_noviab)
    assert "Arbitration Outcome: NO VIABLE CANDIDATE" in analysis_noviab


def test_build_slack_reasoning_blocks_verdict_variations() -> None:
    ctx = _make_sample_context()
    plan = _make_sample_plan("plan_0", 0)

    # 1. Verdict PASS
    v_pass = KernelVerdict(
        incident_id="inc_001",
        plan_id="plan_0",
        verdict=KernelVerdictType.PASS,
        results=[
            InvariantResult(
                invariant_id="K1", tier=InvariantTier.PROOF, satisfied=True, reason="ok"
            ),
            InvariantResult(
                invariant_id="K3", tier=InvariantTier.PROOF, satisfied=True, reason="ok"
            ),
        ],
        missing_facts=[],
        solver_ms=25.4,
        human_reason="Diff verified safe",
    )
    blocks_pass = build_slack_reasoning_blocks(
        incident_id="inc_001",
        plan=plan,
        verdict=v_pass,
        context=ctx,
        prod_outcome="resolved",
    )
    assert len(blocks_pass) <= 50
    assert any("Remediated" in str(b) for b in blocks_pass)
    assert any("PASS" in str(b) for b in blocks_pass)
    assert any("Diff verified safe" in str(b) for b in blocks_pass)
    assert any("Production Actuation: ROLLBACK_DEPLOY" in str(b) for b in blocks_pass)

    # 2. Verdict VETO
    v_veto = KernelVerdict(
        incident_id="inc_001",
        plan_id="plan_0",
        verdict=KernelVerdictType.VETO,
        results=[
            InvariantResult(
                invariant_id="K3",
                tier=InvariantTier.PROOF,
                satisfied=False,
                reason="crosses migration",
            ),
        ],
        missing_facts=[],
        solver_ms=18.2,
        human_reason="Vetoed: rollback crosses schema migration boundary",
    )
    blocks_veto = build_slack_reasoning_blocks(
        incident_id="inc_001",
        plan=plan,
        verdict=v_veto,
        context=ctx,
        prod_outcome=None,
    )
    assert any("VETO" in str(b) for b in blocks_veto)
    assert any("crosses schema migration" in str(b) for b in blocks_veto)
    assert any("Production Actuation: NONE" in str(b) for b in blocks_veto)

    # 3. Verdict UNCERTAIN
    v_unc = KernelVerdict(
        incident_id="inc_001",
        plan_id="plan_0",
        verdict=KernelVerdictType.UNCERTAIN,
        results=[],
        missing_facts=["migration_timestamp"],
        solver_ms=5000.0,
        human_reason="Uncertain: missing fact migration_timestamp",
    )
    blocks_unc = build_slack_reasoning_blocks(
        incident_id="inc_001",
        plan=plan,
        verdict=v_unc,
        context=ctx,
        prod_outcome="not_resolved",
    )
    assert any("UNCERTAIN" in str(b) for b in blocks_unc)
    assert any("Missing Facts:" in str(b) and "migration_timestamp" in str(b) for b in blocks_unc)
    assert any("Failed" in str(b) for b in blocks_unc)

    # 4. No verdict, dry-run actuation
    blocks_no_verdict = build_slack_reasoning_blocks(
        incident_id="inc_001",
        plan=plan,
        verdict=None,
        context=None,
        prod_outcome="executed",
    )
    assert any("Not evaluated" in str(b) for b in blocks_no_verdict)
    assert any("Production Actuation: ROLLBACK_DEPLOY" in str(b) for b in blocks_no_verdict)


def test_build_slack_reasoning_blocks_mirror_fidelity_and_run_record() -> None:
    e0 = _make_sample_evidence("p0", "twin_0", delivered=1500, dropped=3)
    e1 = _make_sample_evidence("p1", "twin_1", delivered=1500, dropped=6)

    blocks = build_slack_reasoning_blocks(
        incident_id="inc_999",
        evidence=[e0, e1],
        run_id="run_custom_999",
    )
    context_blocks = [b for b in blocks if b.get("type") == "context"]
    assert len(context_blocks) >= 2

    # Check mirror line
    mirror_str = str(context_blocks[0])
    assert "3,000 reqs delivered" in mirror_str
    assert "9 dropped" in mirror_str

    # Check run record line
    record_str = str(context_blocks[1])
    assert "run_custom_999" in record_str
    assert "ust store get run_custom_999" in record_str


def test_build_slack_reasoning_text() -> None:
    ctx = _make_sample_context()
    plan = _make_sample_plan("plan_0", 0)
    res = TournamentResult(
        incident_id="inc_001",
        outcome=TournamentOutcome.DECIDED,
        scores=[],
        winner_plan_id="plan_0",
        decided_at=datetime.now(UTC),
    )
    verdict = KernelVerdict(
        incident_id="inc_001",
        plan_id="plan_0",
        verdict=KernelVerdictType.PASS,
        results=[],
        missing_facts=[],
        solver_ms=10.0,
        human_reason="ok",
    )

    txt = build_slack_reasoning_text(
        incident_id="inc_001",
        plan=plan,
        result=res,
        verdict=verdict,
        context=ctx,
        prod_outcome="resolved",
    )
    assert "[Understudy] Incident inc_001" in txt
    assert "Status: RESOLVED" in txt
    assert "Winner: plan_0" in txt
    assert "Kernel: PASS" in txt

    # Minimal fallback
    txt_min = build_slack_reasoning_text("inc_002")
    assert "[Understudy] Incident inc_002" in txt_min
    assert "Kernel: N/A" in txt_min


def test_slack_notifier_resolve_credentials() -> None:
    # 1. Explicit credentials
    notifier = SlackNotifier(bot_token="xoxb-test", channel_id="C123")
    token, channel = notifier._resolve_credentials()
    assert token == "xoxb-test"
    assert channel == "C123"

    # 2. From settings
    custom_settings = Settings(
        secrets=SecretSettings(slack_bot_token="xoxb-setting", slack_channel_id="C456")
    )
    with patch("understudy.notify.slack.get_settings", return_value=custom_settings):
        notifier_cfg = SlackNotifier()
        token2, channel2 = notifier_cfg._resolve_credentials()
        assert token2 == "xoxb-setting"
        assert channel2 == "C456"

    # 3. Missing token
    no_token_settings = Settings(
        secrets=SecretSettings(slack_bot_token=None, slack_channel_id="C456")
    )
    with (
        patch("understudy.notify.slack.get_settings", return_value=no_token_settings),
        pytest.raises(SlackNotificationError, match="token is not configured"),
    ):
        SlackNotifier()._resolve_credentials()

    # 4. Missing channel
    no_channel_settings = Settings(
        secrets=SecretSettings(slack_bot_token="xoxb-ok", slack_channel_id=None)
    )
    with (
        patch("understudy.notify.slack.get_settings", return_value=no_channel_settings),
        pytest.raises(SlackNotificationError, match="channel ID is not configured"),
    ):
        SlackNotifier()._resolve_credentials()


@pytest.mark.asyncio
async def test_slack_notifier_post_reasoning_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer xoxb-mock"
        return httpx.Response(200, json={"ok": True, "ts": "1726500000.000100", "channel": "C0123"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = SlackNotifier(bot_token="xoxb-mock", channel_id="C0123", http_client=client)

    resp = await notifier.post_reasoning(
        incident_id="inc_test",
        message="Incident test message",
        prod_outcome="resolved",
    )
    assert resp["ok"] is True
    assert resp["ts"] == "1726500000.000100"

    # Also test notify_slack delegation
    await notifier.notify_slack(incident_id="inc_test", message="Incident test message")


@pytest.mark.asyncio
async def test_slack_notifier_post_reasoning_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = SlackNotifier(bot_token="xoxb-mock", channel_id="C0123", http_client=client)

    with pytest.raises(SlackNotificationError, match="Slack API error: channel_not_found"):
        await notifier.post_reasoning(incident_id="inc_test")


@pytest.mark.asyncio
async def test_slack_notifier_post_reasoning_invalid_json_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(200, text='"not a json object"')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = SlackNotifier(bot_token="xoxb-mock", channel_id="C0123", http_client=client)

    with pytest.raises(SlackNotificationError, match="expected JSON object"):
        await notifier.post_reasoning(incident_id="inc_test")


@pytest.mark.asyncio
async def test_slack_notifier_post_reasoning_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(500, text="Internal Server Error")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = SlackNotifier(bot_token="xoxb-mock", channel_id="C0123", http_client=client)

    with pytest.raises(SlackNotificationError, match="Slack HTTP 500 error"):
        await notifier.post_reasoning(incident_id="inc_test")


@pytest.mark.asyncio
async def test_slack_notifier_post_reasoning_network_failure() -> None:
    async def mock_post(*_: Any, **__: Any) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    client = httpx.AsyncClient()
    client.post = mock_post  # type: ignore[method-assign]
    notifier = SlackNotifier(bot_token="xoxb-mock", channel_id="C0123", http_client=client)

    with pytest.raises(SlackNotificationError, match="Network failure posting to Slack"):
        await notifier.post_reasoning(incident_id="inc_test")


@pytest.mark.asyncio
async def test_slack_notifier_with_default_client_context_manager() -> None:
    # Test branch where self._http_client is None
    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(200, json={"ok": True, "ts": "123.456", "channel": "C999"})

    mock_transport = httpx.MockTransport(handler)

    notifier = SlackNotifier(bot_token="xoxb-mock", channel_id="C999")
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=mock_transport)):
        resp = await notifier.post_reasoning(incident_id="inc_cm")
        assert resp["ok"] is True


@pytest.mark.asyncio
async def test_slack_notifier_escalate_pagerduty_raises() -> None:
    notifier = SlackNotifier(bot_token="xoxb-mock", channel_id="C0123")
    with pytest.raises(NotImplementedError, match=r"step 5\.4"):
        await notifier.escalate_pagerduty("inc_001", "some reason")


def test_slack_notifier_protocol_conformance() -> None:
    notifier = SlackNotifier(bot_token="xoxb-mock", channel_id="C0123")
    assert isinstance(notifier, Notifier)


def test_build_candidate_table_edge_cases() -> None:
    p0 = _make_sample_plan("plan_0", 0)
    # Evidence where recovered is True, but recovery_seconds, blast set, mirror stats are None
    ev_edge = CandidateEvidence(
        plan_id="plan_0",
        twin_id="twin_0",
        applied_at=datetime.now(UTC),
        probes=[],
        recovered=True,
        recovery_seconds=None,
        observed_blast_set=[],
        downstream_error_delta=0.0,
        invariant_violations=[],
        mirror_stats=MirrorStats(twin_id="twin_0", delivered=0, dropped=0),
        evidence_complete=True,
    )
    # Clear observed_blast_set and mirror_stats
    object.__setattr__(ev_edge, "observed_blast_set", None)
    object.__setattr__(ev_edge, "mirror_stats", None)

    # Result with empty scores
    result = TournamentResult(
        incident_id="inc_001",
        outcome=TournamentOutcome.DECIDED,
        scores=[],
        decided_at=datetime.now(UTC),
    )

    table = build_candidate_table(plans=[p0], result=result, evidence=[ev_edge])
    assert "0    | ROLLBACK_DEPLOY" in table
    assert "-        | -       | -      | -       | -" in table

    # Candidate table with empty evidence
    table_no_ev = build_candidate_table(plans=[p0], result=None, evidence=None)
    assert "0    | ROLLBACK_DEPLOY" in table_no_ev


def test_build_decision_analysis_edge_cases() -> None:
    # 1. Winner and runner-up not found in plans, runner-up has no evidence, no disqualified
    res = TournamentResult(
        incident_id="inc_001",
        outcome=TournamentOutcome.DECIDED,
        scores=[
            CandidateScore(
                plan_id="p_win",
                composite=0.1,
                components={},
                disqualified=False,
                disqualification_reason=None,
            ),
            CandidateScore(
                plan_id="p_run",
                composite=0.4,
                components={},
                disqualified=False,
                disqualification_reason=None,
            ),
        ],
        winner_plan_id="p_win",
        runner_up_plan_id="p_run",
        margin=None,
        llm_agreement=None,
        decided_at=datetime.now(UTC),
    )
    ev_win = _make_sample_evidence("p_win", "twin_0", recovered=True, recovery_seconds=10.0)
    analysis = build_decision_analysis(result=res, plans=[], evidence=[ev_win])
    assert "*Winner:* p_win" in analysis
    assert "*Margin:* N/A over p_run" in analysis
    assert "recovered primary SLO in 10.0s." in analysis

    # 2. Runner-up recovered but recovery_seconds is None
    ev_run = CandidateEvidence(
        plan_id="p_run",
        twin_id="twin_1",
        applied_at=datetime.now(UTC),
        probes=[],
        recovered=True,
        recovery_seconds=None,
        observed_blast_set=[],
        downstream_error_delta=0.0,
        invariant_violations=[],
        mirror_stats=MirrorStats(twin_id="twin_1", delivered=10, dropped=0),
        evidence_complete=True,
    )
    analysis2 = build_decision_analysis(result=res, plans=[], evidence=[ev_win, ev_run])
    assert "recovered primary SLO in 10.0s." in analysis2

    # 3. Disqualified plan not in plans
    res_disq = TournamentResult(
        incident_id="inc_001",
        outcome=TournamentOutcome.DECIDED,
        scores=[
            CandidateScore(
                plan_id="p_win",
                composite=0.1,
                components={},
                disqualified=False,
                disqualification_reason=None,
            ),
            CandidateScore(
                plan_id="p_disq",
                composite=1.0,
                components={},
                disqualified=True,
                disqualification_reason="overflow",
            ),
        ],
        winner_plan_id="p_win",
        runner_up_plan_id=None,
        margin=0.9,
        decided_at=datetime.now(UTC),
    )
    analysis3 = build_decision_analysis(result=res_disq, plans=[], evidence=[])
    assert "*Disqualifications:* Plan p_disq (overflow)" in analysis3


def test_build_slack_reasoning_blocks_edge_cases() -> None:
    # 1. No context, plan with workload, prod_outcome "worsened"
    p = _make_sample_plan("plan_0", 0, workload="custom-service", with_inverse=False)
    v_pass = KernelVerdict(
        incident_id="inc_001",
        plan_id="plan_0",
        verdict=KernelVerdictType.PASS,
        results=[],
        missing_facts=[],
        solver_ms=10.0,
        human_reason="ok",
    )
    blocks = build_slack_reasoning_blocks(
        incident_id="inc_custom",
        plan=p,
        verdict=v_pass,
        prod_outcome="worsened",
    )
    assert any("custom-service" in str(b) for b in blocks)
    assert any("Inverse Plan:* `NONE`" in str(b) for b in blocks)

    # 2. No context, no plan, no verdict
    blocks2 = build_slack_reasoning_blocks(incident_id="inc_empty")
    assert any("unknown-service" in str(b) for b in blocks2)
    assert any("Production Actuation: NONE" in str(b) for b in blocks2)

    # 3. Evidence with 0 total delivered
    ev_zero = _make_sample_evidence("plan_0", "twin_0", delivered=0, dropped=0)
    blocks3 = build_slack_reasoning_blocks(incident_id="inc_zero", evidence=[ev_zero])
    assert any("0 reqs delivered" in str(b) for b in blocks3)

    # 4. Context with inferred_failure_class is None
    ctx_no_class = _make_sample_context()
    object.__setattr__(ctx_no_class, "inferred_failure_class", None)
    blocks4 = build_slack_reasoning_blocks(incident_id="inc_nofc", context=ctx_no_class)
    assert any("unspecified" in str(b) for b in blocks4)

    # 5. Evidence where mirror_stats is None
    ev_no_ms = _make_sample_evidence("plan_0", "twin_0")
    object.__setattr__(ev_no_ms, "mirror_stats", None)
    blocks5 = build_slack_reasoning_blocks(incident_id="inc_noms", evidence=[ev_no_ms])
    assert any("0 reqs delivered" in str(b) for b in blocks5)


@pytest.mark.asyncio
async def test_fake_notifier_full_coverage() -> None:
    from understudy.notify.fakes import FakeNotifier

    fake = FakeNotifier()
    p = _make_sample_plan("p0", 0)
    res = TournamentResult(
        incident_id="inc_fake",
        outcome=TournamentOutcome.DECIDED,
        scores=[],
        decided_at=datetime.now(UTC),
    )
    v = KernelVerdict(
        incident_id="inc_fake",
        plan_id="p0",
        verdict=KernelVerdictType.PASS,
        results=[],
        missing_facts=[],
        solver_ms=5.0,
        human_reason="ok",
    )
    ctx = _make_sample_context()

    await fake.notify_slack(
        incident_id="inc_fake",
        message="msg",
        plan=p,
        result=res,
        verdict=v,
        context=ctx,
        plans=[p],
        evidence=[],
        prod_outcome="resolved",
        run_id="run_fake",
    )
    assert len(fake.slack_posts) == 1

    await fake.escalate_pagerduty(
        incident_id="inc_fake",
        reason="escalate reason",
        partial_evidence=[],
    )
    assert len(fake.pagerduty_escalations) == 1
