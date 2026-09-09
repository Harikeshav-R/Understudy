"""Safety kernel component protocol interfaces."""

from typing import Protocol, runtime_checkable

from understudy.contracts.kernel import Fact, KernelVerdict
from understudy.contracts.plan import RemediationPlan


@runtime_checkable
class SafetyKernel(Protocol):
    """Formal safety verification kernel using SMT solvers."""

    async def verify(self, plan: RemediationPlan, facts: list[Fact]) -> KernelVerdict:
        """Evaluate safety invariants for a proposed plan against current system facts."""
        raise NotImplementedError
