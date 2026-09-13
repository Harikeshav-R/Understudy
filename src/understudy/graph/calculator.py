"""Blast radius calculator computing downstream impact of remediations across dependencies."""

from typing import Any

from understudy.graph.api import BlastRadiusCalculator, DependencyGraph

# Zero-baseline degradation is a step function (no baseline to take a % of), so we use
# small fixed absolute floors instead of reusing the relative `threshold` parameter.
ZERO_BASELINE_ERROR_RATE_FLOOR = 0.01  # 1 percentage point
ZERO_BASELINE_LATENCY_FLOOR_MS = 1.0  # 1ms


class ServiceBlastRadiusCalculator(BlastRadiusCalculator):
    """Calculates normalized blast radius score based on reachable dependents and traffic shares."""

    def __init__(self, graph: DependencyGraph) -> None:
        self.graph = graph

    def calculate(self, target_service: str, affected_services: set[str]) -> float:
        """Calculate normalized blast radius score in [0.0, 1.0].

        Per architecture §2.6:
        blast = Σ over services s in reachable_set(target, dep_graph) request_share(s) * affected(s)
        where reachable_set is the transitive closure of dependents of the mutated workload.
        """
        # reachable_set raises GraphError if target_service is unknown
        reachable = self.graph.reachable_set(target_service)

        if not affected_services or not reachable:
            return 0.0

        blast = sum(self.graph.request_share(s) for s in reachable if s in affected_services)
        return min(max(float(blast), 0.0), 1.0)

    @staticmethod
    def compute_affected_services(
        pre_apply_baselines: dict[str, Any],
        post_apply_windows: dict[str, Any],
        threshold: float = 0.10,
    ) -> set[str]:
        """Determine which services suffered >10% degradation in error rate or p99 latency."""
        affected: set[str] = set()

        for service, post_window in post_apply_windows.items():
            pre_window = pre_apply_baselines.get(service)
            if pre_window is None:
                continue

            pre_p99 = getattr(pre_window, "p99_latency_ms", 0.0) or 0.0
            post_p99 = getattr(post_window, "p99_latency_ms", 0.0) or 0.0

            pre_err = getattr(pre_window, "error_rate", 0.0) or 0.0
            post_err = getattr(post_window, "error_rate", 0.0) or 0.0

            # 1. Latency degradation check
            latency_degraded = False
            if pre_p99 > 0.0:
                if (post_p99 - pre_p99) / pre_p99 > threshold:
                    latency_degraded = True
            elif post_p99 > ZERO_BASELINE_LATENCY_FLOOR_MS:
                latency_degraded = True

            # 2. Error rate degradation check
            error_degraded = False
            if pre_err > 0.0:
                if (post_err - pre_err) / pre_err > threshold:
                    error_degraded = True
            elif post_err > ZERO_BASELINE_ERROR_RATE_FLOOR:
                error_degraded = True

            if latency_degraded or error_degraded:
                affected.add(service)

        return affected
