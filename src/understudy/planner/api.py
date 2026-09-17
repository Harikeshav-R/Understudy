"""Planner component protocol interfaces."""

from typing import Protocol, runtime_checkable

from understudy.contracts.incident import IncidentContext
from understudy.contracts.plan import RemediationPlan


@runtime_checkable
class Planner(Protocol):
    """Generates and normalizes candidate remediation plans."""

    async def generate_candidates(
        self,
        context: IncidentContext,
        count: int = 3,
        playbook_candidate: RemediationPlan | None = None,
    ) -> list[RemediationPlan]:
        """Generate N candidate remediation plans for the active incident context."""
        raise NotImplementedError


from understudy.planner.validate import LLMPlanner  # noqa: E402

__all__ = [
    "LLMPlanner",
    "Planner",
]
