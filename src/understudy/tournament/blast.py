"""Blast radius computation and pre-apply environment baselines for candidate rehearsals.

Implements build-plan step A4.2 and architecture §2.6:
blast = Σ over services s in reachable_set(target, dep_graph)  request_share(s) * affected(s)
where:
- reachable_set is the transitive closure of dependents of the mutated workload.
- request_share(s) is s's share of total requests in the pre-incident window.
- affected(s) is 1 if s's error rate or p99 degraded by more than 10% relative to its
  pre-apply baseline, else 0.
- observed_blast_set is {s : affected(s) = 1}.
- downstream_error_delta is post-apply error rate minus pre-apply, downstream only.
"""

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta

import httpx
from pydantic import BaseModel, ConfigDict, Field

from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import get_settings
from understudy.common.errors import ObservabilityError, UnderstudyError
from understudy.common.logging import get_logger
from understudy.contracts.enums import ActionType
from understudy.contracts.incident import MetricWindow
from understudy.contracts.plan import RemediationPlan
from understudy.contracts.twin import TwinHandle
from understudy.graph.api import DependencyGraph
from understudy.signals.api import ObservabilityAdapter


class BlastConfig(BaseModel):
    """Configuration tunables for remediation blast radius and baseline evaluation."""

    model_config = ConfigDict(frozen=True)

    degradation_threshold: float = 0.10
    baseline_window_seconds: float = 30.0
    zero_baseline_latency_floor_ms: float = 1.0
    zero_baseline_error_rate_floor: float = 0.01
    downstream_error_ceiling: float = 0.10

    @classmethod
    def from_settings(cls) -> "BlastConfig":
        """Construct BlastConfig initialized from system settings."""
        settings = get_settings()
        return cls(
            degradation_threshold=0.10,
            downstream_error_ceiling=settings.scoring.downstream_error_ceiling,
        )


class EnvironmentBaseline(BaseModel):
    """Pre-apply telemetry baseline measurements captured for an environment."""

    model_config = ConfigDict(frozen=True)

    namespace: str
    captured_at: datetime
    window_start: datetime
    window_end: datetime
    baselines: dict[str, MetricWindow] = Field(default_factory=dict)


class BlastEvaluation(BaseModel):
    """Result of blast radius computation and downstream impact evaluation."""

    model_config = ConfigDict(frozen=True)

    target_service: str
    reachable_set: list[str] = Field(default_factory=list)
    affected_services: list[str] = Field(default_factory=list)
    observed_blast_set: list[str] = Field(default_factory=list)
    blast_radius: float
    downstream_error_delta: float
    pre_apply_baselines: dict[str, MetricWindow] = Field(default_factory=dict)
    post_apply_windows: dict[str, MetricWindow] = Field(default_factory=dict)


def compute_affected_services(
    pre_apply_baselines: dict[str, MetricWindow],
    post_apply_windows: dict[str, MetricWindow],
    threshold: float = 0.10,
    zero_baseline_latency_floor_ms: float = 1.0,
    zero_baseline_error_rate_floor: float = 0.01,
) -> set[str]:
    """Determine which services suffered >10% degradation in error rate or p99 latency.

    Per architecture §2.6:
    affected(s) is 1 if s's error rate or p99 degraded by more than 10% relative to its
    pre-apply baseline, else 0.
    """
    affected: set[str] = set()

    for service, post_window in post_apply_windows.items():
        pre_window = pre_apply_baselines.get(service)
        if pre_window is None:
            continue

        pre_p99 = pre_window.p99_latency_ms or 0.0
        post_p99 = post_window.p99_latency_ms or 0.0

        pre_err = pre_window.error_rate or 0.0
        post_err = post_window.error_rate or 0.0

        # Latency degradation check
        latency_degraded = False
        if pre_p99 > 0.0:
            if (post_p99 - pre_p99) / pre_p99 > threshold:
                latency_degraded = True
        elif post_p99 > zero_baseline_latency_floor_ms:
            latency_degraded = True

        # Error rate degradation check
        error_degraded = False
        if pre_err > 0.0:
            if (post_err - pre_err) / pre_err > threshold:
                error_degraded = True
        elif post_err > zero_baseline_error_rate_floor:
            error_degraded = True

        if latency_degraded or error_degraded:
            affected.add(service)

    return affected


def compute_blast_radius(
    target_service: str,
    affected_services: set[str],
    graph: DependencyGraph,
) -> float:
    """Calculate normalized blast radius score in [0.0, 1.0].

    Per architecture §2.6:
    blast = Σ over services s in reachable_set(target, dep_graph) request_share(s) * affected(s)
    where reachable_set is the transitive closure of dependents of the mutated workload.
    """
    if not target_service or target_service == "none":
        return 0.0

    reachable = graph.reachable_set(target_service)
    if not affected_services or not reachable:
        return 0.0

    blast = sum(graph.request_share(s) for s in reachable if s in affected_services)
    return min(max(float(blast), 0.0), 1.0)


def compute_downstream_error_delta(
    target_service: str,
    pre_apply_baselines: dict[str, MetricWindow],
    post_apply_windows: dict[str, MetricWindow],
    graph: DependencyGraph,
) -> float:
    """Calculate post-apply error rate minus pre-apply baseline, downstream only.

    Downstream services are the reachable dependents: reachable_set(target, dep_graph).
    The delta is weighted by request share across reachable services per architecture §2.6.
    """
    if not target_service or target_service == "none":
        return 0.0

    reachable = graph.reachable_set(target_service)
    if not reachable:
        return 0.0

    deltas: dict[str, float] = {}
    for s in reachable:
        post_window = post_apply_windows.get(s)
        pre_window = pre_apply_baselines.get(s)
        post_err = (post_window.error_rate if post_window else None) or 0.0
        pre_err = (pre_window.error_rate if pre_window else None) or 0.0
        deltas[s] = post_err - pre_err

    # Weighted by request share across reachable services
    weights = {s: graph.request_share(s) for s in reachable}
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return 0.0

    weighted_delta = sum(weights[s] * deltas[s] for s in reachable) / total_weight
    return round(float(weighted_delta), 4)


def evaluate_blast(
    plan: RemediationPlan | str,
    pre_apply_baselines: dict[str, MetricWindow],
    post_apply_windows: dict[str, MetricWindow],
    graph: DependencyGraph,
    config: BlastConfig | None = None,
) -> BlastEvaluation:
    """Evaluate blast radius and downstream degradation for a candidate remediation plan."""
    cfg = config or BlastConfig()

    if isinstance(plan, RemediationPlan):
        if plan.action == ActionType.NO_ACTION or not plan.target_resources:
            target_service = plan.params.workload
            return BlastEvaluation(
                target_service=target_service,
                reachable_set=[],
                affected_services=[],
                observed_blast_set=[],
                blast_radius=0.0,
                downstream_error_delta=0.0,
                pre_apply_baselines=pre_apply_baselines,
                post_apply_windows=post_apply_windows,
            )
        target_service = plan.params.workload
    else:
        target_service = str(plan)

    affected = compute_affected_services(
        pre_apply_baselines=pre_apply_baselines,
        post_apply_windows=post_apply_windows,
        threshold=cfg.degradation_threshold,
        zero_baseline_latency_floor_ms=cfg.zero_baseline_latency_floor_ms,
        zero_baseline_error_rate_floor=cfg.zero_baseline_error_rate_floor,
    )

    reachable = sorted(graph.reachable_set(target_service))
    blast = compute_blast_radius(target_service, affected, graph)
    downstream_delta = compute_downstream_error_delta(
        target_service=target_service,
        pre_apply_baselines=pre_apply_baselines,
        post_apply_windows=post_apply_windows,
        graph=graph,
    )

    observed_blast_set = sorted(affected)

    return BlastEvaluation(
        target_service=target_service,
        reachable_set=reachable,
        affected_services=sorted(affected),
        observed_blast_set=observed_blast_set,
        blast_radius=blast,
        downstream_error_delta=downstream_delta,
        pre_apply_baselines=pre_apply_baselines,
        post_apply_windows=post_apply_windows,
    )


class BlastCoordinator:
    """Async coordinator for pre-apply baseline capture and blast radius evaluation."""

    def __init__(
        self,
        observability: ObservabilityAdapter,
        dependency_graph: DependencyGraph,
        clock: Clock | None = None,
        config: BlastConfig | None = None,
    ) -> None:
        self.observability = observability
        self.dependency_graph = dependency_graph
        self.clock: Clock = resolve_clock(clock)
        self.config: BlastConfig = config or BlastConfig()
        self.logger = get_logger(component="blast_coordinator")

    def _get_declared_services(self) -> list[str]:
        """Resolve declared services from dependency graph snapshot."""
        snapshot = self.dependency_graph.snapshot()
        if not snapshot.nodes:
            raise ValueError(
                "Cannot resolve declared services: dependency graph snapshot contains no nodes"
            )
        return sorted(snapshot.nodes)

    async def capture_baseline(
        self,
        namespace: str,
        services: Sequence[str] | None = None,
        at: datetime | None = None,
        lookback_seconds: float | None = None,
    ) -> EnvironmentBaseline:
        """Capture pre-apply telemetry metrics baseline for an environment."""
        now = at or self.clock.now()
        lookback = lookback_seconds or self.config.baseline_window_seconds
        since = now - timedelta(seconds=lookback)

        target_services = list(services) if services is not None else self._get_declared_services()

        async def _fetch_one(svc: str) -> tuple[str, MetricWindow]:
            try:
                window = await self.observability.metric_window(
                    service=svc,
                    since=since,
                    namespace=namespace,
                    until=now,
                )
                return svc, window
            except (ObservabilityError, httpx.HTTPError, UnderstudyError) as exc:
                self.logger.warning(
                    "baseline_metric_fetch_failed",
                    namespace=namespace,
                    service=svc,
                    error=str(exc),
                )
                fallback = MetricWindow(
                    service=svc,
                    start_time=since,
                    end_time=now,
                    series=[],
                    p99_latency_ms=0.0,
                    error_rate=0.0,
                    request_count=0,
                )
                return svc, fallback

        results = await asyncio.gather(*[_fetch_one(s) for s in target_services])
        baselines = dict(results)

        return EnvironmentBaseline(
            namespace=namespace,
            captured_at=now,
            window_start=since,
            window_end=now,
            baselines=baselines,
        )

    async def capture_baselines(
        self,
        twins: Sequence[TwinHandle],
        services: Sequence[str] | None = None,
        at: datetime | None = None,
        lookback_seconds: float | None = None,
    ) -> dict[str, EnvironmentBaseline]:
        """Capture pre-apply baselines concurrently across twin environments."""
        if not twins:
            return {}

        results = await asyncio.gather(
            *[
                self.capture_baseline(
                    namespace=twin.namespace,
                    services=services,
                    at=at,
                    lookback_seconds=lookback_seconds,
                )
                for twin in twins
            ]
        )
        return {twin.twin_id: res for twin, res in zip(twins, results, strict=True)}

    async def capture_post_apply(
        self,
        namespace: str,
        applied_at: datetime,
        until: datetime | None = None,
        services: Sequence[str] | None = None,
    ) -> dict[str, MetricWindow]:
        """Capture post-apply telemetry metric windows for an environment."""
        end_time = until or self.clock.now()
        target_services = list(services) if services is not None else self._get_declared_services()

        async def _fetch_one(svc: str) -> tuple[str, MetricWindow]:
            try:
                window = await self.observability.metric_window(
                    service=svc,
                    since=applied_at,
                    namespace=namespace,
                    until=end_time,
                )
                return svc, window
            except (ObservabilityError, httpx.HTTPError, UnderstudyError) as exc:
                self.logger.warning(
                    "post_apply_metric_fetch_failed",
                    namespace=namespace,
                    service=svc,
                    error=str(exc),
                )
                fallback = MetricWindow(
                    service=svc,
                    start_time=applied_at,
                    end_time=end_time,
                    series=[],
                    p99_latency_ms=0.0,
                    error_rate=0.0,
                    request_count=0,
                )
                return svc, fallback

        results = await asyncio.gather(*[_fetch_one(s) for s in target_services])
        return dict(results)

    async def evaluate_environment(
        self,
        plan: RemediationPlan,
        namespace: str,
        baseline: EnvironmentBaseline,
        applied_at: datetime,
        until: datetime | None = None,
        services: Sequence[str] | None = None,
    ) -> BlastEvaluation:
        """Capture post-apply telemetry and evaluate blast radius against baseline."""
        post_windows = await self.capture_post_apply(
            namespace=namespace,
            applied_at=applied_at,
            until=until,
            services=services,
        )
        return evaluate_blast(
            plan=plan,
            pre_apply_baselines=baseline.baselines,
            post_apply_windows=post_windows,
            graph=self.dependency_graph,
            config=self.config,
        )

    async def evaluate_twins(
        self,
        plans: Sequence[RemediationPlan],
        twins: Sequence[TwinHandle],
        baselines: dict[str, EnvironmentBaseline],
        applied_at: datetime,
        until: datetime | None = None,
    ) -> dict[str, BlastEvaluation]:
        """Evaluate blast radius concurrently across plans and their dedicated twins."""
        tasks = []
        plan_ids = []

        twins_by_index = {t.candidate_index: t for t in twins}
        for plan in plans:
            twin = twins_by_index.get(plan.candidate_index)
            if twin is None:
                continue
            baseline = baselines.get(twin.twin_id)
            if baseline is None:
                baseline = baselines.get(twin.namespace)
            if baseline is None:
                continue

            plan_ids.append(plan.plan_id)
            tasks.append(
                self.evaluate_environment(
                    plan=plan,
                    namespace=twin.namespace,
                    baseline=baseline,
                    applied_at=applied_at,
                    until=until,
                )
            )

        if not tasks:
            return {}

        results = await asyncio.gather(*tasks)
        return dict(zip(plan_ids, results, strict=True))
