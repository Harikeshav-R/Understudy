import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest

from understudy.common.clock import FrozenClock
from understudy.common.errors import OrchestratorError
from understudy.contracts.enums import KernelVerdictType, RunOutcome
from understudy.contracts.incident import Alert
from understudy.contracts.run import RunRecord
from understudy.kernel.fakes import FakeSafetyKernel
from understudy.orchestrator.api import (
    render_graph_mermaid as api_render_graph_mermaid,
)
from understudy.orchestrator.api import (
    render_graph_png as api_render_graph_png,
)
from understudy.orchestrator.api import (
    run_demo as api_run_demo,
)
from understudy.orchestrator.demo import (
    render_graph_mermaid,
    render_graph_png,
    run_demo,
)
from understudy.orchestrator.fakes import FakeOrchestrator, create_fake_deps
from understudy.store.fakes import FakeRunStore


class _MissingRunStore(FakeRunStore):
    """Test fake that returns None for get_run to simulate missing record in store."""

    async def get_run(self, _run_id: str) -> RunRecord | None:
        return None


@pytest.mark.asyncio
async def test_run_demo_standard() -> None:
    """Verify demo runs entire control loop with executed outcome on standard path."""
    transitions, record = await run_demo(seed=42, force_veto=False)
    expected_order = [
        "ingest",
        "gather_context",
        "plan_candidates",
        "fork_fleet",
        "register_mirrors",
        "apply_candidates",
        "observe",
        "tournament",
        "safety_kernel",
        "actuate",
        "notify_slack",
        "teardown_fleet",
        "record_run",
    ]
    assert transitions == expected_order
    assert record.outcome == RunOutcome.EXECUTED
    assert record.prod_applied_plan_id == "plan_cand_0"


@pytest.mark.asyncio
async def test_run_demo_force_veto() -> None:
    """Verify demo branches to escalate_pagerduty when force_veto=True."""
    transitions, record = await run_demo(seed=42, force_veto=True)
    expected_order = [
        "ingest",
        "gather_context",
        "plan_candidates",
        "fork_fleet",
        "register_mirrors",
        "apply_candidates",
        "observe",
        "tournament",
        "safety_kernel",
        "escalate_pagerduty",
        "teardown_fleet",
        "record_run",
    ]
    assert transitions == expected_order
    assert record.outcome == RunOutcome.ESCALATED
    assert record.verdict is not None
    assert record.verdict.verdict == KernelVerdictType.VETO


@pytest.mark.asyncio
async def test_run_demo_on_transition_callback() -> None:
    """Verify on_transition callback is called for each node executed."""
    observed: list[str] = []
    transitions, _ = await run_demo(
        seed=42,
        force_veto=False,
        on_transition=observed.append,
    )
    assert observed == transitions


@pytest.mark.asyncio
async def test_run_demo_missing_record_raises() -> None:
    """Verify OrchestratorError is raised if run record is missing from store."""
    deps = dataclasses.replace(create_fake_deps(), run_store=_MissingRunStore())
    with pytest.raises(OrchestratorError, match="not found in store"):
        await run_demo(deps=deps)


def test_render_graph_mermaid() -> None:
    """Verify render_graph_mermaid returns valid Mermaid flowchart string."""
    mermaid = render_graph_mermaid()
    assert "graph TD;" in mermaid
    assert "ingest" in mermaid
    assert "record_run" in mermaid


def test_render_graph_png(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Verify render_graph_png draws PNG bytes and writes to disk when output_path is set."""
    fake_png = b"\x89PNG\r\n\x1a\nfake-bytes"

    # Avoid real external HTTP request in unit tests per AGENTS.md §6.3
    monkeypatch.setattr(
        "langchain_core.runnables.graph.Graph.draw_mermaid_png",
        lambda *_a, **_kw: fake_png,
    )

    # 1. Without output path
    res = render_graph_png(output_path=None)
    assert res == fake_png

    # 2. With output path in non-existent directory
    target = tmp_path / "sub" / "graph.png"
    res2 = render_graph_png(output_path=target)
    assert res2 == fake_png
    assert target.is_file()
    assert target.read_bytes() == fake_png


@pytest.mark.asyncio
async def test_api_reexports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify orchestrator api exposes run_demo and render helpers."""
    fake_png = b"\x89PNG\r\n\x1a\napi-bytes"
    monkeypatch.setattr(
        "langchain_core.runnables.graph.Graph.draw_mermaid_png",
        lambda *_a, **_kw: fake_png,
    )

    mermaid = api_render_graph_mermaid()
    assert "graph TD;" in mermaid

    png = api_render_graph_png()
    assert png == fake_png

    transitions, record = await api_run_demo(seed=42)
    assert len(transitions) == 13
    assert record.outcome == RunOutcome.EXECUTED


@pytest.mark.asyncio
async def test_fake_deps_and_orchestrator_force_veto() -> None:
    """Verify create_fake_deps and FakeOrchestrator propagate force_veto."""
    deps = create_fake_deps(force_veto=True)
    assert isinstance(deps.safety_kernel, FakeSafetyKernel)
    assert deps.safety_kernel.force_verdict == KernelVerdictType.VETO

    orch = FakeOrchestrator(force_veto=True)
    assert isinstance(orch.deps.safety_kernel, FakeSafetyKernel)
    assert orch.deps.safety_kernel.force_verdict == KernelVerdictType.VETO

    alert = Alert(
        alert_id="fake_alert_01",
        source="synthetic",
        title="Test alert",
        service="edge-gateway",
        severity="error",
        fired_at=orch.clock.now(),
    )
    rec = await orch.run_incident(alert)
    assert rec.outcome == RunOutcome.EXECUTED


@pytest.mark.asyncio
async def test_run_demo_custom_clock() -> None:
    """Verify run_demo respects injected custom clock for deterministic alert creation."""
    moment = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    frozen = FrozenClock(moment)
    transitions, record = await run_demo(seed=42, clock=frozen)
    assert len(transitions) == 13
    assert record.outcome == RunOutcome.EXECUTED

    # Every timestamp the loop stamps comes from the injected clock, not wall time,
    # so an eval-harness replay of the same seed is byte-identical.
    assert record.context.alert.fired_at == moment
    assert record.context.gathered_at == moment
    assert record.started_at == moment
    assert record.finished_at == moment
