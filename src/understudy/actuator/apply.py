"""Kubernetes remediation plan application engine.

Implements build-plan step 5.1:
- Apply a RemediationPlan to a namespace via the Kubernetes API.
- One idempotent handler per action type in ActionType:
  - rollback_deploy: Roll back container image to pre-incident digest/commit.
  - restart_workload: Trigger rolling restart via pod template annotation.
  - scale_workload: Adjust Deployment replica count by replica_delta.
  - disable_flag: Disable feature flag in ConfigMap (or re-enable on inverse).
  - revert_config: Restore configuration key/value in ConfigMap.
  - no_action: Explicit non-mutating candidate (no-op).
- Idempotent: Repeated application of the same plan produces no drift.
- Parameterized namespace and ServiceAccount (impersonating understudy-prod in prod,
  understudy-twin in twins per ADR-028).
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import Settings, get_settings
from understudy.common.errors import ActuationError
from understudy.common.logging import get_logger
from understudy.contracts.enums import ActionType
from understudy.contracts.plan import RemediationPlan
from understudy.signals.api import DeployHistory

logger = get_logger(__name__)

DEFAULT_K8S_TIMEOUT_SECONDS = 10.0
ANNOTATION_PLAN_ID = "understudy.dev/last-applied-plan-id"
ANNOTATION_REPLICA_DELTA = "understudy.dev/applied-replica-delta"
ANNOTATION_RESTART_TIMESTAMP = "kubectl.kubernetes.io/restartedAt"


def resolve_service_account(
    namespace: str,
    service_account: str | None = None,
    settings: Settings | None = None,
) -> str:
    """Resolve the authorized ServiceAccount name for a target namespace."""
    if service_account is not None and service_account.strip():
        return service_account.strip()

    cfg = settings or get_settings()
    prod_ns = cfg.cluster.prod_namespace
    if namespace == prod_ns:
        return "understudy-prod"
    return "understudy-twin"


def create_k8s_clients(
    service_account: str | None = None,
    namespace: str | None = None,
    context: str | None = None,
) -> tuple[client.AppsV1Api, client.CoreV1Api]:
    """Create Kubernetes API clients with optional ServiceAccount impersonation."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config(context=context)

    api_client = client.ApiClient()
    if service_account and namespace:
        api_client.set_default_header(
            "Impersonate-User", f"system:serviceaccount:{namespace}:{service_account}"
        )

    return client.AppsV1Api(api_client=api_client), client.CoreV1Api(api_client=api_client)


def _extract_base_repo(image_spec: str) -> str:
    """Extract the repository prefix without tag or digest."""
    clean = image_spec.strip()
    if "@" in clean:
        return clean.split("@", 1)[0]
    if ":" in clean:
        parts = clean.rsplit(":", 1)
        if "/" not in parts[1]:
            return parts[0]
    return clean


class K8sPlanApplier:
    """Executes declarative remediation plans against Kubernetes namespaces idempotently."""

    def __init__(
        self,
        apps_api: client.AppsV1Api | None = None,
        core_api: client.CoreV1Api | None = None,
        deploy_history: DeployHistory | None = None,
        clock: Clock | None = None,
        settings: Settings | None = None,
        timeout_seconds: float = DEFAULT_K8S_TIMEOUT_SECONDS,
        context: str | None = None,
    ) -> None:
        self._apps_api = apps_api
        self._core_api = core_api
        self._deploy_history = deploy_history
        self._clock = resolve_clock(clock)
        self._settings = settings
        self.timeout_seconds = timeout_seconds
        self.context = context

    def _get_apps_api(self, namespace: str, service_account: str | None) -> client.AppsV1Api:
        """Return the AppsV1Api client, creating with impersonation if not provided."""
        if self._apps_api is not None:
            return self._apps_api
        apps, _ = create_k8s_clients(
            service_account=service_account,
            namespace=namespace,
            context=self.context,
        )
        return apps

    def _get_core_api(self, namespace: str, service_account: str | None) -> client.CoreV1Api:
        """Return the CoreV1Api client, creating with impersonation if not provided."""
        if self._core_api is not None:
            return self._core_api
        _, core = create_k8s_clients(
            service_account=service_account,
            namespace=namespace,
            context=self.context,
        )
        return core

    async def apply(
        self,
        plan: RemediationPlan,
        namespace: str,
        service_account: str | None = None,
    ) -> bool:
        """Apply a remediation plan to a specific namespace idempotently.

        Args:
            plan: The RemediationPlan candidate to execute.
            namespace: Target Kubernetes namespace (e.g. ust-prod or twin namespace).
            service_account: Optional ServiceAccount to impersonate. If omitted, defaults
                to understudy-prod for prod or understudy-twin for twins.

        Returns:
            True if the plan was applied or already in the desired state.

        Raises:
            ActuationError: If an action parameter is invalid, a resource cannot be found,
                or a Kubernetes API operation fails.
        """
        resolved_sa = resolve_service_account(
            namespace=namespace,
            service_account=service_account,
            settings=self._settings,
        )

        log = logger.bind(
            plan_id=plan.plan_id,
            action=plan.action.value,
            namespace=namespace,
            service_account=resolved_sa,
        )
        log.info("applying_remediation_plan")

        handler = self._HANDLERS.get(plan.action)
        if handler is None:
            raise ActuationError(
                f"Unsupported action type for actuation: {plan.action}",
                details={"plan_id": plan.plan_id, "action": str(plan.action)},
            )

        success = await handler(self, plan, namespace, resolved_sa)
        log.info("remediation_plan_applied", success=success)
        return success

    async def revert(
        self,
        plan: RemediationPlan,
        namespace: str,
        service_account: str | None = None,
    ) -> bool:
        """Revert an applied remediation plan by executing its declared inverse.

        Args:
            plan: The previously applied RemediationPlan to revert.
            namespace: Target Kubernetes namespace.
            service_account: Optional ServiceAccount override.

        Returns:
            True if inverse plan application succeeded.

        Raises:
            ActuationError: If the plan is non-reversible (NO_ACTION) or has no inverse.
        """
        if plan.action == ActionType.NO_ACTION:
            raise ActuationError(
                f"Cannot revert plan {plan.plan_id}: no_action is explicitly non-reversible",
                details={"plan_id": plan.plan_id, "action": plan.action.value},
            )

        if plan.inverse is None:
            raise ActuationError(
                f"Cannot revert plan {plan.plan_id}: plan declares no inverse",
                details={"plan_id": plan.plan_id, "action": plan.action.value},
            )

        return await self.apply(
            plan=plan.inverse,
            namespace=namespace,
            service_account=service_account,
        )

    async def _resolve_rollback_image(
        self,
        workload: str,
        target_commit: str,
        current_image: str,
    ) -> str:
        """Resolve the target commit or digest into an apply-ready container image string."""
        clean = target_commit.strip()

        # 1. Raw sha256 digest reference
        if clean.startswith("sha256:"):
            base = _extract_base_repo(current_image)
            return f"{base}@{clean}"
        if len(clean) == 64 and all(c in "0123456789abcdefABCDEF" for c in clean):
            base = _extract_base_repo(current_image)
            return f"{base}@sha256:{clean.lower()}"

        # 2. Direct image tag or repository reference (must contain / or non-sha256 :)
        if "/" in clean or ":" in clean:
            return clean

        # 3. DeployHistory commit SHA lookup
        if self._deploy_history is not None:
            try:
                deploys = await self._deploy_history.recent_deploys(limit=20)
                clean_ref = clean
                match = next(
                    (
                        d
                        for d in deploys
                        if d.commit_sha == clean_ref
                        or d.commit_sha.startswith(clean_ref)
                        or clean_ref.startswith(d.commit_sha)
                    ),
                    None,
                )
                if match and workload in match.image_digests and match.image_digests[workload]:
                    resolved = match.image_digests[workload]
                    if resolved.startswith("sha256:"):
                        base = _extract_base_repo(current_image)
                        return f"{base}@{resolved}"
                    return resolved
            except Exception as exc:
                logger.warning("deploy_history_resolution_failed", error=str(exc))

        raise ActuationError(
            f"Cannot resolve commit '{target_commit}' to an image digest for workload '{workload}'",
            details={"workload": workload, "target_commit": target_commit},
        )

    async def _apply_rollback_deploy(
        self,
        plan: RemediationPlan,
        namespace: str,
        service_account: str,
    ) -> bool:
        """Roll back Deployment container image to target commit or digest."""
        workload = plan.params.workload
        if not workload:
            raise ActuationError(
                f"Cannot apply rollback_deploy for plan {plan.plan_id}: workload name is required",
                details={"plan_id": plan.plan_id},
            )

        target_commit = plan.params.target_commit
        if not target_commit:
            raise ActuationError(
                f"Cannot apply rollback_deploy for plan {plan.plan_id}: target_commit is required",
                details={"plan_id": plan.plan_id, "workload": workload},
            )

        apps_api = self._get_apps_api(namespace, service_account)

        # Read current Deployment
        try:
            dep = await asyncio.to_thread(
                apps_api.read_namespaced_deployment,
                name=workload,
                namespace=namespace,
                _request_timeout=self.timeout_seconds,
            )
        except ApiException as exc:
            raise ActuationError(
                f"Failed to read Deployment '{workload}' in namespace '{namespace}': {exc.reason}",
                details={"status": exc.status, "body": str(exc.body), "plan_id": plan.plan_id},
            ) from exc

        containers = (
            (dep.spec.template.spec.containers or [])
            if dep.spec and dep.spec.template and dep.spec.template.spec
            else []
        )
        if not containers:
            raise ActuationError(
                f"Deployment '{workload}' in namespace '{namespace}' has no containers",
                details={"plan_id": plan.plan_id, "workload": workload},
            )

        target_container = next((c for c in containers if c.name == workload), containers[0])
        current_image = target_container.image or ""

        target_image = await self._resolve_rollback_image(
            workload=workload,
            target_commit=target_commit,
            current_image=current_image,
        )

        # Idempotency check: already at target image
        if current_image == target_image:
            logger.info(
                "deployment_already_at_target_image",
                workload=workload,
                image=target_image,
                namespace=namespace,
            )
            return True

        patch: dict[str, Any] = {
            "metadata": {
                "annotations": {
                    ANNOTATION_PLAN_ID: plan.plan_id,
                }
            },
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            ANNOTATION_PLAN_ID: plan.plan_id,
                        }
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": target_container.name,
                                "image": target_image,
                            }
                        ]
                    },
                }
            },
        }

        try:
            await asyncio.to_thread(
                apps_api.patch_namespaced_deployment,
                name=workload,
                namespace=namespace,
                body=patch,
                _request_timeout=self.timeout_seconds,
            )
        except ApiException as exc:
            msg = (
                f"Failed to patch Deployment '{workload}' image in namespace '{namespace}': "
                f"{exc.reason}"
            )
            raise ActuationError(
                msg,
                details={"status": exc.status, "body": str(exc.body), "plan_id": plan.plan_id},
            ) from exc

        return True

    async def _apply_restart_workload(
        self,
        plan: RemediationPlan,
        namespace: str,
        service_account: str,
    ) -> bool:
        """Trigger a rolling restart of a Deployment via pod template annotation."""
        workload = plan.params.workload
        if not workload:
            raise ActuationError(
                f"Cannot apply restart_workload for plan {plan.plan_id}: workload name is required",
                details={"plan_id": plan.plan_id},
            )

        apps_api = self._get_apps_api(namespace, service_account)

        try:
            dep = await asyncio.to_thread(
                apps_api.read_namespaced_deployment,
                name=workload,
                namespace=namespace,
                _request_timeout=self.timeout_seconds,
            )
        except ApiException as exc:
            raise ActuationError(
                f"Failed to read Deployment '{workload}' in namespace '{namespace}': {exc.reason}",
                details={"status": exc.status, "body": str(exc.body), "plan_id": plan.plan_id},
            ) from exc

        # Idempotency check: already restarted for this plan
        dep_annotations = (
            dep.metadata.annotations if dep.metadata and dep.metadata.annotations else {}
        )
        if dep_annotations.get(ANNOTATION_PLAN_ID) == plan.plan_id:
            logger.info(
                "deployment_already_restarted_for_plan",
                workload=workload,
                plan_id=plan.plan_id,
                namespace=namespace,
            )
            return True

        now_iso = self._clock.now().isoformat()
        patch: dict[str, Any] = {
            "metadata": {
                "annotations": {
                    ANNOTATION_PLAN_ID: plan.plan_id,
                }
            },
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            ANNOTATION_RESTART_TIMESTAMP: now_iso,
                            ANNOTATION_PLAN_ID: plan.plan_id,
                        }
                    }
                }
            },
        }

        try:
            await asyncio.to_thread(
                apps_api.patch_namespaced_deployment,
                name=workload,
                namespace=namespace,
                body=patch,
                _request_timeout=self.timeout_seconds,
            )
        except ApiException as exc:
            msg = (
                f"Failed to restart Deployment '{workload}' in namespace '{namespace}': "
                f"{exc.reason}"
            )
            raise ActuationError(
                msg,
                details={"status": exc.status, "body": str(exc.body), "plan_id": plan.plan_id},
            ) from exc

        return True

    async def _apply_scale_workload(
        self,
        plan: RemediationPlan,
        namespace: str,
        service_account: str,
    ) -> bool:
        """Adjust Deployment replica count by replica_delta."""
        workload = plan.params.workload
        if not workload:
            raise ActuationError(
                f"Cannot apply scale_workload for plan {plan.plan_id}: workload name is required",
                details={"plan_id": plan.plan_id},
            )

        replica_delta = plan.params.replica_delta
        if replica_delta is None:
            raise ActuationError(
                f"Cannot apply scale_workload for plan {plan.plan_id}: replica_delta is required",
                details={"plan_id": plan.plan_id, "workload": workload},
            )

        apps_api = self._get_apps_api(namespace, service_account)

        try:
            dep = await asyncio.to_thread(
                apps_api.read_namespaced_deployment,
                name=workload,
                namespace=namespace,
                _request_timeout=self.timeout_seconds,
            )
        except ApiException as exc:
            raise ActuationError(
                f"Failed to read Deployment '{workload}' in namespace '{namespace}': {exc.reason}",
                details={"status": exc.status, "body": str(exc.body), "plan_id": plan.plan_id},
            ) from exc

        # Idempotency check: already scaled for this plan
        dep_annotations = (
            dep.metadata.annotations if dep.metadata and dep.metadata.annotations else {}
        )
        if dep_annotations.get(ANNOTATION_PLAN_ID) == plan.plan_id:
            logger.info(
                "deployment_already_scaled_for_plan",
                workload=workload,
                plan_id=plan.plan_id,
                namespace=namespace,
            )
            return True

        current_replicas = dep.spec.replicas if dep.spec and dep.spec.replicas is not None else 1
        new_replicas = current_replicas + replica_delta

        if new_replicas < 0:
            raise ActuationError(
                f"Cannot scale workload '{workload}' below 0 replicas "
                f"(current: {current_replicas}, delta: {replica_delta})",
                details={
                    "plan_id": plan.plan_id,
                    "workload": workload,
                    "current_replicas": current_replicas,
                    "replica_delta": replica_delta,
                },
            )

        patch: dict[str, Any] = {
            "metadata": {
                "annotations": {
                    ANNOTATION_PLAN_ID: plan.plan_id,
                    ANNOTATION_REPLICA_DELTA: str(replica_delta),
                }
            },
            "spec": {
                "replicas": new_replicas,
            },
        }

        try:
            await asyncio.to_thread(
                apps_api.patch_namespaced_deployment,
                name=workload,
                namespace=namespace,
                body=patch,
                _request_timeout=self.timeout_seconds,
            )
        except ApiException as exc:
            raise ActuationError(
                f"Failed to scale Deployment '{workload}' in namespace '{namespace}': {exc.reason}",
                details={"status": exc.status, "body": str(exc.body), "plan_id": plan.plan_id},
            ) from exc

        return True

    def _resolve_config_map_name(
        self,
        plan: RemediationPlan,
        default_name: str,
    ) -> str:
        """Resolve ConfigMap name from target resources or action parameters."""
        for target in plan.target_resources:
            if target.kind == "ConfigMap" and target.name:
                return target.name

        workload = plan.params.workload
        if workload and workload.strip() in ("app-config", "feature-flags"):
            return workload.strip()

        return default_name

    async def _apply_disable_flag(
        self,
        plan: RemediationPlan,
        namespace: str,
        service_account: str,
    ) -> bool:
        """Disable feature flag in ConfigMap (or re-enable on inverse plan)."""
        flag_name = plan.params.flag_name
        if not flag_name:
            raise ActuationError(
                f"Cannot apply disable_flag for plan {plan.plan_id}: flag_name is required",
                details={"plan_id": plan.plan_id},
            )

        # Inverse plans re-enable the flag
        if (
            plan.rationale.lower().startswith("re-enable")
            or getattr(plan.params, "config_value", None) == "true"
        ):
            target_value = "true"
        else:
            target_value = "false"

        cm_name = self._resolve_config_map_name(plan, default_name="feature-flags")
        core_api = self._get_core_api(namespace, service_account)

        try:
            cm = await asyncio.to_thread(
                core_api.read_namespaced_config_map,
                name=cm_name,
                namespace=namespace,
                _request_timeout=self.timeout_seconds,
            )
        except ApiException as exc:
            raise ActuationError(
                f"Failed to read ConfigMap '{cm_name}' in namespace '{namespace}': {exc.reason}",
                details={"status": exc.status, "body": str(exc.body), "plan_id": plan.plan_id},
            ) from exc

        data = cm.data or {}
        # Idempotency check: flag already at desired value
        if data.get(flag_name) == target_value:
            logger.info(
                "flag_already_at_target_value",
                cm=cm_name,
                flag=flag_name,
                value=target_value,
                namespace=namespace,
            )
            return True

        patch: dict[str, Any] = {
            "metadata": {
                "annotations": {
                    ANNOTATION_PLAN_ID: plan.plan_id,
                }
            },
            "data": {
                flag_name: target_value,
            },
        }

        try:
            await asyncio.to_thread(
                core_api.patch_namespaced_config_map,
                name=cm_name,
                namespace=namespace,
                body=patch,
                _request_timeout=self.timeout_seconds,
            )
        except ApiException as exc:
            raise ActuationError(
                f"Failed to patch ConfigMap '{cm_name}' in namespace '{namespace}': {exc.reason}",
                details={"status": exc.status, "body": str(exc.body), "plan_id": plan.plan_id},
            ) from exc

        return True

    async def _apply_revert_config(
        self,
        plan: RemediationPlan,
        namespace: str,
        service_account: str,
    ) -> bool:
        """Restore configuration key/value in ConfigMap."""
        config_key = plan.params.config_key
        if not config_key:
            raise ActuationError(
                f"Cannot apply revert_config for plan {plan.plan_id}: config_key is required",
                details={"plan_id": plan.plan_id},
            )

        config_value = plan.params.config_value
        if config_value is None:
            raise ActuationError(
                f"Cannot apply revert_config for plan {plan.plan_id}: config_value is required",
                details={"plan_id": plan.plan_id, "config_key": config_key},
            )

        cm_name = self._resolve_config_map_name(plan, default_name="app-config")
        core_api = self._get_core_api(namespace, service_account)

        try:
            cm = await asyncio.to_thread(
                core_api.read_namespaced_config_map,
                name=cm_name,
                namespace=namespace,
                _request_timeout=self.timeout_seconds,
            )
        except ApiException as exc:
            raise ActuationError(
                f"Failed to read ConfigMap '{cm_name}' in namespace '{namespace}': {exc.reason}",
                details={"status": exc.status, "body": str(exc.body), "plan_id": plan.plan_id},
            ) from exc

        data = cm.data or {}
        # Idempotency check: config_key already has the desired value
        target_val_str = str(config_value)
        if data.get(config_key) == target_val_str:
            logger.info(
                "config_key_already_at_value",
                cm=cm_name,
                key=config_key,
                value=target_val_str,
                namespace=namespace,
            )
            return True

        patch: dict[str, Any] = {
            "metadata": {
                "annotations": {
                    ANNOTATION_PLAN_ID: plan.plan_id,
                }
            },
            "data": {
                config_key: target_val_str,
            },
        }

        try:
            await asyncio.to_thread(
                core_api.patch_namespaced_config_map,
                name=cm_name,
                namespace=namespace,
                body=patch,
                _request_timeout=self.timeout_seconds,
            )
        except ApiException as exc:
            raise ActuationError(
                f"Failed to patch ConfigMap '{cm_name}' in namespace '{namespace}': {exc.reason}",
                details={"status": exc.status, "body": str(exc.body), "plan_id": plan.plan_id},
            ) from exc

        return True

    async def _apply_no_action(
        self,
        plan: RemediationPlan,
        namespace: str,
        _service_account: str,
    ) -> bool:
        """Explicit do-nothing candidate."""
        logger.info(
            "no_action_applied",
            plan_id=plan.plan_id,
            namespace=namespace,
        )
        return True

    _HANDLERS: ClassVar[
        dict[
            ActionType,
            Callable[["K8sPlanApplier", RemediationPlan, str, str], Awaitable[bool]],
        ]
    ] = {
        ActionType.ROLLBACK_DEPLOY: _apply_rollback_deploy,
        ActionType.RESTART_WORKLOAD: _apply_restart_workload,
        ActionType.SCALE_WORKLOAD: _apply_scale_workload,
        ActionType.DISABLE_FLAG: _apply_disable_flag,
        ActionType.REVERT_CONFIG: _apply_revert_config,
        ActionType.NO_ACTION: _apply_no_action,
    }


# Standalone function interfaces
async def apply_plan(
    plan: RemediationPlan,
    namespace: str,
    service_account: str | None = None,
    apps_api: client.AppsV1Api | None = None,
    core_api: client.CoreV1Api | None = None,
    deploy_history: DeployHistory | None = None,
    clock: Clock | None = None,
    settings: Settings | None = None,
    timeout_seconds: float = DEFAULT_K8S_TIMEOUT_SECONDS,
    context: str | None = None,
) -> bool:
    """Convenience function to apply a plan via a transient K8sPlanApplier."""
    applier = K8sPlanApplier(
        apps_api=apps_api,
        core_api=core_api,
        deploy_history=deploy_history,
        clock=clock,
        settings=settings,
        timeout_seconds=timeout_seconds,
        context=context,
    )
    return await applier.apply(plan, namespace, service_account)


async def revert_plan(
    plan: RemediationPlan,
    namespace: str,
    service_account: str | None = None,
    apps_api: client.AppsV1Api | None = None,
    core_api: client.CoreV1Api | None = None,
    deploy_history: DeployHistory | None = None,
    clock: Clock | None = None,
    settings: Settings | None = None,
    timeout_seconds: float = DEFAULT_K8S_TIMEOUT_SECONDS,
    context: str | None = None,
) -> bool:
    """Convenience function to revert an applied plan via a transient K8sPlanApplier."""
    applier = K8sPlanApplier(
        apps_api=apps_api,
        core_api=core_api,
        deploy_history=deploy_history,
        clock=clock,
        settings=settings,
        timeout_seconds=timeout_seconds,
        context=context,
    )
    return await applier.revert(plan, namespace, service_account)


__all__ = [
    "ANNOTATION_PLAN_ID",
    "ANNOTATION_REPLICA_DELTA",
    "ANNOTATION_RESTART_TIMESTAMP",
    "DEFAULT_K8S_TIMEOUT_SECONDS",
    "K8sPlanApplier",
    "apply_plan",
    "create_k8s_clients",
    "resolve_service_account",
    "revert_plan",
]
