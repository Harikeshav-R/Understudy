"""Exhaustive unit tests for tournament SLO probe sampler and recovery detection."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from understudy.common.clock import FrozenClock
from understudy.common.errors import ObservabilityError
from understudy.contracts.enums import ActionType
from understudy.contracts.evidence import ProbeSample
from understudy.contracts.incident import MetricWindow
from understudy.contracts.plan import ActionParams, RemediationPlan
from understudy.contracts.twin import TwinHandle
from understudy.signals.fakes import FakeObservabilityAdapter
from understudy.tournament.api import EnvironmentProbe
from understudy.tournament.fakes import FakeEnvironmentProbe
from understudy.tournament.probe import (
    ProbeConfig,
    ProbeResult,
    ProbeSampler,
    detect_recovery,
)


def test_probe_config_defaults_and_from_settings() -> None:
    """Verify ProbeConfig defaults and from_settings loader."""
    cfg = ProbeConfig()
    assert cfg.service == "edge-gateway"
    assert cfg.poll_interval_seconds == 1.0
    assert cfg.warmup_seconds == 20.0
    assert cfg.consecutive_healthy_threshold == 15
    assert cfg.timeout_seconds == 180.0
    assert cfg.slo_p99_latency_ms == 400.0
    assert cfg.slo_error_rate == 0.02
    assert cfg.min_probe_samples == 60

    loaded = ProbeConfig.from_settings()
    assert loaded.warmup_seconds == 20.0
    assert loaded.consecutive_healthy_threshold == 15
    assert loaded.timeout_seconds == 180.0
    assert loaded.min_probe_samples == 60


def test_detect_recovery_empty_probes() -> None:
    """Empty probe list yields recovered=False, recovery_seconds=None."""
    applied_at = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    recovered, rec_sec = detect_recovery([], applied_at)
    assert recovered is False
    assert rec_sec is None


def test_detect_recovery_warmup_exclusion() -> None:
    """Healthy samples within the 20s warmup window cannot confirm recovery."""
    applied_at = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    # 20 consecutive healthy samples, all inside warmup window [0s, 19s]
    probes = [
        ProbeSample(
            at=applied_at + timedelta(seconds=i),
            healthy=True,
            p99_latency_ms=100.0,
            error_rate=0.0,
        )
        for i in range(20)
    ]
    recovered, rec_sec = detect_recovery(probes, applied_at, warmup_seconds=20.0)
    assert recovered is False
    assert rec_sec is None


def test_detect_recovery_consecutive_success_streak_start_and_end() -> None:
    """15 consecutive healthy samples after warmup triggers recovery."""
    applied_at = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    # 20 warmup samples (unhealthy), followed by 15 healthy samples (seconds 20 to 34)
    warmup_probes = [
        ProbeSample(
            at=applied_at + timedelta(seconds=i),
            healthy=False,
            p99_latency_ms=600.0,
            error_rate=0.1,
        )
        for i in range(20)
    ]
    healthy_probes = [
        ProbeSample(
            at=applied_at + timedelta(seconds=20 + i),
            healthy=True,
            p99_latency_ms=90.0,
            error_rate=0.0,
        )
        for i in range(15)
    ]
    # Intentionally shuffle/unsort to verify sorting inside detect_recovery
    all_probes = healthy_probes[5:] + warmup_probes + healthy_probes[:5]

    rec1, sec1 = detect_recovery(
        all_probes,
        applied_at,
        warmup_seconds=20.0,
        consecutive_healthy_threshold=15,
    )
    assert rec1 is True
    assert sec1 == 20.0

    # Explicit forked_at test: forked 10s before applied_at
    forked_at = applied_at - timedelta(seconds=10)
    rec2, sec2 = detect_recovery(
        all_probes,
        applied_at,
        forked_at=forked_at,
        warmup_seconds=20.0,
        consecutive_healthy_threshold=15,
    )
    assert rec2 is True
    assert sec2 == 20.0


def test_detect_recovery_intermittent_unhealthy_resets_streak() -> None:
    """An unhealthy sample resets the consecutive healthy counter."""
    applied_at = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    probes = []
    # 20s warmup
    for i in range(20):
        probes.append(
            ProbeSample(
                at=applied_at + timedelta(seconds=i),
                healthy=False,
                p99_latency_ms=500.0,
                error_rate=0.05,
            )
        )
    # 14 healthy samples (t=20..33)
    for i in range(14):
        probes.append(
            ProbeSample(
                at=applied_at + timedelta(seconds=20 + i),
                healthy=True,
                p99_latency_ms=100.0,
                error_rate=0.0,
            )
        )
    # 1 unhealthy sample (t=34)
    probes.append(
        ProbeSample(
            at=applied_at + timedelta(seconds=34),
            healthy=False,
            p99_latency_ms=800.0,
            error_rate=0.1,
        )
    )
    # 14 healthy samples again (t=35..48) - neither streak reaches 15!
    for i in range(14):
        probes.append(
            ProbeSample(
                at=applied_at + timedelta(seconds=35 + i),
                healthy=True,
                p99_latency_ms=100.0,
                error_rate=0.0,
            )
        )

    recovered, rec_sec = detect_recovery(
        probes,
        applied_at,
        warmup_seconds=20.0,
        consecutive_healthy_threshold=15,
    )
    assert recovered is False
    assert rec_sec is None

    # Now add the 15th healthy sample (t=49)
    probes.append(
        ProbeSample(
            at=applied_at + timedelta(seconds=49),
            healthy=True,
            p99_latency_ms=100.0,
            error_rate=0.0,
        )
    )
    recovered2, rec_sec2 = detect_recovery(
        probes,
        applied_at,
        warmup_seconds=20.0,
        consecutive_healthy_threshold=15,
    )
    assert recovered2 is True
    # Streak started at t=35
    assert rec_sec2 == 35.0


def test_detect_recovery_timeout_exceeded() -> None:
    """Samples beyond timeout_seconds break recovery evaluation."""
    applied_at = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    # Only 10 healthy samples before timeout (t=170..179), and
    # 5 healthy samples after timeout (t=181..185)
    probes = [
        ProbeSample(
            at=applied_at + timedelta(seconds=170 + i),
            healthy=True,
            p99_latency_ms=100.0,
            error_rate=0.0,
        )
        for i in range(10)
    ]
    probes.extend(
        [
            ProbeSample(
                at=applied_at + timedelta(seconds=181 + i),
                healthy=True,
                p99_latency_ms=100.0,
                error_rate=0.0,
            )
            for i in range(5)
        ]
    )
    recovered, rec_sec = detect_recovery(
        probes,
        applied_at,
        warmup_seconds=20.0,
        timeout_seconds=180.0,
    )
    assert recovered is False
    assert rec_sec is None


class CustomMockObservabilityAdapter(FakeObservabilityAdapter):
    """Mock adapter allowing targeted overrides of metrics and health."""

    def __init__(
        self,
        p99: float | None = 100.0,
        error_rate: float | None = 0.005,
        healthy: bool = True,
        raise_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.override_p99 = p99
        self.override_error_rate = error_rate
        self.override_healthy = healthy
        self.raise_error = raise_error

    async def metric_window(
        self,
        service: str,
        since: datetime,
        namespace: str = "ust-prod",
        until: datetime | None = None,
    ) -> MetricWindow:
        _ = namespace
        if self.raise_error:
            raise self.raise_error
        now = until or datetime.now(UTC)
        return MetricWindow(
            service=service,
            start_time=since,
            end_time=now,
            series=[],
            p99_latency_ms=self.override_p99,
            error_rate=self.override_error_rate,
            request_count=100,
        )

    async def service_health(self, namespace: str, service: str) -> bool:
        _ = (namespace, service)
        if self.raise_error:
            raise self.raise_error
        return self.override_healthy


@pytest.mark.asyncio
async def test_sample_once_healthy() -> None:
    """Sample once returns healthy ProbeSample when SLOs and health pass."""
    adapter = CustomMockObservabilityAdapter(p99=120.0, error_rate=0.01, healthy=True)
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    sampler = ProbeSampler(observability=adapter, clock=clock)

    sample = await sampler.sample_once(namespace="ust-twin-test-0")
    assert sample.healthy is True
    assert sample.p99_latency_ms == 120.0
    assert sample.error_rate == 0.01
    assert sample.at == clock.now()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("p99", "err", "ready", "expected_p99", "expected_err"),
    [
        (450.0, 0.01, True, 450.0, 0.01),  # latency breach
        (100.0, 0.05, True, 100.0, 0.05),  # error rate breach
        (100.0, 0.01, False, 100.0, 0.01),  # readiness failure
        (None, 0.01, True, 0.0, 0.01),  # missing latency metric
        (100.0, None, True, 100.0, 0.0),  # None error rate normalized to 0.0
    ],
)
async def test_sample_once_unhealthy_conditions(
    p99: float | None,
    err: float | None,
    ready: bool,
    expected_p99: float,
    expected_err: float,
) -> None:
    """Sample once correctly yields healthy=False when any SLO condition fails."""
    adapter = CustomMockObservabilityAdapter(p99=p99, error_rate=err, healthy=ready)
    sampler = ProbeSampler(observability=adapter)

    sample = await sampler.sample_once(namespace="ust-twin-test-0")
    # In the (100.0, None, True) case, error_rate becomes 0.0 < 0.02,
    # p99=100.0 < 400, ready=True -> healthy=True
    if p99 == 100.0 and err is None and ready:
        assert sample.healthy is True
    else:
        assert sample.healthy is False
    assert sample.p99_latency_ms == expected_p99
    assert sample.error_rate == expected_err


@pytest.mark.asyncio
async def test_sample_once_observability_error_handled() -> None:
    """Exceptions from observability are caught, logged, and return an unhealthy sample."""
    adapter = CustomMockObservabilityAdapter(raise_error=ObservabilityError("Prometheus timeout"))
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    sampler = ProbeSampler(observability=adapter, clock=clock)

    sample = await sampler.sample_once(namespace="ust-twin-test-0")
    assert sample.healthy is False
    assert sample.p99_latency_ms == 0.0
    assert sample.error_rate == 1.0


@pytest.mark.asyncio
async def test_sample_once_httpx_error_handled() -> None:
    """httpx.HTTPError is gracefully caught and returns an unhealthy sample."""
    adapter = CustomMockObservabilityAdapter(raise_error=httpx.ConnectTimeout("Connection dropped"))
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    sampler = ProbeSampler(observability=adapter, clock=clock)

    sample = await sampler.sample_once(namespace="ust-twin-test-0")
    assert sample.healthy is False
    assert sample.error_rate == 1.0


class StepwiseObservabilityAdapter(FakeObservabilityAdapter):
    """Adapter that transitions from unhealthy to healthy at a specified step."""

    def __init__(self, healthy_after_step: int, clock: FrozenClock) -> None:
        super().__init__()
        self.step = 0
        self.healthy_after_step = healthy_after_step
        self.clock = clock

    async def metric_window(
        self,
        service: str,
        since: datetime,
        namespace: str = "ust-prod",
        until: datetime | None = None,
    ) -> MetricWindow:
        _ = namespace
        self.step += 1
        is_healthy = self.step >= self.healthy_after_step
        now = until or self.clock.now()
        return MetricWindow(
            service=service,
            start_time=since,
            end_time=now,
            series=[],
            p99_latency_ms=90.0 if is_healthy else 600.0,
            error_rate=0.0 if is_healthy else 0.08,
            request_count=100,
        )

    async def service_health(self, namespace: str, service: str) -> bool:
        _ = (namespace, service)
        return True


@pytest.mark.asyncio
async def test_probe_environment_recovery_with_frozen_clock() -> None:
    """probe_environment detects recovery with FrozenClock and 0 wall-clock delay."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    # Becomes healthy at step 21 (after 20s warmup). 15 samples needed -> recovers at step 35
    adapter = StepwiseObservabilityAdapter(healthy_after_step=21, clock=clock)
    config = ProbeConfig(
        warmup_seconds=20.0,
        consecutive_healthy_threshold=15,
        timeout_seconds=180.0,
        min_probe_samples=35,
    )
    sampler = ProbeSampler(observability=adapter, clock=clock, config=config)

    applied_at = clock.now()
    result: ProbeResult = await sampler.probe_environment(
        namespace="ust-twin-test-0",
        applied_at=applied_at,
        min_samples=35,
    )

    assert result.recovered is True
    # In streak_start mode, streak began at sample 21 (20s elapsed from applied_at)
    assert result.recovery_seconds == 20.0
    assert result.timeout_exceeded is False
    assert len(result.probes) == 35


@pytest.mark.asyncio
async def test_probe_environment_with_forked_at_reference() -> None:
    """probe_environment calculates recovery relative to applied_at when forked_at differs."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    forked_at = clock.now() - timedelta(seconds=10.0)  # forked 10s earlier
    adapter = StepwiseObservabilityAdapter(healthy_after_step=15, clock=clock)
    config = ProbeConfig(
        warmup_seconds=20.0,
        consecutive_healthy_threshold=5,
        timeout_seconds=50.0,
        min_probe_samples=20,
    )
    sampler = ProbeSampler(observability=adapter, clock=clock, config=config)

    applied_at = clock.now()
    result = await sampler.probe_environment(
        namespace="ust-twin-test-0",
        applied_at=applied_at,
        forked_at=forked_at,
        min_samples=20,
    )

    assert result.recovered is True
    assert result.recovery_seconds is not None
    assert result.timeout_exceeded is False


@pytest.mark.asyncio
async def test_probe_environment_timeout_exceeded() -> None:
    """probe_environment reports timeout_exceeded when threshold not achieved within timeout."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    # Never healthy
    adapter = CustomMockObservabilityAdapter(p99=800.0, error_rate=0.1, healthy=True)
    config = ProbeConfig(
        warmup_seconds=5.0,
        consecutive_healthy_threshold=10,
        timeout_seconds=15.0,
        min_probe_samples=5,
    )
    sampler = ProbeSampler(observability=adapter, clock=clock, config=config)

    applied_at = clock.now()
    result = await sampler.probe_environment(
        namespace="ust-twin-test-0",
        applied_at=applied_at,
        min_samples=5,
    )

    assert result.recovered is False
    assert result.recovery_seconds is None
    assert result.timeout_exceeded is True


@pytest.mark.asyncio
async def test_probe_environment_recovered_runs_until_timeout_when_not_stopping() -> None:
    """When stop_on_recovery=False, probe continues sampling until timeout even after recovery."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    adapter = CustomMockObservabilityAdapter(p99=100.0, error_rate=0.005, healthy=True)
    config = ProbeConfig(
        warmup_seconds=0.0,
        consecutive_healthy_threshold=2,
        timeout_seconds=5.0,
        min_probe_samples=1,
    )
    sampler = ProbeSampler(observability=adapter, clock=clock, config=config)

    applied_at = clock.now()
    result = await sampler.probe_environment(
        namespace="ust-twin-test-0",
        applied_at=applied_at,
        stop_on_recovery=False,
    )

    assert result.recovered is True
    assert result.recovery_seconds == 0.0
    assert result.timeout_exceeded is False
    assert len(result.probes) >= 5


@pytest.mark.asyncio
async def test_probe_twins_empty_plans() -> None:
    """probe_twins returns empty dict when no plans or matching twins provided."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    adapter = FakeObservabilityAdapter(clock=clock)
    sampler = ProbeSampler(observability=adapter, clock=clock)

    results = await sampler.probe_twins(twins=[], plans=[])
    assert results == {}


@pytest.mark.asyncio
async def test_probe_twins_concurrent() -> None:
    """probe_twins probes multiple twin namespaces concurrently."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    adapter = FakeObservabilityAdapter(clock=clock)
    config = ProbeConfig(
        warmup_seconds=5.0,
        consecutive_healthy_threshold=5,
        timeout_seconds=20.0,
        min_probe_samples=10,
    )
    sampler = ProbeSampler(observability=adapter, clock=clock, config=config)

    now = clock.now()
    twins = [
        TwinHandle(
            twin_id="twin_0",
            incident_id="inc_test",
            candidate_index=0,
            namespace="ust-twin-0",
            database="db0",
            forked_from_snapshot_at=now,
            state="observing",
        ),
        TwinHandle(
            twin_id="twin_1",
            incident_id="inc_test",
            candidate_index=1,
            namespace="ust-twin-1",
            database="db1",
            forked_from_snapshot_at=now,
            state="observing",
        ),
    ]
    params = ActionParams(workload="edge-gateway")
    plans = [
        RemediationPlan(
            plan_id="plan_0",
            candidate_index=0,
            action=ActionType.ROLLBACK_DEPLOY,
            params=params,
            origin="planner",
            rationale="Rollback plan 0",
        ),
        RemediationPlan(
            plan_id="plan_1",
            candidate_index=1,
            action=ActionType.RESTART_WORKLOAD,
            params=params,
            origin="planner",
            rationale="Restart plan 1",
        ),
        # Plan without matching twin
        RemediationPlan(
            plan_id="plan_missing",
            candidate_index=99,
            action=ActionType.NO_ACTION,
            params=params,
            origin="planner",
            rationale="No twin",
        ),
    ]

    results = await sampler.probe_twins(twins=twins, plans=plans, min_samples=10)
    assert len(results) == 2
    assert "plan_0" in results
    assert "plan_1" in results
    assert results["plan_0"].namespace == "ust-twin-0"
    assert results["plan_1"].namespace == "ust-twin-1"


@pytest.mark.asyncio
async def test_fake_environment_probe_conformance() -> None:
    """FakeEnvironmentProbe implements EnvironmentProbe protocol and behaves deterministically."""
    clock = FrozenClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC))
    fake = FakeEnvironmentProbe(recovered=True, recovery_seconds=14.5, clock=clock)

    assert isinstance(fake, EnvironmentProbe)
    sample = await fake.sample_once(namespace="ust-twin-0")
    assert sample.healthy is True
    assert sample.p99_latency_ms == 95.0
    assert sample.error_rate == 0.0

    result = await fake.probe_environment(
        namespace="ust-twin-0",
        applied_at=clock.now(),
    )
    assert result.recovered is True
    assert result.recovery_seconds == 14.5
    assert result.timeout_exceeded is False

    # Unrecovered fake
    fake_unrec = FakeEnvironmentProbe(recovered=False, clock=clock)
    res_unrec = await fake_unrec.probe_environment(
        namespace="ust-twin-0",
        applied_at=clock.now(),
    )
    assert res_unrec.recovered is False
    assert res_unrec.recovery_seconds is None
    assert res_unrec.timeout_exceeded is True


@pytest.mark.parametrize(
    ("early_start", "late_start"),
    [
        (20, 25),
        (20, 40),
        (25, 50),
        (30, 45),
        (35, 60),
    ],
)
def test_detect_recovery_monotonicity_property(early_start: int, late_start: int) -> None:
    """Monotonicity property: an earlier healthy streak strictly yields a smaller recovery time."""
    applied_at = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

    def make_probes(streak_start_sec: int) -> list[ProbeSample]:
        probes = []
        for i in range(80):
            is_healthy = streak_start_sec <= i < (streak_start_sec + 15)
            probes.append(
                ProbeSample(
                    at=applied_at + timedelta(seconds=i),
                    healthy=is_healthy,
                    p99_latency_ms=100.0 if is_healthy else 600.0,
                    error_rate=0.0 if is_healthy else 0.05,
                )
            )
        return probes

    rec_early, sec_early = detect_recovery(
        make_probes(early_start),
        applied_at,
        warmup_seconds=20.0,
        consecutive_healthy_threshold=15,
    )
    rec_late, sec_late = detect_recovery(
        make_probes(late_start),
        applied_at,
        warmup_seconds=20.0,
        consecutive_healthy_threshold=15,
    )

    assert rec_early is True
    assert rec_late is True
    assert sec_early is not None
    assert sec_late is not None
    assert sec_early < sec_late
