"""Scenario alert templates and synthetic alert generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from understudy.common.clock import Clock, resolve_clock
from understudy.common.ids import new_alert_id
from understudy.contracts.incident import Alert


@dataclass(frozen=True)
class ScenarioAlertTemplate:
    """Alert metadata associated with an incident scenario."""

    scenario_id: str
    title: str
    service: str
    severity: Literal["critical", "error", "warning"]


# Canonical 12 seed scenarios defined in docs/05-evaluation.md §5.1
SEED_SCENARIO_TEMPLATES: dict[str, ScenarioAlertTemplate] = {
    "bad_deploy_data_service": ScenarioAlertTemplate(
        scenario_id="bad_deploy_data_service",
        title="p99 latency SLO breach on edge-gateway",
        service="edge-gateway",
        severity="critical",
    ),
    "bad_deploy_auth_cache": ScenarioAlertTemplate(
        scenario_id="bad_deploy_auth_cache",
        title="p99 latency SLO breach on edge-gateway",
        service="edge-gateway",
        severity="critical",
    ),
    "bad_deploy_with_migration": ScenarioAlertTemplate(
        scenario_id="bad_deploy_with_migration",
        title="p99 latency SLO breach on edge-gateway",
        service="edge-gateway",
        severity="critical",
    ),
    "pool_exhaustion_data": ScenarioAlertTemplate(
        scenario_id="pool_exhaustion_data",
        title="Database connection pool exhaustion on data-service",
        service="data-service",
        severity="critical",
    ),
    "memory_leak_auth": ScenarioAlertTemplate(
        scenario_id="memory_leak_auth",
        title="Memory usage breach on auth-service",
        service="auth-service",
        severity="critical",
    ),
    "worker_backlog": ScenarioAlertTemplate(
        scenario_id="worker_backlog",
        title="Queue backlog breach on worker",
        service="worker",
        severity="error",
    ),
    "latency_data_service": ScenarioAlertTemplate(
        scenario_id="latency_data_service",
        title="Latency spike on data-service",
        service="data-service",
        severity="critical",
    ),
    "error_rate_auth": ScenarioAlertTemplate(
        scenario_id="error_rate_auth",
        title="Elevated 5xx error rate on auth-service",
        service="auth-service",
        severity="critical",
    ),
    "hard_down_dependency": ScenarioAlertTemplate(
        scenario_id="hard_down_dependency",
        title="Dependency unavailable: data-service",
        service="data-service",
        severity="critical",
    ),
    "flag_bad_value": ScenarioAlertTemplate(
        scenario_id="flag_bad_value",
        title="Slow path execution breach on edge-gateway",
        service="edge-gateway",
        severity="error",
    ),
    "config_pool_size": ScenarioAlertTemplate(
        scenario_id="config_pool_size",
        title="Connection pool misconfiguration on data-service",
        service="data-service",
        severity="critical",
    ),
    "flag_plus_latency": ScenarioAlertTemplate(
        scenario_id="flag_plus_latency",
        title="Latency breach with concurrent flag change on edge-gateway",
        service="edge-gateway",
        severity="warning",
    ),
}


def _normalize_scenario_id(scenario_id: str) -> str:
    """Strip optional prefix like 'seed/' or 'generated/' and suffix '.yaml'."""
    norm = scenario_id.strip()
    if norm.endswith(".yaml") or norm.endswith(".yml"):
        norm = norm.rsplit(".", 1)[0]
    if "/" in norm:
        norm = norm.split("/")[-1]
    return norm


def load_scenario_template(scenario_id: str, base_dir: Path | None = None) -> ScenarioAlertTemplate:
    """Load scenario alert template from disk or built-in canonical registry."""
    clean_id = _normalize_scenario_id(scenario_id)

    root = base_dir or Path.cwd()
    candidate_paths = [
        root / "scenarios" / "seed" / f"{clean_id}.yaml",
        root / "scenarios" / "generated" / f"{clean_id}.yaml",
        root / "scenarios" / f"{clean_id}.yaml",
        root / f"{clean_id}.yaml",
    ]

    for p in candidate_paths:
        if p.is_file():
            try:
                data: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                template_data = data.get("alert_template", {})
                title = str(template_data.get("title", f"Alert for {clean_id}"))
                service = str(template_data.get("service", "edge-gateway"))
                raw_severity = template_data.get("severity", "critical")
                severity: Literal["critical", "error", "warning"] = (
                    raw_severity if raw_severity in ("critical", "error", "warning") else "critical"
                )
                return ScenarioAlertTemplate(
                    scenario_id=clean_id,
                    title=title,
                    service=service,
                    severity=severity,
                )
            except Exception as exc:
                msg = f"Failed to parse scenario file at {p}: {exc}"
                raise ValueError(msg) from exc

    if clean_id in SEED_SCENARIO_TEMPLATES:
        return SEED_SCENARIO_TEMPLATES[clean_id]

    msg = (
        f"Unknown scenario ID '{scenario_id}'. Expected one of {sorted(SEED_SCENARIO_TEMPLATES)} "
        "or an existing scenario YAML file."
    )
    raise ValueError(msg)


def list_known_scenarios() -> list[str]:
    """Return all canonical seed scenario identifiers."""
    return sorted(SEED_SCENARIO_TEMPLATES.keys())


def create_synthetic_alert(
    scenario_id: str,
    clock: Clock | None = None,
    title: str | None = None,
    service: str | None = None,
    severity: Literal["critical", "error", "warning"] | None = None,
    base_dir: Path | None = None,
) -> Alert:
    """Produce a normalized synthetic Alert based on a scenario template."""
    from understudy.signals.pagerduty import _normalize_service_name

    template = load_scenario_template(scenario_id=scenario_id, base_dir=base_dir)
    resolved_clock = resolve_clock(clock)

    final_title = title if title is not None else template.title
    final_service = _normalize_service_name(service if service is not None else template.service)
    final_severity = severity if severity is not None else template.severity

    return Alert(
        alert_id=new_alert_id(),
        source="synthetic",
        title=final_title,
        service=final_service,
        severity=final_severity,
        fired_at=resolved_clock.now(),
        raw={
            "scenario_id": template.scenario_id,
            "source": "synthetic_fallback",
        },
    )


# MOCKED: ScenarioDeployHistory overlays synthetic migration commits for live scenario evaluation.
# Real path: signals/github.py::GitHubDeployHistory. Tracked in #52.
class ScenarioDeployHistory:
    """DeployHistory adapter that overlays scenario-specific migration/deploy events."""

    def __init__(
        self,
        base: Any,
        scenario_id: str,
        clock: Clock | None = None,
        force_migration: bool = False,
    ) -> None:
        from understudy.signals.api import DeployHistory

        if not isinstance(base, DeployHistory):
            raise TypeError("base must implement DeployHistory protocol")
        self.base = base
        self.scenario_id = _normalize_scenario_id(scenario_id)
        self.clock = resolve_clock(clock)
        self.force_migration = force_migration

    async def recent_deploys(self, limit: int = 5) -> list[Any]:
        """Return recent deploys, injecting a migration commit if required by scenario."""
        from datetime import timedelta

        from understudy.contracts.incident import DeployRef

        deploys: list[DeployRef] = list(await self.base.recent_deploys(limit=limit))
        is_migration_scenario = self.force_migration or "migration" in self.scenario_id
        if is_migration_scenario and not any(d.contains_migration for d in deploys):
            if len(deploys) >= 2:
                head_time = deploys[0].deployed_at
                target_time = deploys[1].deployed_at
                if head_time > target_time:
                    mig_time = target_time + (head_time - target_time) / 2
                else:
                    mig_time = target_time + timedelta(seconds=60)
                mig_deploy = DeployRef(
                    commit_sha="mig_boundary_commit",
                    image_digests=dict(deploys[0].image_digests),
                    deployed_at=mig_time,
                    pr_number=999,
                    contains_migration=True,
                )
                deploys = [deploys[0], mig_deploy, *deploys[1:]]
            elif len(deploys) == 1:
                mig_time = deploys[0].deployed_at + timedelta(seconds=1)
                mig_deploy = DeployRef(
                    commit_sha="mig_boundary_commit",
                    image_digests=dict(deploys[0].image_digests),
                    deployed_at=mig_time,
                    pr_number=999,
                    contains_migration=True,
                )
                deploys = [mig_deploy, *deploys]
            else:
                now = self.clock.now()
                deploys = [
                    DeployRef(
                        commit_sha="c0ffee1",
                        image_digests={"data-service": "localhost:5001/data-service:regression"},
                        deployed_at=now,
                        pr_number=101,
                        contains_migration=False,
                    ),
                    DeployRef(
                        commit_sha="mig_boundary_commit",
                        image_digests={"data-service": "localhost:5001/data-service:good"},
                        deployed_at=now - timedelta(minutes=5),
                        pr_number=100,
                        contains_migration=True,
                    ),
                    DeployRef(
                        commit_sha="c0ffee0",
                        image_digests={"data-service": "localhost:5001/data-service:good"},
                        deployed_at=now - timedelta(minutes=10),
                        pr_number=99,
                        contains_migration=False,
                    ),
                ]
        elif (
            not is_migration_scenario
            and not any(d.contains_migration for d in deploys)
            and len(deploys) >= 3
        ):
            oldest = deploys[-1]
            mig_deploy = DeployRef(
                commit_sha=oldest.commit_sha,
                image_digests=dict(oldest.image_digests),
                deployed_at=oldest.deployed_at - timedelta(seconds=1),
                pr_number=oldest.pr_number,
                contains_migration=True,
            )
            deploys = [*deploys[:-1], mig_deploy]
        return deploys[:limit]

    async def close(self) -> None:
        """Close underlying deploy history adapter if close method exists."""
        if hasattr(self.base, "close"):
            await self.base.close()

    async def __aenter__(self) -> ScenarioDeployHistory:
        if hasattr(self.base, "__aenter__"):
            await self.base.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        if hasattr(self.base, "__aexit__"):
            await self.base.__aexit__(*args)
        elif hasattr(self.base, "close"):
            await self.base.close()
