"""Playbook component protocol interfaces."""

from typing import Protocol, runtime_checkable

from understudy.contracts.incident import IncidentContext
from understudy.contracts.plan import RemediationPlan


@runtime_checkable
class PlaybookLibrary(Protocol):
    """Playbook matching, embedding retrieval, and outcome recording."""

    async def retrieve_candidate(self, incident: IncidentContext) -> RemediationPlan | None:
        """Find and confirm a matching playbook candidate for the active incident."""
        raise NotImplementedError

    async def record_outcome(self, playbook_id: str, success: bool, evidence_run_id: str) -> None:
        """Record the rehearsal or actuation outcome for a matched playbook."""
        raise NotImplementedError
