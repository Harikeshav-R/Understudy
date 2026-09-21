import math
from typing import Any, TypedDict

from understudy.common.errors import StoreError
from understudy.contracts.enums import FailureClass
from understudy.contracts.plan import RemediationPlan
from understudy.contracts.run import RunRecord
from understudy.store.api import (
    CheckpointStore,
    EvalStore,
    PlaybookSearchResult,
    PlaybookStore,
    RunStore,
)


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

    async def claim_run(self, record: RunRecord) -> None:
        """Atomically verify no other active run exists and persist `record`."""
        finished_incident_ids = {
            run.incident_id for run in self._runs.values() if run.finished_at is not None
        }
        if any(
            run.finished_at is None
            and not (
                run.run_id.endswith("_pre_actuation") and run.incident_id in finished_incident_ids
            )
            for run in self._runs.values()
        ):
            raise StoreError(
                "Another active run already exists; cannot claim a new run",
                details={"run_id": record.run_id},
            )
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
        """List runs with no finished_at timestamp yet whose incident has not finished."""
        finished_incident_ids = {
            run.incident_id for run in self._runs.values() if run.finished_at is not None
        }
        return [
            run
            for run in self._runs.values()
            if run.finished_at is None
            and not (
                run.run_id.endswith("_pre_actuation") and run.incident_id in finished_incident_ids
            )
        ]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Calculate cosine similarity between two float vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


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
        successes: int = 0,
        failures: int = 0,
    ) -> None:
        """Save a playbook."""
        existing = self._playbooks.get(playbook_id)
        current_successes = (
            existing["successes"] if existing is not None and successes == 0 else successes
        )
        current_failures = (
            existing["failures"] if existing is not None and failures == 0 else failures
        )
        self._playbooks[playbook_id] = {
            "playbook_id": playbook_id,
            "failure_class": failure_class,
            "signature_text": signature_text,
            "embedding": embedding,
            "plan": plan,
            "evidence_refs": list(evidence_refs),
            "origin": origin,
            "successes": current_successes,
            "failures": current_failures,
        }

    async def get_playbook(self, playbook_id: str) -> RemediationPlan | None:
        """Retrieve a playbook plan template."""
        record = self._playbooks.get(playbook_id)
        if record is None:
            return None
        return record["plan"]

    async def get_playbook_by_signature(self, signature_text: str) -> PlaybookSearchResult | None:
        """Retrieve playbook record matching the exact signature text if present."""
        for record in self._playbooks.values():
            if record["signature_text"] == signature_text:
                fc_val = (
                    record["failure_class"].value
                    if hasattr(record["failure_class"], "value")
                    else str(record["failure_class"])
                )
                return PlaybookSearchResult(
                    playbook_id=record["playbook_id"],
                    failure_class=fc_val,
                    signature_text=record["signature_text"],
                    similarity=1.0,
                    plan=record["plan"],
                    evidence_refs=list(record["evidence_refs"]),
                    successes=record["successes"],
                    failures=record["failures"],
                    origin=record["origin"],
                )
        return None

    async def search_playbooks(
        self, embedding: list[float], limit: int = 5
    ) -> list[RemediationPlan]:
        """Search playbooks (returns all stored plans up to limit)."""
        _ = embedding
        return [item["plan"] for item in list(self._playbooks.values())[:limit]]

    async def search_playbooks_with_scores(
        self, embedding: list[float], limit: int = 3
    ) -> list[PlaybookSearchResult]:
        """Find candidate playbooks nearest to the given embedding with similarity scores."""
        scored: list[tuple[float, _PlaybookRecord]] = []
        for record in self._playbooks.values():
            sim = _cosine_similarity(embedding, record["embedding"])
            scored.append((sim, record))

        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[PlaybookSearchResult] = []
        for sim, record in scored[:limit]:
            fc_val = (
                record["failure_class"].value
                if hasattr(record["failure_class"], "value")
                else str(record["failure_class"])
            )
            results.append(
                PlaybookSearchResult(
                    playbook_id=record["playbook_id"],
                    failure_class=fc_val,
                    signature_text=record["signature_text"],
                    similarity=sim,
                    plan=record["plan"],
                    evidence_refs=list(record["evidence_refs"]),
                    successes=record["successes"],
                    failures=record["failures"],
                    origin=record["origin"],
                )
            )
        return results

    async def list_playbooks(self) -> list[PlaybookSearchResult]:
        """List all stored playbooks with operational metadata."""
        results: list[PlaybookSearchResult] = []
        for record in self._playbooks.values():
            fc_val = (
                record["failure_class"].value
                if hasattr(record["failure_class"], "value")
                else str(record["failure_class"])
            )
            results.append(
                PlaybookSearchResult(
                    playbook_id=record["playbook_id"],
                    failure_class=fc_val,
                    signature_text=record["signature_text"],
                    similarity=1.0,
                    plan=record["plan"],
                    evidence_refs=list(record["evidence_refs"]),
                    successes=record["successes"],
                    failures=record["failures"],
                    origin=record["origin"],
                )
            )
        return results

    async def increment_success(self, playbook_id: str, evidence_run_id: str | None = None) -> None:
        """Increment success counter and optionally append evidence run ID."""
        if playbook_id in self._playbooks:
            self._playbooks[playbook_id]["successes"] += 1
            if (
                evidence_run_id is not None
                and evidence_run_id not in self._playbooks[playbook_id]["evidence_refs"]
            ):
                self._playbooks[playbook_id]["evidence_refs"].append(evidence_run_id)

    async def increment_failure(self, playbook_id: str, evidence_run_id: str | None = None) -> None:
        """Increment failure counter and optionally append evidence run ID."""
        if playbook_id in self._playbooks:
            self._playbooks[playbook_id]["failures"] += 1
            if (
                evidence_run_id is not None
                and evidence_run_id not in self._playbooks[playbook_id]["evidence_refs"]
            ):
                self._playbooks[playbook_id]["evidence_refs"].append(evidence_run_id)


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


class FakeCheckpointStore(CheckpointStore):
    """In-memory deterministic fake checkpoint store for LangGraph executions."""

    def __init__(self) -> None:
        self.checkpoints: dict[
            tuple[str, str, str], tuple[tuple[str, bytes], tuple[str, bytes], str | None]
        ] = {}
        self.blobs: dict[tuple[str, str, str, str | int | float], tuple[str, bytes]] = {}
        self.writes: dict[
            tuple[str, str, str],
            dict[tuple[str, int], tuple[str, str, tuple[str, bytes], str]],
        ] = {}

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
        self.checkpoints[(thread_id, checkpoint_ns, checkpoint_id)] = (
            checkpoint,
            metadata,
            parent_checkpoint_id,
        )

    async def get_checkpoint(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None = None,
    ) -> tuple[str, tuple[str, bytes], tuple[str, bytes], str | None] | None:
        """Retrieve checkpoint by id, or latest checkpoint if checkpoint_id is None."""
        if checkpoint_id is not None:
            key = (thread_id, checkpoint_ns, checkpoint_id)
            if key in self.checkpoints:
                cp, md, pid = self.checkpoints[key]
                return (checkpoint_id, cp, md, pid)
            return None

        matching = [
            (cid, cp, md, pid)
            for (tid, ns, cid), (cp, md, pid) in self.checkpoints.items()
            if tid == thread_id and ns == checkpoint_ns
        ]
        if not matching:
            return None
        matching.sort(key=lambda x: x[0])
        return matching[-1]

    async def list_checkpoints(
        self,
        thread_id: str | None = None,
        checkpoint_ns: str | None = None,
        before_checkpoint_id: str | None = None,
        limit: int | None = None,
    ) -> list[tuple[str, str, str, tuple[str, bytes], tuple[str, bytes], str | None]]:
        """List checkpoints matching filters in reverse chronological order."""
        items = [
            (tid, ns, cid, cp, md, pid)
            for (tid, ns, cid), (cp, md, pid) in self.checkpoints.items()
            if (thread_id is None or tid == thread_id)
            and (checkpoint_ns is None or ns == checkpoint_ns)
            and (before_checkpoint_id is None or cid < before_checkpoint_id)
        ]
        items.sort(key=lambda x: x[2], reverse=True)
        if limit is not None:
            items = items[:limit]
        return items

    async def put_blobs(
        self,
        blobs: list[tuple[str, str, str, str | int | float, tuple[str, bytes]]],
    ) -> None:
        """Store channel version blobs."""
        for tid, ns, ch, ver, blob in blobs:
            self.blobs[(tid, ns, ch, ver)] = blob

    async def get_blobs(
        self,
        thread_id: str,
        checkpoint_ns: str,
        channel_versions: dict[str, str | int | float],
    ) -> dict[str, tuple[str, bytes]]:
        """Retrieve channel blobs for the given channel versions."""
        res: dict[str, tuple[str, bytes]] = {}
        for ch, ver in channel_versions.items():
            key = (thread_id, checkpoint_ns, ch, ver)
            if key in self.blobs:
                res[ch] = self.blobs[key]
        return res

    async def put_writes(
        self,
        writes: list[tuple[str, str, str, str, int, str, tuple[str, bytes], str]],
    ) -> None:
        """Store task execution writes."""
        for tid, ns, cid, task_id, idx, ch, val, task_path in writes:
            outer_key = (tid, ns, cid)
            if outer_key not in self.writes:
                self.writes[outer_key] = {}
            self.writes[outer_key][(task_id, idx)] = (task_id, ch, val, task_path)

    async def get_writes(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
    ) -> list[tuple[str, str, tuple[str, bytes], str]]:
        """Retrieve pending writes for a checkpoint."""
        outer_key = (thread_id, checkpoint_ns, checkpoint_id)
        if outer_key not in self.writes:
            return []
        return list(self.writes[outer_key].values())

    async def delete_thread(self, thread_id: str) -> None:
        """Delete all checkpoints, blobs, and writes for thread_id."""
        for kc in list(self.checkpoints.keys()):
            if kc[0] == thread_id:
                del self.checkpoints[kc]
        for kb in list(self.blobs.keys()):
            if kb[0] == thread_id:
                del self.blobs[kb]
        for kw in list(self.writes.keys()):
            if kw[0] == thread_id:
                del self.writes[kw]


__all__ = [
    "FakeCheckpointStore",
    "FakeEvalStore",
    "FakePlaybookStore",
    "FakeRunStore",
]
