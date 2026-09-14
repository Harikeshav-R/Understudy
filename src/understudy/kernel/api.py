"""Safety kernel component protocol interfaces and fact extraction APIs."""

from typing import Protocol, runtime_checkable

from understudy.contracts.kernel import Fact, KernelVerdict
from understudy.contracts.plan import RemediationPlan
from understudy.kernel.facts import (
    FactExtractor,
    K8sFactSource,
    WorkloadReaderFactAdapter,
    extract_facts,
    load_facts_json,
    save_facts_json,
)


@runtime_checkable
class SafetyKernel(Protocol):
    """Formal safety verification kernel using SMT solvers."""

    async def verify(self, plan: RemediationPlan, facts: list[Fact]) -> KernelVerdict:
        """Evaluate safety invariants for a proposed plan against current system facts."""
        raise NotImplementedError


__all__ = [
    "FactExtractor",
    "K8sFactSource",
    "SafetyKernel",
    "WorkloadReaderFactAdapter",
    "extract_facts",
    "load_facts_json",
    "save_facts_json",
]
