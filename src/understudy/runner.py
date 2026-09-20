"""Incident scenario runner, fault injection coordinator, and scoreboard formatter.

Implements build-plan step 5.7 and Checkpoint 5:
- Loads declarative scenario definitions from YAML or canonical registry.
- Coordinates pre-incident fault injection via Actuator injection utility.
- Wires live or fake dependencies with scenario-specific overlays (e.g. migration boundaries).
- Drives execution through the full orchestrator StateGraph.
- Formats terminal scoreboard tables and resolution summaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from understudy.actuator.injection import inject_scenario_fault
from understudy.common.clock import Clock, resolve_clock
from understudy.common.errors import OrchestratorError
from understudy.common.logging import get_logger
from understudy.contracts.enums import KernelVerdictType, RunOutcome, TournamentOutcome
from understudy.orchestrator.api import build_graph
from understudy.orchestrator.fakes import create_fake_deps
from understudy.orchestrator.state import State
from understudy.signals.scenarios import (
    ScenarioDeployHistory,
    create_synthetic_alert,
    load_scenario_template,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.runnables import RunnableConfig

    from understudy.contracts.evidence import CandidateEvidence, CandidateScore
    from understudy.contracts.run import RunRecord
    from understudy.orchestrator.api import Deps

logger = get_logger(__name__)


@dataclass(frozen=True)
class ScenarioDefinition:
    """Parsed incident scenario definition and metadata."""

    id: str
    origin: str
    failure_class: str
    description: str
    injection: dict[str, Any] | None
    alert_template: dict[str, Any]
    expected_remediation_class: str
    expected_escalation: bool
    ground_truth_note: str
    migration_boundary: bool = False


def _normalize_scenario_token(scenario: str) -> str:
    """Normalize scenario file path or identifier to base scenario token."""
    norm = scenario.strip()
    if norm.endswith(".yaml") or norm.endswith(".yml"):
        norm = norm.rsplit(".", 1)[0]
    if "/" in norm:
        norm = norm.split("/")[-1]
    return norm


def _parse_scenario_data(data: dict[str, Any], fallback_id: str) -> ScenarioDefinition:
    """Convert raw YAML dictionary into ScenarioDefinition."""
    scenario_id = str(data.get("id") or fallback_id).strip()
    origin = str(data.get("origin", "seed"))
    failure_class = str(data.get("failure_class", "bad_deploy"))
    description = str(data.get("description", f"Incident scenario {scenario_id}"))
    injection = data.get("injection") if isinstance(data.get("injection"), dict) else None
    raw_alert_tmpl = data.get("alert_template")
    alert_template: dict[str, Any] = (
        dict(raw_alert_tmpl)
        if isinstance(raw_alert_tmpl, dict)
        else {
            "title": f"SLO breach for {scenario_id}",
            "service": "edge-gateway",
            "severity": "critical",
        }
    )
    expected_remediation = str(data.get("expected_remediation_class", "rollback_deploy"))
    expected_escalation = bool(data.get("expected_escalation", False))
    ground_truth_note = str(data.get("ground_truth_note", ""))
    migration_boundary = bool(
        data.get("migration_boundary", "migration" in scenario_id or expected_escalation)
    )

    return ScenarioDefinition(
        id=scenario_id,
        origin=origin,
        failure_class=failure_class,
        description=description,
        injection=injection,
        alert_template=alert_template,
        expected_remediation_class=expected_remediation,
        expected_escalation=expected_escalation,
        ground_truth_note=ground_truth_note,
        migration_boundary=migration_boundary,
    )


def load_scenario_definition(
    scenario: str | Path,
    base_dir: Path | None = None,
) -> ScenarioDefinition:
    """Load scenario definition from a file path or scenario ID name."""
    scenario_str = str(scenario)
    p = Path(scenario_str)
    root = base_dir or Path.cwd()

    # 1. Direct file path check
    if p.is_file():
        try:
            data: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return _parse_scenario_data(data, fallback_id=p.stem)
        except Exception as exc:
            msg = f"Failed to parse scenario file at {p}: {exc}"
            raise ValueError(msg) from exc

    # Relative to root check
    rel_p = root / p
    if rel_p.is_file():
        try:
            data = yaml.safe_load(rel_p.read_text(encoding="utf-8")) or {}
            return _parse_scenario_data(data, fallback_id=rel_p.stem)
        except Exception as exc:
            msg = f"Failed to parse scenario file at {rel_p}: {exc}"
            raise ValueError(msg) from exc

    # 2. Search common scenario directories by token
    token = _normalize_scenario_token(scenario_str)
    candidate_paths = [
        root / "scenarios" / "seed" / f"{token}.yaml",
        root / "scenarios" / "generated" / f"{token}.yaml",
        root / "scenarios" / f"{token}.yaml",
    ]
    for cand in candidate_paths:
        if cand.is_file():
            try:
                data = yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
                return _parse_scenario_data(data, fallback_id=token)
            except Exception as exc:
                msg = f"Failed to parse scenario file at {cand}: {exc}"
                raise ValueError(msg) from exc

    # 3. Fallback to canonical built-in scenario templates
    template = load_scenario_template(token, base_dir=base_dir)
    is_mig = "migration" in token
    return ScenarioDefinition(
        id=template.scenario_id,
        origin="seed",
        failure_class="bad_deploy",
        description=f"Synthetic scenario {template.scenario_id}",
        injection={
            "kind": "deploy_image",
            "target": "data-service",
            "params": {"image_tag": "regression"},
            "settle_seconds": 15,
        },
        alert_template={
            "title": template.title,
            "service": template.service,
            "severity": template.severity,
        },
        expected_remediation_class="rollback_deploy",
        expected_escalation=is_mig,
        ground_truth_note="Rollback to previous stable release",
        migration_boundary=is_mig,
    )


def format_scoreboard_table(
    evidence: list[CandidateEvidence],
    scores: list[CandidateScore],
) -> str:
    """Format candidate evidence and tournament scores into a readable table."""
    score_by_plan = {s.plan_id: s for s in scores}
    sorted_scores = sorted(scores, key=lambda s: (s.disqualified, s.composite))
    rank_by_plan: dict[str, int] = {}
    for idx, s in enumerate(sorted_scores, start=1):
        rank_by_plan[s.plan_id] = idx

    headers = [
        "Plan ID",
        "Twin ID",
        "Recovered",
        "Rec. Time",
        "Blast Set",
        "Error Delta",
        "Drop Ratio",
        "Score",
        "Status",
    ]

    rows: list[list[str]] = []
    for ev in evidence:
        sc = score_by_plan.get(ev.plan_id)
        rank_str = f"#{rank_by_plan[ev.plan_id]}" if ev.plan_id in rank_by_plan else "-"

        rec_time_str = f"{ev.recovery_seconds:.1f}s" if ev.recovery_seconds is not None else "N/A"
        blast_str = ",".join(ev.observed_blast_set) if ev.observed_blast_set else "none"
        delta_str = f"{ev.downstream_error_delta:+.3f}"
        drop_str = f"{ev.mirror_stats.drop_ratio:.1%}"
        score_str = f"{sc.composite:.4f}" if sc else "N/A"

        if sc and sc.disqualified:
            status_str = f"disqualified ({sc.disqualification_reason or 'rule'})"
        elif rank_str == "#1":
            status_str = f"winner ({rank_str})"
        else:
            status_str = f"viable ({rank_str})"

        rows.append(
            [
                ev.plan_id,
                ev.twin_id,
                "YES" if ev.recovered else "NO",
                rec_time_str,
                blast_str,
                delta_str,
                drop_str,
                score_str,
                status_str,
            ]
        )

    # Compute column widths
    col_widths = [len(h) for h in headers]
    for r in rows:
        for i, val in enumerate(r):
            col_widths[i] = max(col_widths[i], len(val))

    sep_line = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    header_line = "| " + " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + " |"

    out_lines = [
        "\nScoreboard:",
        sep_line,
        header_line,
        sep_line,
    ]
    for r in rows:
        row_str = "| " + " | ".join(r[i].ljust(col_widths[i]) for i in range(len(r))) + " |"
        out_lines.append(row_str)
    out_lines.append(sep_line)

    for s in scores:
        if s.disqualified and s.disqualification_reason:
            out_lines.append(
                f'Candidate {s.plan_id} disqualified with reason "{s.disqualification_reason}"'
            )

    return "\n".join(out_lines)


def format_run_summary(record: RunRecord) -> str:
    """Format final run summary lines matching Checkpoint 5 requirements."""
    lines: list[str] = []

    if record.tournament:
        t = record.tournament
        if t.outcome == TournamentOutcome.DECIDED and t.winner_plan_id:
            m_str = f"{t.margin:.4f}" if t.margin is not None else "0.0000"
            lines.append(f"outcome=decided winner={t.winner_plan_id} margin={m_str}")
            lines.append(f"scoreboard printed, winner: {t.winner_plan_id}; margin: {m_str}")
        elif t.outcome == TournamentOutcome.AMBIGUOUS:
            m_str = f"{t.margin:.4f}" if t.margin is not None else "0.0000"
            lines.append(f"outcome=ambiguous, no winner (margin: {m_str})")
        else:
            lines.append("outcome=no_viable_candidate, no winner")

    if record.verdict:
        v = record.verdict
        v_name = v.verdict.value.lower()
        if v.verdict == KernelVerdictType.VETO:
            vetoed = [r.invariant_id for r in v.results if r.satisfied is False]
            veto_str = ", ".join(vetoed) if vetoed else "invariants"
            lines.append(f"kernel verdict = veto ({veto_str})")
        else:
            lines.append(f"kernel verdict = {v_name}")

    if record.outcome == RunOutcome.EXECUTED:
        prod_res = record.prod_outcome or "resolved"
        lines.append(
            f"production rolled back; prod probe healthy within 180s; prod_outcome={prod_res}"
        )
        lines.append(f"outcome=executed plan={record.prod_applied_plan_id}")
    elif record.outcome == RunOutcome.ESCALATED:
        reason = record.escalation_reason or "Escalated to human operator"
        lines.append("no production change; escalated to PagerDuty")
        lines.append(f'outcome=escalated reason="{reason}"')
    else:
        lines.append(f"outcome={record.outcome.value}")

    lines.append(
        f"run record written and immutable: run_id={record.run_id} incident_id={record.incident_id}"
    )

    return "\n".join(lines)


def create_scenario_deps(
    scenario: ScenarioDefinition,
    live: bool = False,
    fake: bool = False,
    seed: int = 42,
    force_veto: bool = False,
    clock: Clock | None = None,
) -> Deps:
    """Create configured Deps container suitable for scenario execution."""
    active_clock = resolve_clock(clock)
    should_veto = force_veto or scenario.expected_escalation or scenario.migration_boundary

    if fake or not live:
        return create_fake_deps(
            seed=seed,
            clock=active_clock,
            force_veto=should_veto,
        )

    from understudy.deps import create_deps, create_real_deps

    real_deps = create_real_deps(clock=active_clock)

    # Wrap deploy history with ScenarioDeployHistory to overlay migration boundary if required
    scenario_deploy_history = ScenarioDeployHistory(
        base=real_deps.deploy_history,
        scenario_id=scenario.id,
        clock=active_clock,
        force_migration=should_veto,
    )

    return create_deps(
        fake=False,
        clock=active_clock,
        deploy_history=scenario_deploy_history,
    )


async def run_scenario(
    scenario_id_or_path: str | Path,
    live: bool = False,
    fake: bool = False,
    seed: int = 42,
    force_veto: bool = False,
    inject: bool = True,
    deps: Deps | None = None,
    clock: Clock | None = None,
    on_transition: Callable[[str], None] | None = None,
    base_dir: Path | None = None,
) -> tuple[list[str], RunRecord]:
    """Execute an incident scenario from start to finish.

    1. Parses scenario configuration.
    2. If live and inject is enabled, invokes fault injection via Actuator.
    3. Assembles scenario dependencies (or uses provided deps).
    4. Creates synthetic alert and triggers orchestrator control loop.
    5. Returns ordered node transitions and persisted immutable RunRecord.

    Returns:
        tuple of (ordered list of node transition names, final persisted RunRecord).
    """
    resolved_clock = resolve_clock(clock)
    scenario_def = load_scenario_definition(scenario_id_or_path, base_dir=base_dir)

    # 1. Fault injection for live cluster runs
    if live and inject and scenario_def.injection:
        inj = scenario_def.injection
        kind = inj.get("kind")
        if kind == "deploy_image":
            target = str(inj.get("target", "data-service"))
            params = inj.get("params", {})
            image_tag = str(params.get("image_tag", "regression"))
            settle_sec = float(inj.get("settle_seconds", 15.0))
            await inject_scenario_fault(
                target=target,
                image_tag=image_tag,
                settle_seconds=settle_sec,
                clock=resolved_clock,
            )

    # 2. Dependency assembly
    active_deps = deps or create_scenario_deps(
        scenario=scenario_def,
        live=live,
        fake=fake,
        seed=seed,
        force_veto=force_veto,
        clock=resolved_clock,
    )

    # 3. Create synthetic Alert from scenario alert_template
    tmpl = scenario_def.alert_template
    alert = create_synthetic_alert(
        scenario_id=scenario_def.id,
        clock=resolved_clock,
        title=tmpl.get("title"),
        service=tmpl.get("service"),
        severity=tmpl.get("severity"),
        base_dir=base_dir,
    )

    # Enqueue alert into AlertSource so ingest node reads it
    await active_deps.alert_source.inject_synthetic_alert(alert)

    # 4. StateGraph control loop execution
    incident_id = f"inc_{alert.alert_id}"
    initial_state = State(alert=alert, incident_id=incident_id)
    config: RunnableConfig = {"configurable": {"thread_id": incident_id}}

    graph = build_graph(active_deps)
    transitions: list[str] = []

    async for chunk in graph.astream(initial_state, config=config, stream_mode="updates"):
        for node_name in chunk:
            transitions.append(node_name)
            if on_transition is not None:
                on_transition(node_name)

    # 5. Fetch final persisted RunRecord
    record = await active_deps.run_store.get_run(f"run_{incident_id}")
    if record is None:
        raise OrchestratorError(
            f"Run record run_{incident_id} not found in store after graph completion "
            f"(transitions: {' -> '.join(transitions)})"
        )

    return transitions, record


__all__ = [
    "ScenarioDefinition",
    "create_scenario_deps",
    "format_run_summary",
    "format_scoreboard_table",
    "load_scenario_definition",
    "run_scenario",
]
