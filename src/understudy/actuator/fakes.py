"""Deterministic fake actuator implementation."""

from understudy.actuator.api import Actuator
from understudy.common.errors import ActuationError
from understudy.contracts.enums import KernelVerdictType
from understudy.contracts.incident import IncidentContext
from understudy.contracts.kernel import KernelVerdict
from understudy.contracts.plan import RemediationPlan


class FakeActuator(Actuator):
    """Deterministic fake actuator recording applied and reverted plans."""

    def __init__(self) -> None:
        self.applied_plans: list[tuple[str, str]] = []  # (plan_id, namespace)
        self.applied_service_accounts: list[str | None] = []
        self.applied_contexts: list[IncidentContext | None] = []
        self.reverted_plans: list[tuple[str, str]] = []

    async def apply(
        self,
        plan: RemediationPlan,
        namespace: str,
        service_account: str | None = None,
    ) -> bool:
        """Apply plan to target namespace."""
        self.applied_plans.append((plan.plan_id, namespace))
        self.applied_service_accounts.append(service_account)
        return True

    async def apply_to_production(
        self,
        plan: RemediationPlan,
        verdict: KernelVerdict,
        context: IncidentContext | None = None,
    ) -> bool:
        """Apply plan to production namespace, enforcing K10 PASS verdict."""
        self.applied_contexts.append(context)
        if verdict.verdict != KernelVerdictType.PASS:
            msg = (
                f"Cannot actuate plan {plan.plan_id} on production without PASS "
                f"verdict (got {verdict.verdict})"
            )
            raise ActuationError(msg)
        if verdict.plan_id != plan.plan_id:
            msg = (
                f"Cannot actuate plan {plan.plan_id} on production: "
                f"verdict plan_id mismatch ({verdict.plan_id})"
            )
            raise ActuationError(msg)
        self.applied_plans.append((plan.plan_id, "ust-prod"))
        return True

    async def revert(self, plan: RemediationPlan, namespace: str) -> bool:
        """Revert plan from namespace."""
        self.reverted_plans.append((plan.plan_id, namespace))
        return True
