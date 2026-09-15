"""Safety kernel component protocol interfaces and fact extraction APIs."""

from typing import Protocol, runtime_checkable

from understudy.contracts.kernel import Fact, KernelVerdict
from understudy.contracts.plan import RemediationPlan
from understudy.kernel.catalogue import (
    CATALOGUE_INVARIANTS,
    generate_catalogue_markdown,
    get_catalogue_invariants,
    get_invariant_counts,
    render_entry_markdown,
    update_docs_catalogue,
    verify_catalogue_matches_docs,
)
from understudy.kernel.explain import (
    VetoExplanation,
    explain_veto,
    format_actionable_prose,
    render_veto_reason,
)
from understudy.kernel.facts import (
    FactExtractor,
    K8sFactSource,
    WorkloadReaderFactAdapter,
    extract_facts,
    load_facts_json,
    save_facts_json,
)
from understudy.kernel.verify import (
    PROOF_INVARIANTS,
    Z3SafetyKernel,
    verify,
)


@runtime_checkable
class SafetyKernel(Protocol):
    """Formal safety verification kernel using SMT solvers."""

    async def verify(self, plan: RemediationPlan, facts: list[Fact]) -> KernelVerdict:
        """Evaluate safety invariants for a proposed plan against current system facts."""
        raise NotImplementedError


__all__ = [
    "CATALOGUE_INVARIANTS",
    "PROOF_INVARIANTS",
    "FactExtractor",
    "K8sFactSource",
    "SafetyKernel",
    "VetoExplanation",
    "WorkloadReaderFactAdapter",
    "Z3SafetyKernel",
    "explain_veto",
    "extract_facts",
    "format_actionable_prose",
    "generate_catalogue_markdown",
    "get_catalogue_invariants",
    "get_invariant_counts",
    "load_facts_json",
    "render_entry_markdown",
    "render_veto_reason",
    "save_facts_json",
    "update_docs_catalogue",
    "verify",
    "verify_catalogue_matches_docs",
]
