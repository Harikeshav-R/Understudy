"""Unit tests for playbook LLM confirmation arbiter."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from understudy.common.config import SecretSettings, Settings, TimeoutSettings
from understudy.common.errors import PlaybookConfirmationError
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
from understudy.playbook.confirmation import (
    PlaybookConfirmer,
    build_confirmation_prompt,
    parse_confirmation_response,
)
from understudy.store.api import PlaybookSearchResult


def _make_candidate(
    playbook_id: str = "pb_test_01",
    similarity: float = 0.92,
) -> PlaybookSearchResult:
    plan = RemediationPlan(
        plan_id="plan_001",
        candidate_index=0,
        action=ActionType.ROLLBACK_DEPLOY,
        params=ActionParams(workload="data-service", target_commit="c0ffee0"),
        target_resources=[
            ResourceRef(namespace="ust-twin", kind="Deployment", name="data-service")
        ],
        declared_blast_set=["data-service"],
        inverse=None,
        rationale="Rollback to c0ffee0",
        origin="playbook",
        playbook_id=playbook_id,
    )
    return PlaybookSearchResult(
        playbook_id=playbook_id,
        failure_class="bad_deploy",
        signature_text="failure_class: bad_deploy\naffected_service: data-service",
        similarity=similarity,
        plan=plan,
        evidence_refs=["run_01"],
        successes=2,
        failures=0,
        origin="seed",
    )


def _make_context() -> IncidentContext:
    fired = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    return IncidentContext(
        incident_id="inc_conf_001",
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


def test_build_confirmation_prompt() -> None:
    ctx = _make_context()
    cand = _make_candidate()
    messages = build_confirmation_prompt(ctx, [cand])

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "pb_test_01" in messages[1]["content"]
    assert "data-service" in messages[1]["content"]


def test_build_confirmation_prompt_empty_signatures_and_deploys() -> None:
    fired = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    ctx_empty = IncidentContext(
        incident_id="inc_empty",
        alert=Alert(
            alert_id="alt_001",
            source="synthetic",
            title="SLO",
            service="auth-service",
            severity="error",
            fired_at=fired,
            raw={},
        ),
        signatures=[],
        metrics_window=MetricWindow(
            service="auth-service",
            start_time=fired,
            end_time=fired,
            series=[],
            p99_latency_ms=100.0,
            error_rate=0.0,
            request_count=10,
        ),
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(
            nodes=["auth-service"],
            edges=[],
            observed_at=fired,
        ),
        inferred_failure_class=None,
        gathered_at=fired,
    )
    messages = build_confirmation_prompt(ctx_empty, [])
    assert "- none" in messages[1]["content"]
    assert "Inferred Failure Class: unknown" in messages[1]["content"]


def test_parse_confirmation_response_success() -> None:
    valid_ids = {"pb_test_01", "pb_test_02"}

    # 1. Plain JSON
    raw1 = (
        '{"retained_playbook_id": "pb_test_01", "confidence": 0.95, "reason": "Matches root cause"}'
    )
    res1 = parse_confirmation_response(raw1, valid_ids)
    assert res1.retained_playbook_id == "pb_test_01"
    assert res1.confidence == 0.95
    assert res1.reason == "Matches root cause"

    # 2. Markdown fenced JSON
    raw2 = (
        '```json\n{"retained_playbook_id": "pb_test_02", "confidence": 0.8, '
        '"reason": "Good match"}\n```'
    )
    res2 = parse_confirmation_response(raw2, valid_ids)
    assert res2.retained_playbook_id == "pb_test_02"

    # 3. Rejection (retained_playbook_id: null)
    raw3 = '{"retained_playbook_id": null, "confidence": 1.0, "reason": "Inapplicable action"}'
    res3 = parse_confirmation_response(raw3, valid_ids)
    assert res3.retained_playbook_id is None
    assert res3.reason == "Inapplicable action"

    # 4. Empty reason fallback
    raw4 = '{"retained_playbook_id": null}'
    res4 = parse_confirmation_response(raw4, valid_ids)
    assert res4.retained_playbook_id is None
    assert "No confirmation reason provided" in res4.reason

    # 5. Think tags and conversational wrapper
    raw5 = (
        "<think>Let me evaluate the candidates carefully.</think>\n"
        "Here is the evaluation:\n"
        '{"retained_playbook_id": "pb_test_01", "confidence": 0.9, "reason": "Matched perfectly"}\n'
        "Hope this helps!"
    )
    res5 = parse_confirmation_response(raw5, valid_ids)
    assert res5.retained_playbook_id == "pb_test_01"
    assert res5.confidence == 0.9


def test_parse_confirmation_response_hallucinated_id() -> None:
    valid_ids = {"pb_test_01"}
    raw = '{"retained_playbook_id": "pb_invented_99", "confidence": 0.9, "reason": "Invented plan"}'
    res = parse_confirmation_response(raw, valid_ids)

    # Must reject for safety
    assert res.retained_playbook_id is None
    assert "LLM proposed unknown playbook ID 'pb_invented_99'" in res.reason


def test_parse_confirmation_response_invalid_json() -> None:
    valid_ids = {"pb_test_01"}
    with pytest.raises(PlaybookConfirmationError) as exc:
        parse_confirmation_response("not valid json at all", valid_ids)
    assert "Failed to parse LLM confirmation response" in str(exc.value)


@pytest.mark.asyncio
async def test_playbook_confirmer_empty_candidates() -> None:
    ctx = _make_context()
    confirmer = PlaybookConfirmer()
    res = await confirmer.confirm(ctx, [])
    assert res.retained_playbook_id is None
    assert "No candidate playbooks found" in res.reason


@pytest.mark.asyncio
async def test_playbook_confirmer_custom_caller() -> None:
    ctx = _make_context()
    cand = _make_candidate()
    raw_json = (
        '{"retained_playbook_id": "pb_test_01", "confidence": 0.9, "reason": "Confirmed via mock"}'
    )
    caller = AsyncMock(return_value=raw_json)

    confirmer = PlaybookConfirmer(llm_caller=caller)
    res = await confirmer.confirm(ctx, [cand])

    assert res.retained_playbook_id == "pb_test_01"
    assert res.reason == "Confirmed via mock"
    caller.assert_awaited_once()


@pytest.mark.asyncio
async def test_playbook_confirmer_missing_api_key() -> None:
    ctx = _make_context()
    cand = _make_candidate()
    settings = Settings(
        secrets=SecretSettings(openrouter_api_key=None),
        timeouts=TimeoutSettings(),
    )
    confirmer = PlaybookConfirmer(settings=settings)

    with pytest.raises(PlaybookConfirmationError) as exc:
        await confirmer.confirm(ctx, [cand])
    assert "OPENROUTER_API_KEY is not configured" in str(exc.value)


@pytest.mark.asyncio
async def test_playbook_confirmer_success_and_http_errors() -> None:
    ctx = _make_context()
    cand = _make_candidate()
    settings = Settings(
        secrets=SecretSettings(openrouter_api_key="sk-or-test-key"),
        timeouts=TimeoutSettings(incident_seconds=30),
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    confirmer = PlaybookConfirmer(settings=settings, client=mock_client)

    # 1. Success HTTP 200
    mock_content = (
        '{"retained_playbook_id": "pb_test_01", "confidence": 0.95, "reason": "Safe rollback"}'
    )
    mock_resp_data = {"choices": [{"message": {"content": mock_content}}]}
    mock_client.post = AsyncMock(return_value=httpx.Response(200, json=mock_resp_data))

    res = await confirmer.confirm(ctx, [cand])
    assert res.retained_playbook_id == "pb_test_01"
    assert res.reason == "Safe rollback"

    # 2. HTTP 500 error
    mock_client.post = AsyncMock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(PlaybookConfirmationError) as exc1:
        await confirmer.confirm(ctx, [cand])
    assert "returned status 500" in str(exc1.value)

    # 3. Missing choices
    mock_client.post = AsyncMock(return_value=httpx.Response(200, json={"error": "none"}))
    with pytest.raises(PlaybookConfirmationError) as exc2:
        await confirmer.confirm(ctx, [cand])
    assert "missing choices array" in str(exc2.value)

    # 4. Choice not dict
    mock_client.post = AsyncMock(return_value=httpx.Response(200, json={"choices": ["not_dict"]}))
    with pytest.raises(PlaybookConfirmationError) as exc3:
        await confirmer.confirm(ctx, [cand])
    assert "choice item is not an object" in str(exc3.value)

    # 5. Content not string
    mock_client.post = AsyncMock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": 123}}]})
    )
    with pytest.raises(PlaybookConfirmationError) as exc4:
        await confirmer.confirm(ctx, [cand])
    assert "content is not a string" in str(exc4.value)


@pytest.mark.asyncio
async def test_playbook_confirmer_default_client_context_manager() -> None:
    ctx = _make_context()
    cand = _make_candidate()
    settings = Settings(
        secrets=SecretSettings(openrouter_api_key="sk-or-test-key"),
        timeouts=TimeoutSettings(incident_seconds=30),
    )
    confirmer = PlaybookConfirmer(settings=settings)
    mock_content = '{"retained_playbook_id": "pb_test_01", "confidence": 0.9, "reason": "OK"}'
    mock_resp = httpx.Response(200, json={"choices": [{"message": {"content": mock_content}}]})

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = await confirmer.confirm(ctx, [cand])
        assert res.retained_playbook_id == "pb_test_01"
