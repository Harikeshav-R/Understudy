"""Remediation plan contract models."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from understudy.contracts.enums import ActionType


class ResourceRef(BaseModel):
    """Reference to a Kubernetes resource targeted for mutation."""

    model_config = ConfigDict(frozen=True)

    namespace: str
    kind: Literal["Deployment", "ConfigMap", "Secret"]
    name: str


class ActionParams(BaseModel):
    """Parameters specific to an action type."""

    model_config = ConfigDict(frozen=True)

    workload: str  # e.g. "data-service"
    target_commit: str | None = None  # ROLLBACK_DEPLOY
    replica_delta: int | None = None  # SCALE_WORKLOAD
    flag_name: str | None = None  # DISABLE_FLAG
    config_key: str | None = None  # REVERT_CONFIG
    config_value: str | None = None


class RemediationPlan(BaseModel):
    """Declarative, typed, and reversible remediation plan candidate."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    candidate_index: int
    action: ActionType
    params: ActionParams
    target_resources: list[ResourceRef] = Field(
        default_factory=list
    )  # exactly what will be mutated
    declared_blast_set: list[str] = Field(
        default_factory=list
    )  # service names expected to be affected
    inverse: "RemediationPlan | None" = None  # K9 requires non-null except for NO_ACTION
    rationale: str  # planner's stated reasoning, for the Slack post
    origin: Literal["planner", "playbook", "shadow"]
    playbook_id: str | None = None


RemediationPlan.model_rebuild()
