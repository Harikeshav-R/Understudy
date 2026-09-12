"""LangGraph state definition and explicit reducers for Understudy orchestrator.

Enforces ADR-005 and build-plan step B1.1:
- State is a Pydantic model holding incident_id, context, plans, twins, evidence,
  tournament, verdict, outcome, errors, plus incident lifecycle metadata.
- All reducers are explicit; no implicit dict merging.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from understudy.common.errors import OrchestratorError
from understudy.contracts.enums import RunOutcome
from understudy.contracts.evidence import CandidateEvidence, TournamentResult
from understudy.contracts.incident import Alert, IncidentContext
from understudy.contracts.kernel import KernelVerdict
from understudy.contracts.plan import RemediationPlan
from understudy.contracts.run import RunRecord
from understudy.contracts.twin import TwinHandle


def reduce_incident_id(current: str, update: str | None) -> str:
    """Ensure incident_id is write-once per control loop, preventing accidental switches."""
    if not update:
        return current
    if current and update != current:
        raise OrchestratorError(f"Cannot overwrite incident_id '{current}' with '{update}'")
    return update


def reduce_alert(current: Alert | None, update: Alert | dict[str, Any] | None) -> Alert | None:
    """Explicitly validate or replace the incoming Alert model."""
    if update is None:
        return current
    if isinstance(update, dict):
        return Alert.model_validate(update)
    return update


def reduce_context(
    current: IncidentContext | None, update: IncidentContext | dict[str, Any] | None
) -> IncidentContext | None:
    """Replace context with explicitly validated IncidentContext; no implicit dict merge."""
    if update is None:
        return current
    if isinstance(update, dict):
        return IncidentContext.model_validate(update)
    return update


def reduce_plans(
    current: list[RemediationPlan],
    update: Sequence[RemediationPlan | dict[str, Any]] | RemediationPlan | dict[str, Any] | None,
) -> list[RemediationPlan]:
    """Reconcile plans by plan_id preserving order, updating existing or appending new ones."""
    if update is None:
        return current
    updates: Sequence[RemediationPlan | dict[str, Any]] = (
        [update] if isinstance(update, (RemediationPlan, dict)) else update
    )
    validated = [
        p if isinstance(p, RemediationPlan) else RemediationPlan.model_validate(p) for p in updates
    ]
    if not current:
        return validated

    updated_by_id = {p.plan_id: p for p in validated}
    result = [updated_by_id.pop(p.plan_id, p) for p in current]
    result.extend(updated_by_id.values())
    return result


def reduce_twins(
    current: list[TwinHandle],
    update: Sequence[TwinHandle | dict[str, Any]] | TwinHandle | dict[str, Any] | None,
) -> list[TwinHandle]:
    """Reconcile twin handles by twin_id, preserving order while tracking state progression."""
    if update is None:
        return current
    updates: Sequence[TwinHandle | dict[str, Any]] = (
        [update] if isinstance(update, (TwinHandle, dict)) else update
    )
    validated = [t if isinstance(t, TwinHandle) else TwinHandle.model_validate(t) for t in updates]
    if not current:
        return validated

    updated_by_id = {t.twin_id: t for t in validated}
    result = [updated_by_id.pop(t.twin_id, t) for t in current]
    result.extend(updated_by_id.values())
    return result


def reduce_evidence(
    current: list[CandidateEvidence],
    update: Sequence[CandidateEvidence | dict[str, Any]]
    | CandidateEvidence
    | dict[str, Any]
    | None,
) -> list[CandidateEvidence]:
    """Reconcile candidate evidence by plan_id, updating existing records or appending new ones."""
    if update is None:
        return current
    updates: Sequence[CandidateEvidence | dict[str, Any]] = (
        [update] if isinstance(update, (CandidateEvidence, dict)) else update
    )
    validated = [
        e if isinstance(e, CandidateEvidence) else CandidateEvidence.model_validate(e)
        for e in updates
    ]
    if not current:
        return validated

    updated_by_id = {e.plan_id: e for e in validated}
    result = [updated_by_id.pop(e.plan_id, e) for e in current]
    result.extend(updated_by_id.values())
    return result


def reduce_tournament(
    current: TournamentResult | None, update: TournamentResult | dict[str, Any] | None
) -> TournamentResult | None:
    """Replace tournament result with explicitly validated TournamentResult."""
    if update is None:
        return current
    if isinstance(update, dict):
        return TournamentResult.model_validate(update)
    return update


def reduce_verdict(
    current: KernelVerdict | None, update: KernelVerdict | dict[str, Any] | None
) -> KernelVerdict | None:
    """Replace safety kernel verdict with explicitly validated KernelVerdict."""
    if update is None:
        return current
    if isinstance(update, dict):
        return KernelVerdict.model_validate(update)
    return update


def reduce_outcome(
    current: RunOutcome | None, update: RunOutcome | str | None
) -> RunOutcome | None:
    """Update execution outcome, strictly coercing strings to RunOutcome enum."""
    if update is None:
        return current
    return RunOutcome(update)


def reduce_errors(current: list[str], update: Sequence[str] | str | None) -> list[str]:
    """Append errors into the state history to prevent silent context loss."""
    if update is None:
        return current
    if isinstance(update, str):
        return [*current, update]
    return [*current, *update]


def reduce_started_at(current: datetime | None, update: datetime | None) -> datetime | None:
    """Preserve the earliest non-None start timestamp."""
    if current is not None:
        return current
    return update


def reduce_finished_at(current: datetime | None, update: datetime | None) -> datetime | None:
    """Update finish timestamp when provided."""
    return update if update is not None else current


def reduce_optional_str(current: str | None, update: str | None) -> str | None:
    """Update optional string fields such as plan IDs, reasons, or scenario names."""
    return update if update is not None else current


def reduce_prod_outcome(
    current: Literal["resolved", "not_resolved", "worsened"] | None,
    update: Literal["resolved", "not_resolved", "worsened"] | None,
) -> Literal["resolved", "not_resolved", "worsened"] | None:
    """Update production actuation outcome when verified."""
    return update if update is not None else current


class State(BaseModel):
    """LangGraph execution state for Understudy incident control loop.

    Holds all domain contracts generated across the 13-node control loop (§2.2).
    Every field specifies an explicit reducer preventing implicit dict merging.
    """

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    # Core required fields per build-plan step B1.1
    incident_id: Annotated[str, reduce_incident_id] = ""
    context: Annotated[IncidentContext | None, reduce_context] = None
    plans: Annotated[list[RemediationPlan], reduce_plans] = Field(default_factory=list)
    twins: Annotated[list[TwinHandle], reduce_twins] = Field(default_factory=list)
    evidence: Annotated[list[CandidateEvidence], reduce_evidence] = Field(default_factory=list)
    tournament: Annotated[TournamentResult | None, reduce_tournament] = None
    verdict: Annotated[KernelVerdict | None, reduce_verdict] = None
    outcome: Annotated[RunOutcome | None, reduce_outcome] = None
    errors: Annotated[list[str], reduce_errors] = Field(default_factory=list)

    # Incident lifecycle and actuation tracking
    alert: Annotated[Alert | None, reduce_alert] = None
    started_at: Annotated[datetime | None, reduce_started_at] = None
    finished_at: Annotated[datetime | None, reduce_finished_at] = None
    prod_applied_plan_id: Annotated[str | None, reduce_optional_str] = None
    prod_outcome: Annotated[
        Literal["resolved", "not_resolved", "worsened"] | None, reduce_prod_outcome
    ] = None
    escalation_reason: Annotated[str | None, reduce_optional_str] = None
    scenario_id: Annotated[str | None, reduce_optional_str] = None

    def to_run_record(self, run_id: str | None = None) -> RunRecord:
        """Convert current state to immutable RunRecord contract.

        Raises OrchestratorError if context is absent, ensuring incomplete runs
        cannot forge unanchored records.
        """
        if self.context is None:
            raise OrchestratorError("Cannot convert State to RunRecord without context")
        now = datetime.now(UTC)
        return RunRecord(
            run_id=run_id or f"run_{self.incident_id}",
            incident_id=self.incident_id,
            scenario_id=self.scenario_id,
            started_at=self.started_at or now,
            finished_at=self.finished_at or now,
            outcome=self.outcome or (RunOutcome.FAILED if self.errors else RunOutcome.EXECUTED),
            context=self.context,
            plans=self.plans,
            evidence=self.evidence,
            tournament=self.tournament,
            verdict=self.verdict,
            prod_applied_plan_id=self.prod_applied_plan_id,
            prod_outcome=self.prod_outcome,
            escalation_reason=self.escalation_reason,
        )
