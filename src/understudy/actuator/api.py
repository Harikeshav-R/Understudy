"""Actuator component protocol interfaces."""

from typing import Protocol, runtime_checkable

from understudy.contracts.kernel import KernelVerdict
from understudy.contracts.plan import RemediationPlan


@runtime_checkable
class Actuator(Protocol):
    """Executes remediation actions in twin and production environments."""

    async def apply(self, plan: RemediationPlan, namespace: str) -> bool:
        """Apply a remediation plan to a specific namespace."""
        raise NotImplementedError

    async def apply_to_production(self, plan: RemediationPlan, verdict: KernelVerdict) -> bool:
        """Apply a PASS-verified remediation plan to the production namespace."""
        raise NotImplementedError

    async def revert(self, plan: RemediationPlan, namespace: str) -> bool:
        """Revert an applied remediation plan by executing its inverse."""
        raise NotImplementedError


__all__ = ["Actuator"]
