"""Store component protocol interfaces."""

from typing import Any, Protocol, runtime_checkable

from understudy.contracts.enums import FailureClass
from understudy.contracts.plan import RemediationPlan
from understudy.contracts.run import RunRecord


@runtime_checkable
class RunStore(Protocol):
    """Append-only store for incident run records and execution history."""

    async def record_run(self, record: RunRecord) -> None:
        """Persist an immutable run record."""
        raise NotImplementedError

    async def get_run(self, run_id: str) -> RunRecord | None:
        """Retrieve a run record by its unique identifier."""
        raise NotImplementedError

    async def list_runs(
        self, incident_id: str | None = None, scenario_id: str | None = None
    ) -> list[RunRecord]:
        """List runs matching optional incident_id or scenario_id filters."""
        raise NotImplementedError

    async def get_active_runs(self) -> list[RunRecord]:
        """Retrieve runs currently in flight."""
        raise NotImplementedError


@runtime_checkable
class PlaybookStore(Protocol):
    """Store for reusable incident playbooks and vector embeddings."""

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
        """Save a new or updated playbook template."""
        raise NotImplementedError

    async def get_playbook(self, playbook_id: str) -> RemediationPlan | None:
        """Retrieve a playbook plan template by identifier."""
        raise NotImplementedError

    async def search_playbooks(
        self, embedding: list[float], limit: int = 5
    ) -> list[RemediationPlan]:
        """Find candidate playbooks nearest to the given embedding."""
        raise NotImplementedError

    async def increment_success(self, playbook_id: str) -> None:
        """Increment the successful resolution counter for a playbook."""
        raise NotImplementedError

    async def increment_failure(self, playbook_id: str) -> None:
        """Increment the failure counter for a playbook."""
        raise NotImplementedError


@runtime_checkable
class EvalStore(Protocol):
    """Store for offline evaluation harness results and comparison metrics."""

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
        """Record evaluation execution results for a scenario repeat."""
        raise NotImplementedError

    async def get_scenario_results(self, scenario_id: str | None = None) -> list[dict[str, Any]]:
        """Retrieve evaluation results across scenarios."""
        raise NotImplementedError
