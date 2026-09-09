"""Deterministic fake evaluation harness implementation."""

from datetime import UTC, datetime
from typing import Any

from understudy.contracts.enums import RunOutcome
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.run import RunRecord
from understudy.eval.api import EvalHarness


class FakeEvalHarness(EvalHarness):
    """Deterministic evaluation harness fake."""

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed

    async def run_scenario(self, scenario_id: str) -> RunRecord:
        """Run a fake evaluation scenario and produce a RunRecord."""
        now = datetime.now(UTC)
        context = IncidentContext(
            incident_id=f"inc_{scenario_id}",
            alert=Alert(
                alert_id=f"alt_{scenario_id}",
                source="synthetic",
                title=f"Evaluation scenario {scenario_id}",
                service="data-service",
                severity="critical",
                fired_at=now,
            ),
            signatures=[],
            metrics_window=MetricWindow(service="data-service", start_time=now, end_time=now),
            recent_deploys=[],
            dependency_graph=DependencyGraphSnapshot(observed_at=now),
            gathered_at=now,
        )
        return RunRecord(
            run_id=f"run_eval_{scenario_id}",
            incident_id=f"inc_{scenario_id}",
            scenario_id=scenario_id,
            started_at=now,
            finished_at=now,
            outcome=RunOutcome.EXECUTED,
            context=context,
            plans=[],
            evidence=[],
            prod_applied_plan_id="plan_cand_0",
            prod_outcome="resolved",
        )

    async def run_corpus(self) -> dict[str, Any]:
        """Run fake evaluation corpus and produce metric report summary."""
        return {
            "total_scenarios": 12,
            "correlation": 1.0,
            "efficiency": 0.85,
            "verification_coverage": 1.0,
            "escalation_precision": 1.0,
            "escalation_recall": 1.0,
        }
