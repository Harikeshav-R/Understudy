"""Unit tests for orchestrator timeouts and whole-incident watchdog.

Implements build-plan step 5.6 and architecture §2.9:
- Tests resolve_node_timeout for default mapping and custom overrides.
- Tests with_node_timeout for success, zero/None timeout, and timeout expiry.
- Tests wrap_nodes_with_timeouts.
- Tests StateTracker on_node_enter updates.
- Tests execute_emergency_escalation with mirror/fleet cleanup and PagerDuty.
- Tests IncidentWatchdog normal completion, timeout trigger, context manager, and resilience.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from understudy.common.config import TimeoutSettings
from understudy.common.errors import (
    IncidentTimeoutError,
    NodeTimeoutError,
    OrchestratorTimeoutError,
)
from understudy.contracts.enums import RunOutcome
from understudy.contracts.incident import (
    Alert,
    DependencyGraphSnapshot,
    IncidentContext,
    MetricWindow,
)
from understudy.contracts.twin import TwinHandle
from understudy.notify.fakes import FakeNotifier
from understudy.orchestrator.fakes import create_fake_deps
from understudy.orchestrator.state import State
from understudy.orchestrator.timeouts import (
    IncidentWatchdog,
    StateTracker,
    execute_emergency_escalation,
    resolve_node_timeout,
    with_node_timeout,
    wrap_nodes_with_timeouts,
)

if TYPE_CHECKING:
    from understudy.orchestrator.api import Deps


def _sample_context(incident_id: str = "inc_timeout_test") -> IncidentContext:
    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    alert = Alert(
        alert_id="alt_timeout",
        source="synthetic",
        title="High error rate",
        service="edge-gateway",
        severity="critical",
        fired_at=now,
    )
    return IncidentContext(
        incident_id=incident_id,
        alert=alert,
        signatures=[],
        metrics_window=MetricWindow(service=alert.service, start_time=now, end_time=now),
        recent_deploys=[],
        dependency_graph=DependencyGraphSnapshot(nodes=[alert.service], edges=[], observed_at=now),
        gathered_at=now,
    )


def _sample_twin(twin_id: str = "twin_0", incident_id: str = "inc_test") -> TwinHandle:
    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    return TwinHandle(
        twin_id=twin_id,
        incident_id=incident_id,
        candidate_index=0,
        namespace=f"ust-twin-{twin_id}",
        database=f"twin_{twin_id}",
        forked_from_snapshot_at=now,
        ready_at=now,
        state="ready",
    )


def test_resolve_node_timeout_defaults() -> None:
    """Verify resolve_node_timeout extracts §2.9 defaults correctly."""
    timeouts = TimeoutSettings(
        fork_seconds=120,
        candidate_apply_seconds=60,
        observation_seconds=200,
        kernel_seconds=5,
        incident_seconds=600,
    )

    assert resolve_node_timeout("fork_fleet", timeouts) == 120.0
    assert resolve_node_timeout("apply_candidates", timeouts) == 60.0
    assert resolve_node_timeout("observe", timeouts) == 200.0
    assert resolve_node_timeout("safety_kernel", timeouts) == 5.0
    assert resolve_node_timeout("ingest", timeouts) is None
    assert resolve_node_timeout("actuate", timeouts) is None


def test_resolve_node_timeout_custom_overrides() -> None:
    """Verify custom overrides take precedence over TimeoutSettings."""
    timeouts = TimeoutSettings(fork_seconds=120)
    custom = {"fork_fleet": 15.5, "ingest": 10.0}

    assert resolve_node_timeout("fork_fleet", timeouts, custom_timeouts=custom) == 15.5
    assert resolve_node_timeout("ingest", timeouts, custom_timeouts=custom) == 10.0
    assert resolve_node_timeout("safety_kernel", timeouts, custom_timeouts=custom) == 5.0
    assert resolve_node_timeout("unknown_node", timeouts, custom_timeouts=custom) is None


def test_resolve_node_timeout_none_timeouts() -> None:
    """Verify resolve_node_timeout handles None timeouts cleanly."""
    assert resolve_node_timeout("fork_fleet", None) is None
    assert resolve_node_timeout("fork_fleet", None, custom_timeouts={"fork_fleet": 2.0}) == 2.0


@pytest.mark.asyncio
async def test_with_node_timeout_success() -> None:
    """Verify with_node_timeout executes fast node successfully."""
    deps = create_fake_deps()
    state = State(incident_id="inc_fast")

    async def fast_node(s: State, d: Deps) -> dict[str, Any]:
        _ = (s, d)
        return {"errors": []}

    wrapped = with_node_timeout(fast_node, "fast_node", 1.0)
    res = await wrapped(state, deps)
    assert res == {"errors": []}


@pytest.mark.asyncio
async def test_with_node_timeout_no_timeout() -> None:
    """Verify with_node_timeout returns original function when timeout is None or <= 0."""

    async def sample_node(s: State, d: Deps) -> dict[str, Any]:
        _ = (s, d)
        return {"outcome": RunOutcome.EXECUTED}

    assert with_node_timeout(sample_node, "sample", None) is sample_node
    assert with_node_timeout(sample_node, "sample", 0) is sample_node
    assert with_node_timeout(sample_node, "sample", -5.0) is sample_node


@pytest.mark.asyncio
async def test_with_node_timeout_exceeded() -> None:
    """Verify with_node_timeout raises typed NodeTimeoutError when duration is exceeded."""
    deps = create_fake_deps()
    state = State(incident_id="inc_slow")

    async def slow_node(s: State, d: Deps) -> dict[str, Any]:
        _ = (s, d)
        await asyncio.sleep(0.2)
        return {"outcome": RunOutcome.EXECUTED}

    wrapped = with_node_timeout(slow_node, "slow_node", 0.02)
    with pytest.raises(NodeTimeoutError) as exc_info:
        await wrapped(state, deps)

    err = exc_info.value
    assert err.node_name == "slow_node"
    assert err.timeout_seconds == 0.02
    assert "timed out after 0.02s" in err.message
    assert err.details["node_name"] == "slow_node"
    assert err.details["timeout_seconds"] == 0.02
    assert isinstance(err, OrchestratorTimeoutError)


def test_wrap_nodes_with_timeouts() -> None:
    """Verify wrap_nodes_with_timeouts wraps all nodes according to configuration."""
    timeouts = TimeoutSettings(fork_seconds=10)

    async def dummy(s: State, d: Deps) -> dict[str, Any]:
        _ = (s, d)
        return {}

    nodes = {"fork_fleet": dummy, "ingest": dummy}
    wrapped = wrap_nodes_with_timeouts(nodes, timeouts, custom_timeouts={"ingest": 5.0})

    assert "fork_fleet" in wrapped
    assert "ingest" in wrapped
    assert wrapped["fork_fleet"] is not dummy
    assert wrapped["ingest"] is not dummy


def test_state_tracker() -> None:
    """Verify StateTracker accurately reflects the latest State on each node enter."""
    initial = State(incident_id="inc_track")
    tracker = StateTracker(initial)
    assert tracker.current_state.incident_id == "inc_track"

    updated = State(incident_id="inc_track", errors=["err1"])
    tracker.on_node_enter("gather_context", updated)
    assert tracker.current_state.errors == ["err1"]


@pytest.mark.asyncio
async def test_execute_emergency_escalation_full() -> None:
    """Verify emergency escalation tears down twins, escalates PagerDuty, and records run."""
    deps = create_fake_deps()
    ctx = _sample_context("inc_emerg")
    twin = _sample_twin("twin_0", incident_id="inc_emerg")
    state = State(
        incident_id="inc_emerg",
        context=ctx,
        twins=[twin],
    )

    record = await execute_emergency_escalation(
        state=state,
        deps=deps,
        reason="Test emergency timeout",
    )

    assert record is not None
    assert record.outcome == RunOutcome.ESCALATED
    assert record.escalation_reason == "Test emergency timeout"

    notifier = deps.notifier
    assert isinstance(notifier, FakeNotifier)
    assert len(notifier.pagerduty_escalations) == 1
    assert notifier.pagerduty_escalations[0]["reason"] == "Test emergency timeout"
    assert notifier.pagerduty_escalations[0]["urgency"] == "high"

    # Verify run recorded in store
    persisted = await deps.run_store.get_run("run_inc_emerg")
    assert persisted is not None
    assert persisted.outcome == RunOutcome.ESCALATED


@pytest.mark.asyncio
async def test_execute_emergency_escalation_missing_context() -> None:
    """Verify emergency escalation without context does not persist run record."""
    deps = create_fake_deps()
    state = State(incident_id="inc_no_ctx")

    record = await execute_emergency_escalation(
        state=state,
        deps=deps,
        reason="Early crash timeout",
    )
    assert record is None

    notifier = deps.notifier
    assert isinstance(notifier, FakeNotifier)
    assert len(notifier.pagerduty_escalations) == 1


@pytest.mark.asyncio
async def test_execute_emergency_escalation_resilience() -> None:
    """Verify cleanup errors during emergency escalation are caught and logged."""
    deps = create_fake_deps()
    twin = _sample_twin("twin_bad", incident_id="inc_broken")
    state = State(
        incident_id="inc_broken",
        context=_sample_context("inc_broken"),
        twins=[twin],
    )

    # Make unregister_twin, teardown_all, and escalate_pagerduty raise
    deps.mirror_registry.unregister_twin = AsyncMock(side_effect=RuntimeError("mirror failure"))  # type: ignore[method-assign]
    deps.fleet_controller.teardown_all = AsyncMock(side_effect=RuntimeError("fleet failure"))  # type: ignore[method-assign]
    deps.notifier.escalate_pagerduty = AsyncMock(side_effect=RuntimeError("notify failure"))  # type: ignore[method-assign]

    # Should still succeed in writing the run record
    record = await execute_emergency_escalation(
        state=state,
        deps=deps,
        reason="Resilience test",
    )
    assert record is not None
    assert record.outcome == RunOutcome.ESCALATED

    # Verify run store error resilience
    deps.run_store.record_run = AsyncMock(side_effect=RuntimeError("store failure"))  # type: ignore[method-assign]
    record2 = await execute_emergency_escalation(
        state=state,
        deps=deps,
        reason="Store fail test",
    )
    assert record2 is not None


@pytest.mark.asyncio
async def test_incident_watchdog_normal_completion() -> None:
    """Verify IncidentWatchdog disarms cleanly when task completes before timeout."""
    deps = create_fake_deps()
    watchdog = IncidentWatchdog(
        incident_id="inc_normal",
        timeout_seconds=0.5,
        deps=deps,
    )

    async def fast_task() -> str:
        await asyncio.sleep(0.01)
        return "completed"

    res = await watchdog.run(fast_task())
    assert res == "completed"
    assert watchdog.fired is False
    assert watchdog.escalated_record is None


@pytest.mark.asyncio
async def test_incident_watchdog_timeout_fires() -> None:
    """Verify IncidentWatchdog fires, cancels task, tears down, and raises IncidentTimeoutError."""
    deps = create_fake_deps()
    ctx = _sample_context("inc_dog_to")
    state = State(incident_id="inc_dog_to", context=ctx)

    watchdog = IncidentWatchdog(
        incident_id="inc_dog_to",
        timeout_seconds=0.02,
        deps=deps,
        get_current_state=lambda: state,
        raise_on_timeout=True,
    )

    async def hanging_task() -> str:
        await asyncio.sleep(1.0)
        return "unreachable"

    with pytest.raises(IncidentTimeoutError) as exc_info:
        await watchdog.run(hanging_task())

    assert watchdog.fired is True
    assert exc_info.value.timeout_seconds == 0.02
    assert "Incident watchdog timeout exceeded" in exc_info.value.message
    assert watchdog.escalated_record is not None
    assert watchdog.escalated_record.outcome == RunOutcome.ESCALATED

    # PagerDuty was notified
    notifier = deps.notifier
    assert isinstance(notifier, FakeNotifier)
    assert len(notifier.pagerduty_escalations) == 1


@pytest.mark.asyncio
async def test_incident_watchdog_zero_timeout() -> None:
    """Verify IncidentWatchdog runs coroutine directly when timeout <= 0."""
    deps = create_fake_deps()
    watchdog = IncidentWatchdog(
        incident_id="inc_zero",
        timeout_seconds=0.0,
        deps=deps,
    )

    async def task() -> int:
        return 42

    assert await watchdog.run(task()) == 42
    assert watchdog.fired is False


@pytest.mark.asyncio
async def test_incident_watchdog_no_raise_on_timeout() -> None:
    """Verify IncidentWatchdog does not raise when raise_on_timeout=False upon firing."""
    deps = create_fake_deps()
    watchdog = IncidentWatchdog(
        incident_id="inc_no_raise",
        timeout_seconds=0.02,
        deps=deps,
        raise_on_timeout=False,
    )

    async def hanging_task() -> None:
        await asyncio.sleep(1.0)

    # When raise_on_timeout is False, the inner CancelledError is re-raised
    with pytest.raises(asyncio.CancelledError):
        await watchdog.run(hanging_task())

    assert watchdog.fired is True


@pytest.mark.asyncio
async def test_incident_watchdog_context_manager_normal() -> None:
    """Verify IncidentWatchdog functions as an async context manager in normal path."""
    deps = create_fake_deps()
    watchdog = IncidentWatchdog(
        incident_id="inc_cm_norm",
        timeout_seconds=0.5,
        deps=deps,
    )

    async with watchdog as w:
        assert w is watchdog
        await asyncio.sleep(0.01)

    assert watchdog.fired is False


@pytest.mark.asyncio
async def test_incident_watchdog_context_manager_timeout() -> None:
    """Verify IncidentWatchdog context manager raises IncidentTimeoutError on timeout."""
    deps = create_fake_deps()
    watchdog = IncidentWatchdog(
        incident_id="inc_cm_to",
        timeout_seconds=0.02,
        deps=deps,
        raise_on_timeout=True,
    )

    with pytest.raises(IncidentTimeoutError):
        async with watchdog:
            await asyncio.sleep(1.0)

    assert watchdog.fired is True


@pytest.mark.asyncio
async def test_incident_watchdog_context_manager_no_task_raises() -> None:
    """Verify IncidentWatchdog context manager raises RuntimeError if current_task is None."""
    deps = create_fake_deps()
    watchdog = IncidentWatchdog(incident_id="inc_no_task", timeout_seconds=1.0, deps=deps)

    with (
        patch("asyncio.current_task", return_value=None),
        pytest.raises(RuntimeError, match="must be called from an async task"),
    ):
        async with watchdog:
            pass


@pytest.mark.asyncio
async def test_execute_emergency_escalation_empty_incident_id() -> None:
    """Verify emergency escalation without incident_id skips fleet teardown."""
    deps = create_fake_deps()
    mock_teardown = AsyncMock()
    deps.fleet_controller.teardown_all = mock_teardown  # type: ignore[method-assign]
    state = State(incident_id="")
    record = await execute_emergency_escalation(
        state=state,
        deps=deps,
        reason="No incident ID",
    )
    assert record is None
    mock_teardown.assert_not_called()


@pytest.mark.asyncio
async def test_incident_watchdog_loop_target_task_already_done() -> None:
    """Verify _watchdog_loop handles target_task already done or None."""
    deps = create_fake_deps()
    watchdog = IncidentWatchdog(
        incident_id="inc_done",
        timeout_seconds=0.01,
        deps=deps,
    )
    watchdog._target_task = None
    await watchdog._watchdog_loop()
    assert watchdog.fired is True

    async def finished() -> None:
        pass

    task = asyncio.create_task(finished())
    await task
    watchdog2 = IncidentWatchdog(
        incident_id="inc_done2",
        timeout_seconds=0.01,
        deps=deps,
    )
    watchdog2._target_task = task
    await watchdog2._watchdog_loop()
    assert watchdog2.fired is True


@pytest.mark.asyncio
async def test_incident_watchdog_run_external_cancellation() -> None:
    """Verify run re-raises CancelledError when watchdog did not fire."""
    deps = create_fake_deps()
    watchdog = IncidentWatchdog(
        incident_id="inc_ext_cancel",
        timeout_seconds=5.0,
        deps=deps,
    )

    async def self_cancelling_task() -> None:
        raise asyncio.CancelledError("External cancellation")

    with pytest.raises(asyncio.CancelledError):
        await watchdog.run(self_cancelling_task())

    assert watchdog.fired is False


@pytest.mark.asyncio
async def test_incident_watchdog_context_manager_zero_timeout() -> None:
    """Verify IncidentWatchdog context manager does not launch loop when timeout <= 0."""
    deps = create_fake_deps()
    watchdog = IncidentWatchdog(
        incident_id="inc_cm_zero",
        timeout_seconds=0.0,
        deps=deps,
    )

    async with watchdog:
        pass

    assert watchdog.fired is False
    assert watchdog._watchdog_task is None


@pytest.mark.asyncio
async def test_incident_watchdog_context_manager_suppress_timeout() -> None:
    """Verify context manager suppresses CancelledError when raise_on_timeout=False."""
    deps = create_fake_deps()
    watchdog = IncidentWatchdog(
        incident_id="inc_cm_suppress",
        timeout_seconds=0.02,
        deps=deps,
        raise_on_timeout=False,
    )

    async with watchdog:
        await asyncio.sleep(1.0)

    assert watchdog.fired is True
