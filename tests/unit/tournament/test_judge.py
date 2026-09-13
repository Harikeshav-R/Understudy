"""Unit tests for advisory LLM judge (build-plan step A4.4)."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from understudy.contracts.enums import ActionType
from understudy.contracts.evidence import (
    CandidateEvidence,
    CandidateScore,
    ProbeSample,
)
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.contracts.twin import MirrorStats
from understudy.tournament import (
    AdvisoryLLMJudge,
    FakeLLMJudge,
    JudgeAPIError,
    JudgeConfig,
    JudgeConfigError,
    JudgeParseError,
    JudgeTimeoutError,
    LLMJudge,
    build_judge_system_prompt,
    build_judge_user_prompt,
    compute_judge_agreement,
    parse_judge_response,
    serialize_evidence_for_judge,
)


def _make_evidence(
    plan_id: str,
    recovered: bool = True,
    recovery_seconds: float | None = 15.0,
    blast_set: list[str] | None = None,
    downstream_error_delta: float = 0.0,
    violations: list[str] | None = None,
    drop_ratio: float = 0.01,
    probe_latencies: list[float] | None = None,
    evidence_complete: bool = True,
) -> CandidateEvidence:
    now = datetime(2026, 9, 13, 16, 0, 0, tzinfo=UTC)
    latencies = probe_latencies if probe_latencies is not None else [100.0, 110.0, 95.0]
    probes = [
        ProbeSample(
            at=now,
            healthy=recovered,
            p99_latency_ms=lat,
            error_rate=0.0 if recovered else 0.05,
        )
        for lat in latencies
    ]
    delivered = 1000
    dropped = int(delivered * drop_ratio / (1.0 - drop_ratio)) if drop_ratio < 1.0 else 1000
    return CandidateEvidence(
        plan_id=plan_id,
        twin_id=f"twin_{plan_id}",
        applied_at=now,
        probes=probes,
        recovered=recovered,
        recovery_seconds=recovery_seconds,
        observed_blast_set=blast_set or ["data-service"],
        downstream_error_delta=downstream_error_delta,
        invariant_violations=violations or [],
        mirror_stats=MirrorStats(twin_id=f"twin_{plan_id}", delivered=delivered, dropped=dropped),
        evidence_complete=evidence_complete,
    )


def _make_plan(
    plan_id: str,
    action: ActionType = ActionType.ROLLBACK_DEPLOY,
    workload: str = "data-service",
    target_commit: str | None = "abc1234",
    replica_delta: int | None = None,
    flag_name: str | None = None,
    config_key: str | None = None,
    config_value: str | None = None,
    rationale: str = "Test plan rationale",
) -> RemediationPlan:
    return RemediationPlan(
        plan_id=plan_id,
        candidate_index=0,
        action=action,
        params=ActionParams(
            workload=workload,
            target_commit=target_commit,
            replica_delta=replica_delta,
            flag_name=flag_name,
            config_key=config_key,
            config_value=config_value,
        ),
        target_resources=[ResourceRef(namespace="ust-twin", kind="Deployment", name=workload)],
        declared_blast_set=[workload],
        rationale=rationale,
        origin="planner",
    )


def test_protocol_conformance() -> None:
    """Verify AdvisoryLLMJudge and FakeLLMJudge implement LLMJudge protocol."""
    real_judge = AdvisoryLLMJudge(
        config=JudgeConfig(api_key="test-key"),
        client=httpx.AsyncClient(),
    )
    fake_judge = FakeLLMJudge()
    assert isinstance(real_judge, LLMJudge)
    assert isinstance(fake_judge, LLMJudge)


def test_judge_config_from_settings() -> None:
    """Verify JudgeConfig loads defaults from system settings."""
    cfg = JudgeConfig.from_settings()
    assert cfg.model == "anthropic/claude-3.5-sonnet"
    assert "openrouter" in cfg.base_url
    assert cfg.timeout_seconds == 30.0
    assert cfg.temperature == 0.0
    assert cfg.max_tokens == 1024


def test_serialize_evidence_no_score_leakage() -> None:
    """Verify serialized evidence never leaks deterministic scores or weights."""
    ev1 = _make_evidence("plan_1", recovered=True, recovery_seconds=12.5)
    ev2 = _make_evidence(
        "plan_2",
        recovered=False,
        recovery_seconds=None,
        violations=["K3"],
        drop_ratio=0.08,
        evidence_complete=False,
    )
    ev_empty_probes = CandidateEvidence(
        plan_id="plan_empty",
        twin_id="twin_empty",
        applied_at=datetime(2026, 9, 13, 16, 0, 0, tzinfo=UTC),
        probes=[],
        recovered=False,
        recovery_seconds=None,
        observed_blast_set=[],
        downstream_error_delta=0.0,
        invariant_violations=[],
        mirror_stats=MirrorStats(twin_id="twin_empty", delivered=0, dropped=0),
        evidence_complete=True,
    )

    plans = [
        _make_plan("plan_1", action=ActionType.ROLLBACK_DEPLOY, target_commit="sha_foo"),
        _make_plan(
            "plan_2",
            action=ActionType.SCALE_WORKLOAD,
            replica_delta=2,
            target_commit=None,
            flag_name="flag_x",
            config_key="key_y",
            config_value="val_z",
        ),
    ]

    serialized = serialize_evidence_for_judge([ev1, ev2, ev_empty_probes], plans=plans)
    assert len(serialized) == 3

    # Forbidden deterministic terms that must never appear in serialized payloads
    forbidden = {
        "composite",
        "recovery_weight",
        "blast_weight",
        "downstream_weight",
        "violations_weight",
        "disqualification_reason",
        "disqualified",
        "subscore",
    }

    as_str = json.dumps(serialized)
    for term in forbidden:
        assert term not in as_str

    item1 = serialized[0]
    assert item1["plan_id"] == "plan_1"
    assert item1["telemetry"]["recovered"] is True
    assert item1["telemetry"]["recovery_seconds"] == 12.5
    assert item1["telemetry"]["probes"]["total_samples"] == 3
    assert item1["telemetry"]["probes"]["avg_p99_latency_ms"] == 101.67
    assert item1["plan"]["action"] == "rollback_deploy"
    assert item1["plan"]["target_commit"] == "sha_foo"

    item2 = serialized[1]
    assert item2["plan_id"] == "plan_2"
    assert item2["telemetry"]["recovered"] is False
    assert item2["plan"]["replica_delta"] == 2
    assert item2["plan"]["flag_name"] == "flag_x"
    assert item2["plan"]["config_key"] == "key_y"
    assert item2["plan"]["config_value"] == "val_z"

    item_empty = serialized[2]
    assert item_empty["telemetry"]["probes"]["avg_p99_latency_ms"] is None
    assert "plan" not in item_empty  # no matching plan passed


def test_prompts_construction() -> None:
    """Verify system and user prompts are structured cleanly."""
    sys_prompt = build_judge_system_prompt()
    assert "(SRE) advisory judge" in sys_prompt
    assert "ranking" in sys_prompt
    assert "reasons" in sys_prompt
    assert "rationale" in sys_prompt

    serialized = [{"plan_id": "plan_1", "telemetry": {"recovered": True}}]
    user_prompt = build_judge_user_prompt(serialized)
    assert "plan_1" in user_prompt
    assert "```json" in user_prompt


def test_parse_judge_response_clean_json() -> None:
    """Test parsing clean JSON response."""
    raw = json.dumps(
        {
            "ranking": ["plan_1", "plan_2"],
            "reasons": {
                "plan_1": "Clean recovery in 12s",
                "plan_2": "Higher latency",
            },
            "rationale": "Plan 1 recovered much faster.",
        }
    )
    eval_res = parse_judge_response(raw, ["plan_1", "plan_2"], model="claude-3.5-sonnet")
    assert eval_res.ranking == ["plan_1", "plan_2"]
    assert eval_res.reasons["plan_1"] == "Clean recovery in 12s"
    assert eval_res.rationale == "Plan 1 recovered much faster."
    assert eval_res.model == "claude-3.5-sonnet"


def test_parse_judge_response_markdown_blocks() -> None:
    """Test parsing response embedded inside markdown code blocks."""
    raw = """
Here is my advisory evaluation:
```json
{
  "ranking": ["plan_b", "plan_a"],
  "reasons": {
    "plan_b": "Zero blast radius",
    "plan_a": "Slower recovery"
  },
  "rationale": "Plan B prioritized blast containment."
}
```
Thank you.
"""
    eval_res = parse_judge_response(raw, ["plan_a", "plan_b"])
    assert eval_res.ranking == ["plan_b", "plan_a"]
    assert eval_res.reasons["plan_b"] == "Zero blast radius"

    # Variant: markdown code fence without "json" language tag
    raw_no_tag = '```\n{\n  "ranking": ["plan_a", "plan_b"],\n  "reasons": {}\n}\n```'
    eval_no_tag = parse_judge_response(raw_no_tag, ["plan_a", "plan_b"])
    assert eval_no_tag.ranking == ["plan_a", "plan_b"]

    # Variant: multiple code blocks where earlier block is non-json code
    raw_multiple = (
        "Explanation:\n```bash\nkubectl get pods\n```\n"
        'Decision:\n```json\n{"ranking": ["plan_b", "plan_a"], "reasons": {}}\n```'
    )
    eval_multiple = parse_judge_response(raw_multiple, ["plan_a", "plan_b"])
    assert eval_multiple.ranking == ["plan_b", "plan_a"]

    # Variant: code block that doesn't contain JSON raises JudgeParseError
    with pytest.raises(JudgeParseError, match="Failed to parse LLM response as JSON"):
        parse_judge_response("```text\nignorable block\n```", ["plan_a", "plan_b"])


def test_parse_judge_response_sanitization() -> None:
    """Test defensive sanitization: hallucinated IDs filtered, omitted IDs appended, deduped."""
    raw = json.dumps(
        {
            "ranking": ["plan_hallucinated", "plan_2", "plan_2", "plan_random"],
            "reasons": {"plan_2": "Only valid ranked"},
            "rationale": "Messy LLM output",
        }
    )
    # Candidates are plan_1, plan_2, plan_3
    eval_res = parse_judge_response(raw, ["plan_1", "plan_2", "plan_3"])
    # plan_2 was valid, duplicates dropped; plan_1 and plan_3 were omitted so appended at end
    assert eval_res.ranking == ["plan_2", "plan_1", "plan_3"]
    assert eval_res.reasons["plan_2"] == "Only valid ranked"
    # Auto-filled default reasons for omitted candidates
    assert "plan_1" in eval_res.reasons
    assert "plan_3" in eval_res.reasons


def test_parse_judge_response_errors() -> None:
    """Test error handling for malformed JSON or schema violations."""
    with pytest.raises(JudgeParseError, match="Failed to parse LLM response as JSON"):
        parse_judge_response("not a json string at all", ["plan_1"])

    with pytest.raises(JudgeParseError, match="Expected JSON object"):
        parse_judge_response("[1, 2, 3]", ["plan_1"])

    with pytest.raises(JudgeParseError, match="ranking must be a list"):
        parse_judge_response(json.dumps({"ranking": "not-a-list"}), ["plan_1"])

    with pytest.raises(JudgeParseError, match="reasons must be a dictionary"):
        parse_judge_response(json.dumps({"ranking": ["plan_1"], "reasons": "bad"}), ["plan_1"])


def test_compute_judge_agreement() -> None:
    """Verify top-1 agreement computation against string plan_id and CandidateScore sequence."""
    scores = [
        CandidateScore(plan_id="plan_1", composite=0.15),
        CandidateScore(plan_id="plan_2", composite=0.45),
        CandidateScore(plan_id="plan_3", composite=0.85),
    ]

    # Matching string
    assert compute_judge_agreement(["plan_1", "plan_2"], "plan_1") is True
    # Non-matching string
    assert compute_judge_agreement(["plan_2", "plan_1"], "plan_1") is False

    # Matching CandidateScore sequence
    assert compute_judge_agreement(["plan_1", "plan_3"], scores) is True
    # Non-matching CandidateScore sequence
    assert compute_judge_agreement(["plan_2", "plan_1"], scores) is False

    # Empty inputs or None
    assert compute_judge_agreement([], "plan_1") is None
    assert compute_judge_agreement(None, "plan_1") is None
    assert compute_judge_agreement(["plan_1"], None) is None
    assert compute_judge_agreement(["plan_1"], []) is None


@pytest.mark.asyncio
async def test_advisory_llm_judge_empty_or_single() -> None:
    """Verify short-circuit behavior for empty or single candidate evaluations."""
    judge = AdvisoryLLMJudge(config=JudgeConfig(api_key="test-key"))

    # Empty evidence
    res_empty = await judge.evaluate([])
    assert res_empty.ranking == []
    assert res_empty.reasons == {}

    # Single candidate
    ev_single = _make_evidence("plan_only")
    res_single = await judge.evaluate([ev_single])
    assert res_single.ranking == ["plan_only"]
    assert "plan_only" in res_single.reasons
    assert "only plan" in res_single.rationale.lower()


@pytest.mark.asyncio
async def test_advisory_llm_judge_missing_api_key() -> None:
    """Verify missing API key raises JudgeConfigError."""
    judge = AdvisoryLLMJudge(config=JudgeConfig(api_key=None))
    evs = [_make_evidence("p1"), _make_evidence("p2")]
    with pytest.raises(JudgeConfigError, match="OPENROUTER_API_KEY is required"):
        await judge.evaluate(evs)


@pytest.mark.asyncio
async def test_advisory_llm_judge_success_mock() -> None:
    """Verify full evaluate execution over mocked HTTP transport."""
    ev1 = _make_evidence("plan_1", recovered=True, recovery_seconds=10.0)
    ev2 = _make_evidence("plan_2", recovered=True, recovery_seconds=45.0)

    llm_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "ranking": ["plan_1", "plan_2"],
                            "reasons": {
                                "plan_1": "Faster recovery in 10s vs 45s",
                                "plan_2": "Slower recovery",
                            },
                            "rationale": "Plan 1 is demonstrably faster.",
                        }
                    )
                }
            }
        ]
    }

    recorded_headers: dict[str, str] = {}
    recorded_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal recorded_headers, recorded_body
        recorded_headers = dict(request.headers)
        recorded_body = json.loads(request.read())
        return httpx.Response(200, json=llm_payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        judge = AdvisoryLLMJudge(
            config=JudgeConfig(api_key="sk-or-v1-secret-test"),
            client=client,
        )
        eval_result = await judge.evaluate([ev1, ev2])

    assert eval_result.ranking == ["plan_1", "plan_2"]
    assert eval_result.reasons["plan_1"] == "Faster recovery in 10s vs 45s"
    assert recorded_headers["authorization"] == "Bearer sk-or-v1-secret-test"
    assert "Understudy" in recorded_headers["x-title"]
    assert recorded_body["model"] == "anthropic/claude-3.5-sonnet"


@pytest.mark.asyncio
async def test_advisory_llm_judge_api_errors() -> None:
    """Verify HTTP 4xx/5xx and network errors raise typed JudgeAPIError."""
    evs = [_make_evidence("p1"), _make_evidence("p2")]

    # 401 Unauthorized
    def err_401(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Invalid API key")

    async with httpx.AsyncClient(transport=httpx.MockTransport(err_401)) as client:
        judge = AdvisoryLLMJudge(config=JudgeConfig(api_key="bad-key"), client=client)
        with pytest.raises(JudgeAPIError, match="OpenRouter returned HTTP 401"):
            await judge.evaluate(evs)

    # 500 Internal Server Error
    def err_500(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    async with httpx.AsyncClient(transport=httpx.MockTransport(err_500)) as client:
        judge = AdvisoryLLMJudge(config=JudgeConfig(api_key="key"), client=client)
        with pytest.raises(JudgeAPIError, match="OpenRouter returned HTTP 500"):
            await judge.evaluate(evs)


@pytest.mark.asyncio
async def test_advisory_llm_judge_timeout_and_network_error() -> None:
    """Verify timeout and network disconnects raise typed errors."""
    evs = [_make_evidence("p1"), _make_evidence("p2")]

    def timeout_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Read timed out")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as client:
        judge = AdvisoryLLMJudge(config=JudgeConfig(api_key="key"), client=client)
        with pytest.raises(JudgeTimeoutError, match="timed out"):
            await judge.evaluate(evs)

    def network_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(network_handler)) as client:
        judge = AdvisoryLLMJudge(config=JudgeConfig(api_key="key"), client=client)
        with pytest.raises(JudgeAPIError, match="Network request to OpenRouter failed"):
            await judge.evaluate(evs)


@pytest.mark.asyncio
async def test_advisory_llm_judge_malformed_envelope() -> None:
    """Verify response without standard chat completions choices raises JudgeParseError."""
    evs = [_make_evidence("p1"), _make_evidence("p2")]

    def malformed_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "payload"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(malformed_handler)) as client:
        judge = AdvisoryLLMJudge(config=JudgeConfig(api_key="key"), client=client)
        with pytest.raises(JudgeParseError, match="Invalid chat completion envelope"):
            await judge.evaluate(evs)


@pytest.mark.asyncio
async def test_advisory_llm_judge_lifecycle_and_context_manager() -> None:
    """Verify owned client creation, aclose, and async context manager."""
    judge = AdvisoryLLMJudge(config=JudgeConfig(api_key="test-key"))

    # Calling aclose when _owned_client is None
    await judge.aclose()
    assert judge._owned_client is None

    # First get creates client
    client = await judge._get_client()
    assert client is not None
    assert not client.is_closed

    # Second get returns same client while open
    client_cached = await judge._get_client()
    assert client_cached is client

    await judge.aclose()
    assert judge._owned_client is None

    # Calling aclose again when already None
    await judge.aclose()

    # Re-acquisition creates a fresh client
    client2 = await judge._get_client()
    assert client2 is not None
    assert not client2.is_closed

    async with judge as j:
        assert j is judge

    assert judge._owned_client is None


@pytest.mark.asyncio
async def test_fake_llm_judge() -> None:
    """Verify FakeLLMJudge behaviors: empty, ranking override, agreement, and disagreement."""
    fake = FakeLLMJudge(seed=100)

    # Empty
    res_empty = await fake.evaluate([])
    assert res_empty.ranking == []

    ev1 = _make_evidence("plan_slow", recovered=True, recovery_seconds=60.0)
    ev2 = _make_evidence("plan_fast", recovered=True, recovery_seconds=10.0)
    ev3 = _make_evidence("plan_unrecovered", recovered=False, recovery_seconds=None)

    # agree_with_first=True -> sorts recovered by recovery_seconds
    res_default = await fake.evaluate([ev1, ev2, ev3])
    assert res_default.ranking == ["plan_fast", "plan_slow", "plan_unrecovered"]
    assert "plan_fast" in res_default.reasons

    # Explicit ranking override with partial list (auto-appends unranked)
    fake_explicit = FakeLLMJudge(ranking=["plan_slow"])
    res_explicit = await fake_explicit.evaluate([ev1, ev2, ev3])
    assert res_explicit.ranking == ["plan_slow", "plan_fast", "plan_unrecovered"]

    # agree_with_first=False -> reverses candidate list
    fake_disagree = FakeLLMJudge(agree_with_first=False)
    res_disagree = await fake_disagree.evaluate([ev1, ev2, ev3])
    assert res_disagree.ranking == ["plan_unrecovered", "plan_fast", "plan_slow"]
