"""SLO probe sampler and recovery detection for candidate rehearsals."""

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta

import httpx
from pydantic import BaseModel, ConfigDict, Field

from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import get_settings
from understudy.common.errors import ObservabilityError, UnderstudyError
from understudy.common.logging import get_logger
from understudy.contracts.evidence import ProbeSample
from understudy.contracts.plan import RemediationPlan
from understudy.contracts.twin import TwinHandle
from understudy.signals.api import ObservabilityAdapter


class ProbeConfig(BaseModel):
    """Configuration tunables for environment SLO probing."""

    model_config = ConfigDict(frozen=True)

    service: str = "edge-gateway"
    poll_interval_seconds: float = 1.0
    warmup_seconds: float = 20.0
    consecutive_healthy_threshold: int = 15
    timeout_seconds: float = 180.0
    slo_p99_latency_ms: float = 400.0
    slo_error_rate: float = 0.02
    min_probe_samples: int = 60
    metric_lookback_seconds: float = 15.0

    @classmethod
    def from_settings(cls) -> "ProbeConfig":
        """Construct ProbeConfig initialized from global system settings."""
        settings = get_settings()
        return cls(
            warmup_seconds=float(settings.scoring.warm_up_seconds),
            consecutive_healthy_threshold=settings.scoring.recovery_consecutive_seconds,
            timeout_seconds=float(settings.scoring.recovery_timeout_seconds),
            min_probe_samples=settings.scoring.min_probe_samples,
        )


class ProbeResult(BaseModel):
    """Outcome and collected telemetry samples from an environment probe session."""

    model_config = ConfigDict(frozen=True)

    namespace: str
    target_service: str
    probes: list[ProbeSample] = Field(default_factory=list)
    recovered: bool
    recovery_seconds: float | None = None
    timeout_exceeded: bool = False


def detect_recovery(
    probes: Sequence[ProbeSample],
    applied_at: datetime,
    forked_at: datetime | None = None,
    warmup_seconds: float = 20.0,
    consecutive_healthy_threshold: int = 15,
    timeout_seconds: float = 180.0,
) -> tuple[bool, float | None]:
    """Pure function evaluating recovery across a sequence of probe samples.

    Samples taken within `warmup_seconds` after `forked_at` (or `applied_at` if
    `forked_at` is not provided) are excluded from qualifying for the consecutive
    healthy sample count per architecture §2.6 so cold starts are not scored.

    Recovery requires `consecutive_healthy_threshold` consecutive healthy samples
    observed after the warmup window and within `timeout_seconds` of `applied_at`.
    Recovery time is measured from `applied_at` to the start of the consecutive streak.

    Returns:
        tuple[bool, float | None]: (recovered, recovery_seconds).
    """
    if not probes:
        return False, None

    sorted_probes = sorted(probes, key=lambda p: p.at)
    warmup_reference = forked_at if forked_at is not None else applied_at
    warmup_threshold = warmup_reference + timedelta(seconds=warmup_seconds)
    timeout_threshold = applied_at + timedelta(seconds=timeout_seconds)

    consecutive_healthy = 0
    streak_start_time: datetime | None = None

    for probe in sorted_probes:
        # Check timeout boundary: samples strictly past timeout cannot confirm recovery
        if probe.at > timeout_threshold:
            break

        # Samples strictly inside the warm-up window are excluded from recovery streaks
        if probe.at < warmup_threshold:
            consecutive_healthy = 0
            streak_start_time = None
            continue

        if probe.healthy:
            if consecutive_healthy == 0:
                streak_start_time = probe.at
            consecutive_healthy += 1

            if consecutive_healthy >= consecutive_healthy_threshold:
                assert streak_start_time is not None
                rec_sec = max(0.0, (streak_start_time - applied_at).total_seconds())
                return True, round(rec_sec, 3)
        else:
            consecutive_healthy = 0
            streak_start_time = None

    return False, None


class ProbeSampler:
    """1 Hz environment telemetry sampler and recovery detector."""

    def __init__(
        self,
        observability: ObservabilityAdapter,
        clock: Clock | None = None,
        config: ProbeConfig | None = None,
    ) -> None:
        self.observability = observability
        self.clock: Clock = resolve_clock(clock)
        self.config: ProbeConfig = config or ProbeConfig()
        self.logger = get_logger(component="probe_sampler")

    async def sample_once(
        self,
        namespace: str,
        target_service: str | None = None,
        at: datetime | None = None,
    ) -> ProbeSample:
        """Sample a single probe measurement from the environment's telemetry."""
        service = target_service or self.config.service
        now = at or self.clock.now()
        lookback = timedelta(seconds=max(10.0, self.config.metric_lookback_seconds))
        since = now - lookback

        try:
            metric_window = await self.observability.metric_window(
                service=service,
                since=since,
                namespace=namespace,
                until=now,
            )
            is_ready = await self.observability.service_health(
                namespace=namespace,
                service=service,
            )
        except (ObservabilityError, httpx.HTTPError, UnderstudyError) as exc:
            self.logger.warning(
                "probe_sample_failed",
                namespace=namespace,
                service=service,
                error=str(exc),
            )
            return ProbeSample(
                at=now,
                healthy=False,
                p99_latency_ms=0.0,
                error_rate=1.0,
            )

        p99 = metric_window.p99_latency_ms
        raw_error_rate = metric_window.error_rate
        error_rate = raw_error_rate if raw_error_rate is not None else 0.0

        # healthy := p99_latency_ms < 400 AND error_rate < 0.02 AND readyz == 200
        # If p99 is None (e.g. no requests or scrape unavailable), healthy is False
        healthy = (
            p99 is not None
            and p99 < self.config.slo_p99_latency_ms
            and error_rate < self.config.slo_error_rate
            and is_ready
        )

        sample_p99 = p99 if p99 is not None else 0.0
        return ProbeSample(
            at=now,
            healthy=healthy,
            p99_latency_ms=sample_p99,
            error_rate=error_rate,
        )

    async def probe_environment(
        self,
        namespace: str,
        applied_at: datetime,
        forked_at: datetime | None = None,
        target_service: str | None = None,
        min_samples: int | None = None,
        stop_on_recovery: bool = True,
    ) -> ProbeResult:
        """Run 1 Hz probe loop against an environment until recovery or timeout."""
        service = target_service or self.config.service
        required_min_samples = (
            min_samples if min_samples is not None else self.config.min_probe_samples
        )
        probes: list[ProbeSample] = []
        timeout_limit = applied_at + timedelta(seconds=self.config.timeout_seconds)

        recovered = False
        recovery_seconds: float | None = None
        timeout_exceeded = False

        while True:
            sample = await self.sample_once(namespace=namespace, target_service=service)
            probes.append(sample)

            if not recovered:
                rec, rec_sec = detect_recovery(
                    probes=probes,
                    applied_at=applied_at,
                    forked_at=forked_at,
                    warmup_seconds=self.config.warmup_seconds,
                    consecutive_healthy_threshold=self.config.consecutive_healthy_threshold,
                    timeout_seconds=self.config.timeout_seconds,
                )
                if rec:
                    recovered = True
                    recovery_seconds = rec_sec
                    self.logger.info(
                        "probe_recovery_detected",
                        namespace=namespace,
                        recovery_seconds=recovery_seconds,
                        samples_collected=len(probes),
                    )

            current_time = self.clock.now()
            if current_time >= timeout_limit:
                if not recovered:
                    timeout_exceeded = True
                    self.logger.warning(
                        "probe_timeout_exceeded",
                        namespace=namespace,
                        samples_collected=len(probes),
                    )
                break

            if recovered and stop_on_recovery and len(probes) >= required_min_samples:
                break

            await self.clock.sleep(self.config.poll_interval_seconds)

        return ProbeResult(
            namespace=namespace,
            target_service=service,
            probes=probes,
            recovered=recovered,
            recovery_seconds=recovery_seconds,
            timeout_exceeded=timeout_exceeded,
        )

    async def probe_twins(
        self,
        twins: Sequence[TwinHandle],
        plans: Sequence[RemediationPlan],
        target_service: str | None = None,
        min_samples: int | None = None,
        stop_on_recovery: bool = True,
    ) -> dict[str, ProbeResult]:
        """Probe multiple twins concurrently across candidate rehearsals."""
        applied_at = self.clock.now()
        tasks = []

        for plan in plans:
            twin = next((t for t in twins if t.candidate_index == plan.candidate_index), None)
            if twin is None:
                continue
            tasks.append(
                (
                    plan.plan_id,
                    self.probe_environment(
                        namespace=twin.namespace,
                        applied_at=applied_at,
                        forked_at=twin.forked_from_snapshot_at,
                        target_service=target_service,
                        min_samples=min_samples,
                        stop_on_recovery=stop_on_recovery,
                    ),
                )
            )

        results: dict[str, ProbeResult] = {}
        if tasks:
            plan_ids = [p_id for p_id, _ in tasks]
            coroutines = [coro for _, coro in tasks]
            probe_results = await asyncio.gather(*coroutines)
            for plan_id, res in zip(plan_ids, probe_results, strict=True):
                results[plan_id] = res

        return results
