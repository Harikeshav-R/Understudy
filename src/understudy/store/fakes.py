"""Deterministic in-memory fake store implementations."""

from typing import Any, TypedDict

from understudy.contracts.enums import FailureClass
from understudy.contracts.plan import RemediationPlan
from understudy.contracts.run import RunRecord
from understudy.store.api import EvalStore, PlaybookStore, RunStore


class _PlaybookRecord(TypedDict):
    playbook_id: str
    failure_class: FailureClass
    signature_text: str
    embedding: list[float]
    plan: RemediationPlan
    evidence_refs: list[str]
    origin: str
    successes: int
    failures: int


class FakeRunStore(RunStore):
    """In-memory append-only fake run store."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}

    async def record_run(self, record: RunRecord) -> None:
        """Persist a run record append-only."""
        self._runs[record.run_id] = record

    async def get_run(self, run_id: str) -> RunRecord | None:
        """Retrieve run by ID."""
        return self._runs.get(run_id)

    async def list_runs(
        self, incident_id: str | None = None, scenario_id: str | None = None
    ) -> list[RunRecord]:
        """List runs matching filters."""
        results: list[RunRecord] = []
        for run in self._runs.values():
            if incident_id is not None and run.incident_id != incident_id:
                continue
            if scenario_id is not None and run.scenario_id != scenario_id:
                continue
            results.append(run)
        return results

    async def get_active_runs(self) -> list[RunRecord]:
        """List runs with no finished_at timestamp yet."""
        return [run for run in self._runs.values() if run.finished_at is None]


class FakePlaybookStore(PlaybookStore):
    """In-memory fake playbook store."""

    def __init__(self) -> None:
        self._playbooks: dict[str, _PlaybookRecord] = {}

    async def save_playbook(
        self,
        playbook_id: str,
        failure_class: FailureClass,
        signature_text: str,
        embedding: list[float],
        plan: RemediationPlan,
        evidence_refs: list[str],
        origin: str,
    ) -> None:
        """Save a playbook."""
        self._playbooks[playbook_id] = {
            "playbook_id": playbook_id,
            "failure_class": failure_class,
            "signature_text": signature_text,
            "embedding": embedding,
            "plan": plan,
            "evidence_refs": list(evidence_refs),
            "origin": origin,
            "successes": 0,
            "failures": 0,
        }

    async def get_playbook(self, playbook_id: str) -> RemediationPlan | None:
        """Retrieve a playbook plan template."""
        record = self._playbooks.get(playbook_id)
        if record is None:
            return None
        return record["plan"]

    async def search_playbooks(
        self, embedding: list[float], limit: int = 5
    ) -> list[RemediationPlan]:
        """Search playbooks (returns all stored plans up to limit)."""
        _ = embedding
        return [item["plan"] for item in list(self._playbooks.values())[:limit]]

    async def increment_success(self, playbook_id: str) -> None:
        """Increment success counter."""
        if playbook_id in self._playbooks:
            self._playbooks[playbook_id]["successes"] += 1

    async def increment_failure(self, playbook_id: str) -> None:
        """Increment failure counter."""
        if playbook_id in self._playbooks:
            self._playbooks[playbook_id]["failures"] += 1


class FakeEvalStore(EvalStore):
    """In-memory fake evaluation store."""

    def __init__(self) -> None:
        self._results: list[dict[str, Any]] = []

    async def record_scenario_result(
        self,
        scenario_id: str,
        run_id: str,
        repeat_index: int,
        twin_predicted_success: bool | None,
        prod_actual_success: bool | None,
        expected_escalation: bool,
        did_escalate: bool,
        runner_up_plan_id: str | None = None,
        runner_up_prod_success: bool | None = None,
    ) -> None:
        """Record evaluation result."""
        self._results.append(
            {
                "scenario_id": scenario_id,
                "run_id": run_id,
                "repeat_index": repeat_index,
                "twin_predicted_success": twin_predicted_success,
                "prod_actual_success": prod_actual_success,
                "expected_escalation": expected_escalation,
                "did_escalate": did_escalate,
                "runner_up_plan_id": runner_up_plan_id,
                "runner_up_prod_success": runner_up_prod_success,
            }
        )

    async def get_scenario_results(self, scenario_id: str | None = None) -> list[dict[str, Any]]:
        """Retrieve evaluation results."""
        if scenario_id is None:
            return list(self._results)
        return [r for r in self._results if r["scenario_id"] == scenario_id]
