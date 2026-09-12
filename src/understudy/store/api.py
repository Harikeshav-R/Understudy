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
        """List runs with no finished_at timestamp yet.

        This is the documented fact source for kernel invariant K5 (single writer):
        `in_flight_plan_targets` is read from this set under a transaction that also
        inserts the new claim, so the check and the claim are atomic (docs/03-invariants.md).
        """
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


@runtime_checkable
class CheckpointStore(Protocol):
    """Storage interface for LangGraph state checkpoints, blobs, and pending writes."""

    async def put_checkpoint(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        parent_checkpoint_id: str | None,
        checkpoint: tuple[str, bytes],
        metadata: tuple[str, bytes],
    ) -> None:
        """Store serialized checkpoint data and metadata."""
        raise NotImplementedError

    async def get_checkpoint(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None = None,
    ) -> tuple[str, tuple[str, bytes], tuple[str, bytes], str | None] | None:
        """Retrieve checkpoint by id, or latest checkpoint if checkpoint_id is None.

        Returns (checkpoint_id, checkpoint, metadata, parent_checkpoint_id) or None.
        """
        raise NotImplementedError

    async def list_checkpoints(
        self,
        thread_id: str | None = None,
        checkpoint_ns: str | None = None,
        before_checkpoint_id: str | None = None,
        limit: int | None = None,
    ) -> list[tuple[str, str, str, tuple[str, bytes], tuple[str, bytes], str | None]]:
        """List checkpoints matching filters.

        Returns list of (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata, parent_id).
        """
        raise NotImplementedError

    async def put_blobs(
        self,
        blobs: list[tuple[str, str, str, str | int | float, tuple[str, bytes]]],
    ) -> None:
        """Store channel version blobs as (thread_id, checkpoint_ns, channel, version, blob)."""
        raise NotImplementedError

    async def get_blobs(
        self,
        thread_id: str,
        checkpoint_ns: str,
        channel_versions: dict[str, str | int | float],
    ) -> dict[str, tuple[str, bytes]]:
        """Retrieve channel blobs for the given channel versions."""
        raise NotImplementedError

    async def put_writes(
        self,
        writes: list[tuple[str, str, str, str, int, str, tuple[str, bytes], str]],
    ) -> None:
        """Store task execution writes as (thread_id, ns, cid, task_id, idx, channel, val, path)."""
        raise NotImplementedError

    async def get_writes(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
    ) -> list[tuple[str, str, tuple[str, bytes], str]]:
        """Retrieve pending writes as (task_id, channel, value_tuple, task_path)."""
        raise NotImplementedError

    async def delete_thread(self, thread_id: str) -> None:
        """Delete all checkpoints, blobs, and writes for thread_id."""
        raise NotImplementedError


__all__ = [
    "CheckpointStore",
    "EvalStore",
    "PlaybookStore",
    "RunStore",
]
