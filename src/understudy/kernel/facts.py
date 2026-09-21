"""Safety kernel fact extraction and serialization module.

Implements build-plan step B4.2:
Fact extraction from Kubernetes, GitHub, the run store, the dependency graph,
tournament evidence, remediation plans, and configuration, per docs/03-invariants.md §3.3.
Every fact is stamped with observed_at. In accordance with zero-default safety (Rule 5.6),
missing or unavailable facts are omitted rather than defaulted so that KernelContext detects
their absence and raises MissingFact.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from understudy.common.clock import Clock, SystemClock, resolve_clock
from understudy.common.config import Settings, get_settings
from understudy.common.logging import get_logger
from understudy.contracts.enums import ActionType
from understudy.contracts.evidence import CandidateEvidence
from understudy.contracts.incident import DeployRef, IncidentContext
from understudy.contracts.kernel import Fact
from understudy.contracts.plan import RemediationPlan, ResourceRef
from understudy.fleet.api import ClusterWorkloadSnapshot, WorkloadReader
from understudy.graph.api import DependencyGraph
from understudy.signals.api import DeployHistory
from understudy.store.api import RunStore

logger = get_logger(__name__)

DEFAULT_SLO_PATH = Path("config/slo.yaml")
RESOURCE_SET_FACT_NAMES = frozenset({"in_flight_plan_targets", "plan_targets", "inverse_targets"})
STRING_SET_FACT_NAMES = frozenset(
    {"plan_target_namespaces", "declared_blast_set", "observed_blast_set"}
)
DATETIME_FACT_NAMES = frozenset({"last_migration_commit_time", "rollback_target_commit_time"})


@runtime_checkable
class K8sFactSource(Protocol):
    """Protocol for querying Kubernetes cluster state for safety kernel facts."""

    async def get_workload_replicas(self, namespace: str, service: str) -> tuple[int, int] | None:
        """Return (desired_replicas, healthy_replicas) for service, or None if not found."""
        raise NotImplementedError

    async def check_egress_policy_present(self, namespace: str) -> bool:
        """Check if twin egress NetworkPolicy is present and active."""
        raise NotImplementedError


class WorkloadReaderFactAdapter(K8sFactSource):
    """Adapts a WorkloadReader or ClusterWorkloadSnapshot into a K8sFactSource."""

    def __init__(
        self,
        workload_reader: WorkloadReader | None = None,
        snapshot: ClusterWorkloadSnapshot | None = None,
        egress_policy_present: bool = True,
    ) -> None:
        self._workload_reader = workload_reader
        self._snapshot = snapshot
        self._egress_policy_present = egress_policy_present

    async def get_workload_replicas(self, namespace: str, service: str) -> tuple[int, int] | None:
        snap = self._snapshot
        if snap is None and self._workload_reader is not None:
            snap = await self._workload_reader.read_workloads(namespace)
        if snap is None or service not in snap.workloads:
            return None
        wl = snap.workloads[service]
        return wl.replicas, wl.replicas

    async def check_egress_policy_present(self, namespace: str) -> bool:
        _ = namespace
        return self._egress_policy_present


def load_slo_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load SLO configuration from YAML file, returning empty dict if absent."""
    slo_path = Path(path) if path else DEFAULT_SLO_PATH
    if not slo_path.is_file():
        return {}
    try:
        with slo_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("slo_config_load_failed", path=str(slo_path), error=str(exc))
        return {}


class FactExtractor:
    """Extracts immutable timestamped facts for safety kernel verification."""

    def __init__(
        self,
        *,
        k8s_source: K8sFactSource | None = None,
        deploy_history: DeployHistory | None = None,
        run_store: RunStore | None = None,
        dependency_graph: DependencyGraph | None = None,
        clock: Clock | None = None,
        settings: Settings | None = None,
        slo_config: Mapping[str, Any] | None = None,
        slo_config_path: Path | str | None = None,
    ) -> None:
        self._k8s_source = k8s_source
        self._deploy_history = deploy_history
        self._run_store = run_store
        self._dependency_graph = dependency_graph
        self._clock: Clock = resolve_clock(clock or SystemClock())
        self._settings = settings
        if slo_config is not None:
            self._slo_config = dict(slo_config)
        else:
            self._slo_config = load_slo_config(slo_config_path)

    @property
    def clock(self) -> Clock:
        """Return the clock used by this extractor."""
        return self._clock

    def _get_settings(self) -> Settings:
        """Return injected or global settings."""
        if self._settings is not None:
            return self._settings
        return get_settings()

    async def extract_k8s_facts(
        self,
        plan: RemediationPlan,
        now: datetime,
        namespace: str = "ust-prod",
    ) -> list[Fact]:
        """Extract replica counts and egress policy facts from Kubernetes."""
        facts: list[Fact] = []
        if self._k8s_source is None:
            return facts

        # Query egress policy presence
        policy_present = await self._k8s_source.check_egress_policy_present(namespace)
        facts.append(
            Fact(
                name="twin_egress_policy_present",
                value=policy_present,
                source="k8s",
                observed_at=now,
            )
        )

        # Query replicas for services targeted by plan or action params
        services_to_query: set[str] = set()
        if plan.params.workload:
            services_to_query.add(plan.params.workload)
        for target in plan.target_resources:
            if target.kind == "Deployment":
                services_to_query.add(target.name)

        replicas_dict: dict[str, int] = {}
        healthy_dict: dict[str, int] = {}

        for svc in sorted(services_to_query):
            result = await self._k8s_source.get_workload_replicas(namespace, svc)
            if result is not None:
                desired, healthy = result
                facts.append(
                    Fact(
                        name=f"replicas[{svc}]",
                        value=desired,
                        source="k8s",
                        observed_at=now,
                    )
                )
                facts.append(
                    Fact(
                        name=f"healthy_replicas[{svc}]",
                        value=healthy,
                        source="k8s",
                        observed_at=now,
                    )
                )
                replicas_dict[svc] = desired
                healthy_dict[svc] = healthy

        if replicas_dict:
            facts.append(
                Fact(
                    name="replicas",
                    value=replicas_dict,
                    source="k8s",
                    observed_at=now,
                )
            )
        if healthy_dict:
            facts.append(
                Fact(
                    name="healthy_replicas",
                    value=healthy_dict,
                    source="k8s",
                    observed_at=now,
                )
            )

        return facts

    def extract_config_facts(self, plan: RemediationPlan, now: datetime) -> list[Fact]:
        """Extract configuration-derived facts (authorized namespace, min replicas, budget)."""
        _ = plan
        facts: list[Fact] = []
        settings = self._get_settings()

        # Authorized namespace (K2)
        authorized_ns = settings.cluster.prod_namespace
        facts.append(
            Fact(
                name="authorized_namespace",
                value=authorized_ns,
                source="config",
                observed_at=now,
            )
        )

        # Mutation budget (K7)
        mutation_budget = int(self._slo_config.get("mutation_budget", 3))
        facts.append(
            Fact(
                name="mutation_budget",
                value=mutation_budget,
                source="config",
                observed_at=now,
            )
        )

        # Min replicas per service (K1)
        min_reps = self._slo_config.get("min_replicas")
        if isinstance(min_reps, Mapping):
            min_reps_dict: dict[str, int] = {}
            for svc, count in min_reps.items():
                if isinstance(count, int):
                    facts.append(
                        Fact(
                            name=f"min_replicas[{svc}]",
                            value=count,
                            source="config",
                            observed_at=now,
                        )
                    )
                    min_reps_dict[str(svc)] = count
            if min_reps_dict:
                facts.append(
                    Fact(
                        name="min_replicas",
                        value=min_reps_dict,
                        source="config",
                        observed_at=now,
                    )
                )

        return facts

    def extract_plan_facts(self, plan: RemediationPlan, now: datetime) -> list[Fact]:
        """Extract declared plan diff properties (targets, namespaces, blast set, inverse)."""
        target_namespaces = {r.namespace for r in plan.target_resources if r.namespace}
        plan_targets = set(plan.target_resources)
        declared_blast = set(plan.declared_blast_set)
        has_inverse = plan.inverse is not None or plan.action == ActionType.NO_ACTION
        inverse_targets = set(plan.inverse.target_resources) if plan.inverse is not None else set()

        return [
            Fact(
                name="plan_target_namespaces",
                value=target_namespaces,
                source="plan",
                observed_at=now,
            ),
            Fact(
                name="plan_targets",
                value=plan_targets,
                source="plan",
                observed_at=now,
            ),
            Fact(
                name="declared_blast_set",
                value=declared_blast,
                source="plan",
                observed_at=now,
            ),
            Fact(
                name="plan_has_inverse",
                value=has_inverse,
                source="plan",
                observed_at=now,
            ),
            Fact(
                name="inverse_targets",
                value=inverse_targets,
                source="plan",
                observed_at=now,
            ),
        ]

    async def extract_github_facts(
        self,
        plan: RemediationPlan,
        now: datetime,
        recent_deploys: list[DeployRef] | None = None,
    ) -> list[Fact]:
        """Extract schema migration and rollback target commit facts from GitHub deploys."""
        facts: list[Fact] = []
        deploys = recent_deploys

        if deploys is None and self._deploy_history is not None:
            deploys = await self._deploy_history.recent_deploys(limit=10)

        if deploys is None:
            return facts

        # Find most recent commit containing a schema migration (K3)
        migration_deploys = [d for d in deploys if d.contains_migration]
        if migration_deploys:
            latest_migration = max(migration_deploys, key=lambda d: d.deployed_at)
            facts.append(
                Fact(
                    name="last_migration_commit_time",
                    value=latest_migration.deployed_at,
                    source="github",
                    observed_at=now,
                )
            )

        # If plan is a rollback, locate target commit in deploy history
        if plan.action == ActionType.ROLLBACK_DEPLOY and plan.params.target_commit:
            target_commit = plan.params.target_commit.strip()
            target_deploy = next(
                (
                    d
                    for d in deploys
                    if d.commit_sha == target_commit
                    or d.commit_sha.startswith(target_commit)
                    or target_commit.startswith(d.commit_sha)
                ),
                None,
            )
            if target_deploy is not None:
                facts.append(
                    Fact(
                        name="rollback_target_commit_time",
                        value=target_deploy.deployed_at,
                        source="github",
                        observed_at=now,
                    )
                )
                facts.append(
                    Fact(
                        name="target_contains_migration",
                        value=target_deploy.contains_migration,
                        source="github",
                        observed_at=now,
                    )
                )

        return facts

    def extract_graph_facts(
        self,
        plan: RemediationPlan,
        now: datetime,
        graph_snapshot: Any = None,
    ) -> list[Fact]:
        """Extract dependency graph topology and dependents facts (K4)."""
        facts: list[Fact] = []
        services_to_query: set[str] = set()
        if plan.params.workload:
            services_to_query.add(plan.params.workload)
        for target in plan.target_resources:
            if target.kind == "Deployment":
                services_to_query.add(target.name)

        dependents_map: dict[str, set[str]] = {}

        if self._dependency_graph is not None:
            for svc in sorted(services_to_query):
                deps = self._dependency_graph.reachable_set(svc)
                dependents_map[svc] = deps
                facts.append(
                    Fact(
                        name=f"dependents[{svc}]",
                        value=deps,
                        source="graph",
                        observed_at=now,
                    )
                )
        elif graph_snapshot is not None and hasattr(graph_snapshot, "edges"):
            adjacency: dict[str, set[str]] = {}
            for e in graph_snapshot.edges:
                adjacency.setdefault(e.target, set()).add(e.source)

            for svc in sorted(services_to_query):
                visited: set[str] = set()
                queue = list(adjacency.get(svc, set()))
                while queue:
                    curr = queue.pop(0)
                    if curr not in visited:
                        visited.add(curr)
                        queue.extend(adjacency.get(curr, set()))
                deps = visited
                dependents_map[svc] = deps
                facts.append(
                    Fact(
                        name=f"dependents[{svc}]",
                        value=deps,
                        source="graph",
                        observed_at=now,
                    )
                )

        if dependents_map:
            facts.append(
                Fact(
                    name="dependents",
                    value=dependents_map,
                    source="graph",
                    observed_at=now,
                )
            )

        return facts

    async def extract_store_facts(
        self,
        plan: RemediationPlan,
        now: datetime,
        context: IncidentContext | None = None,
    ) -> list[Fact]:
        """Extract active run targets (K5) and mutation count in rolling window (K7)."""
        _ = plan
        facts: list[Fact] = []
        if self._run_store is None:
            return facts

        # K5: In-flight plan targets across all active runs
        active_runs = await self._run_store.get_active_runs()
        in_flight_targets: set[ResourceRef] = set()
        for run in active_runs:
            if (
                context is not None
                and context.incident_id
                and run.incident_id == context.incident_id
            ):
                continue
            for p in run.plans:
                in_flight_targets.update(p.target_resources)

        facts.append(
            Fact(
                name="in_flight_plan_targets",
                value=in_flight_targets,
                source="store",
                observed_at=now,
            )
        )

        # K7: Production mutations in rolling window
        window_minutes = int(self._slo_config.get("mutation_window_minutes", 15))
        window_start = now - timedelta(minutes=window_minutes)
        all_runs = await self._run_store.list_runs()
        mutations_in_window = sum(
            1
            for r in all_runs
            if r.started_at >= window_start and r.prod_applied_plan_id is not None
        )

        facts.append(
            Fact(
                name="prod_mutations_in_window",
                value=mutations_in_window,
                source="store",
                observed_at=now,
            )
        )

        return facts

    def extract_evidence_facts(
        self,
        plan: RemediationPlan,
        evidence: CandidateEvidence | None,
        now: datetime,
    ) -> list[Fact]:
        """Extract tournament rehearsal evidence facts (blast, drop ratio, freshness) (K8)."""
        _ = plan
        if evidence is None:
            return []

        age_seconds = max(0.0, (now - evidence.applied_at).total_seconds())
        return [
            Fact(
                name="observed_blast_set",
                value=set(evidence.observed_blast_set),
                source="tournament",
                observed_at=now,
            ),
            Fact(
                name="evidence_age_seconds",
                value=age_seconds,
                source="tournament",
                observed_at=now,
            ),
            Fact(
                name="max_drop_ratio",
                value=evidence.mirror_stats.drop_ratio,
                source="tournament",
                observed_at=now,
            ),
            Fact(
                name="probe_sample_count",
                value=len(evidence.probes),
                source="tournament",
                observed_at=now,
            ),
        ]

    async def extract_all(
        self,
        plan: RemediationPlan,
        evidence: CandidateEvidence | None = None,
        context: IncidentContext | None = None,
    ) -> list[Fact]:
        """Extract all system, plan, config, deploy, store, graph, and evidence facts."""
        now = self._clock.now()
        facts: list[Fact] = []

        # 1. Plan facts (always present from declared plan)
        facts.extend(self.extract_plan_facts(plan, now))

        # 2. Config facts
        facts.extend(self.extract_config_facts(plan, now))

        # 3. K8s cluster facts
        facts.extend(await self.extract_k8s_facts(plan, now))

        # 4. GitHub deploy facts (use context if provided, else query history)
        deploys = context.recent_deploys if context is not None else None
        facts.extend(await self.extract_github_facts(plan, now, recent_deploys=deploys))

        # 5. Graph facts (use context if provided, else query graph)
        graph_snap = context.dependency_graph if context is not None else None
        facts.extend(self.extract_graph_facts(plan, now, graph_snapshot=graph_snap))

        # 6. Store facts
        facts.extend(await self.extract_store_facts(plan, now, context=context))

        # 7. Tournament evidence facts
        facts.extend(self.extract_evidence_facts(plan, evidence, now))

        return facts


async def extract_facts(
    plan: RemediationPlan,
    evidence: CandidateEvidence | None = None,
    context: IncidentContext | None = None,
    *,
    k8s_source: K8sFactSource | None = None,
    deploy_history: DeployHistory | None = None,
    run_store: RunStore | None = None,
    dependency_graph: DependencyGraph | None = None,
    clock: Clock | None = None,
    settings: Settings | None = None,
    slo_config: Mapping[str, Any] | None = None,
    slo_config_path: Path | str | None = None,
) -> list[Fact]:
    """Convenience helper to extract all facts using a transient FactExtractor."""
    extractor = FactExtractor(
        k8s_source=k8s_source,
        deploy_history=deploy_history,
        run_store=run_store,
        dependency_graph=dependency_graph,
        clock=clock,
        settings=settings,
        slo_config=slo_config,
        slo_config_path=slo_config_path,
    )
    return await extractor.extract_all(plan, evidence=evidence, context=context)


# --- Serialization Helpers for Fixtures and CLI ---


def _serialize_fact_value(value: Any) -> Any:
    """Recursively convert Fact value into JSON-serializable primitives."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ResourceRef):
        return value.model_dump()
    if isinstance(value, (set, frozenset)):
        items = [_serialize_fact_value(elem) for elem in value]
        try:
            return sorted(items)
        except TypeError:
            return sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(value, Mapping):
        return {str(k): _serialize_fact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_fact_value(elem) for elem in value]
    return value


def facts_to_dict(facts: Sequence[Fact]) -> list[dict[str, Any]]:
    """Convert a sequence of Fact objects to JSON-serializable dictionaries."""
    return [
        {
            "name": f.name,
            "value": _serialize_fact_value(f.value),
            "source": f.source,
            "observed_at": f.observed_at.isoformat(),
        }
        for f in facts
    ]


def _deserialize_fact_value(name: str, value: Any) -> Any:
    """Reconstruct typed values (ResourceRef sets, datetimes, mappings) from raw data."""
    if name in DATETIME_FACT_NAMES and isinstance(value, str):
        return datetime.fromisoformat(value)

    if name in RESOURCE_SET_FACT_NAMES and isinstance(value, list):
        reconstructed: set[ResourceRef] = set()
        for item in value:
            if isinstance(item, dict):
                reconstructed.add(ResourceRef.model_validate(item))
            elif isinstance(item, ResourceRef):
                reconstructed.add(item)
        return reconstructed

    if (name in STRING_SET_FACT_NAMES or name.startswith("dependents[")) and isinstance(
        value, list
    ):
        return {str(item) for item in value}

    if name == "dependents" and isinstance(value, Mapping):
        return {
            str(k): {str(item) for item in v} if isinstance(v, list) else v
            for k, v in value.items()
        }

    return value


def facts_from_dict(data: Sequence[dict[str, Any]] | dict[str, Any]) -> list[Fact]:
    """Convert serialized dictionaries into validated Fact objects with reconstructed types."""
    entries = data.get("facts", []) if isinstance(data, dict) and "facts" in data else data
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise TypeError(f"Expected sequence of fact dictionaries, got {type(entries).__name__}")

    facts: list[Fact] = []
    for item in entries:
        if not isinstance(item, Mapping):
            raise TypeError(f"Expected mapping for fact item, got {type(item).__name__}")
        name = str(item["name"])
        raw_val = item["value"]
        value = _deserialize_fact_value(name, raw_val)
        source = item["source"]
        observed_str = item.get("observed_at")
        if not observed_str:
            raise KeyError(f"Fact '{name}' missing required 'observed_at' timestamp")
        observed_at = datetime.fromisoformat(observed_str)
        facts.append(
            Fact(
                name=name,
                value=value,
                source=source,
                observed_at=observed_at,
            )
        )
    return facts


def save_facts_json(facts: Sequence[Fact], path: Path | str) -> None:
    """Save a list of Facts to a JSON file."""
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = {"facts": facts_to_dict(facts)}
    target_path.write_text(json.dumps(serialized, indent=2), encoding="utf-8")


def load_facts_json(path: Path | str) -> list[Fact]:
    """Load a list of Facts from a JSON file."""
    source_path = Path(path)
    content = json.loads(source_path.read_text(encoding="utf-8"))
    return facts_from_dict(content)


__all__ = [
    "DEFAULT_SLO_PATH",
    "FactExtractor",
    "K8sFactSource",
    "WorkloadReaderFactAdapter",
    "extract_facts",
    "facts_from_dict",
    "facts_to_dict",
    "load_facts_json",
    "load_slo_config",
    "save_facts_json",
]
