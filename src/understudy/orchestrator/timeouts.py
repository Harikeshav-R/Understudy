"""Node-level timeout wrappers and whole-incident watchdog for Understudy orchestrator.

Implements build-plan step 5.6 and architecture §2.9:
- Enforce §2.9 timeouts as node-level wrappers:
  - fork: 120s (fork_fleet)
  - candidate apply: 60s (apply_candidates)
  - observation: 200s (observe)
  - kernel: 5s (safety_kernel)
- Individual node timeout raises typed NodeTimeoutError, diverting to handle_failure.
- Whole-incident watchdog task runs concurrently, monitoring incident_seconds (default 600s).
- Exceeding the incident timeout is an escalation, never a best-effort action:
  - cancels the running graph execution,
  - tears down twins and unregisters mirrors,
  - escalates to PagerDuty with the watchdog reason and partial evidence,
  - writes an ESCALATED run record.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, TypeVar

from understudy.common.errors import (
    IncidentTimeoutError,
    NodeTimeoutError,
    OrchestratorTimeoutError,
)
from understudy.common.logging import get_logger
from understudy.contracts.enums import RunOutcome
from understudy.orchestrator.cleanup import cleanup_incident_twins
from understudy.orchestrator.state import State

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from understudy.common.config import TimeoutSettings
    from understudy.contracts.run import RunRecord
    from understudy.orchestrator.api import Deps
    from understudy.orchestrator.nodes import NodeFunc

T = TypeVar("T")

# Canonical mapping of §2.9 node-level timeouts to TimeoutSettings fields
DEFAULT_NODE_TIMEOUT_MAP: dict[str, str] = {
    "fork_fleet": "fork_seconds",
    "apply_candidates": "candidate_apply_seconds",
    "observe": "observation_seconds",
    "safety_kernel": "kernel_seconds",
}


def resolve_node_timeout(
    node_name: str,
    timeouts: TimeoutSettings | None,
    custom_timeouts: dict[str, float] | None = None,
) -> float | None:
    """Resolve the allocated timeout in seconds for an individual graph node.

    Args:
        node_name: Name of the node in ALL_NODES.
        timeouts: Configured TimeoutSettings instance, if any.
        custom_timeouts: Optional per-node override dictionary in seconds.

    Returns:
        Timeout in seconds as a float, or None if no timeout applies to this node.
    """
    if custom_timeouts is not None and node_name in custom_timeouts:
        return float(custom_timeouts[node_name])

    if timeouts is not None and node_name in DEFAULT_NODE_TIMEOUT_MAP:
        field_name = DEFAULT_NODE_TIMEOUT_MAP[node_name]
        return float(getattr(timeouts, field_name))

    return None


def with_node_timeout(
    fn: NodeFunc,
    node_name: str,
    timeout_seconds: float | None,
) -> NodeFunc:
    """Wrap a node function with an asyncio timeout barrier.

    Args:
        fn: Original async node function (state, deps) -> dict.
        node_name: Identifying name of the node for error reporting and metrics.
        timeout_seconds: Timeout limit in seconds. If None or <= 0, no timeout is enforced.

    Returns:
        Wrapped NodeFunc enforcing the timeout.
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        return fn

    async def _timeout_wrapped(state: State, deps: Deps) -> dict[str, Any]:
        try:
            async with asyncio.timeout(timeout_seconds):
                return await fn(state, deps)
        except TimeoutError as exc:
            raise NodeTimeoutError(
                node_name=node_name,
                timeout_seconds=timeout_seconds,
            ) from exc

    return _timeout_wrapped


def wrap_nodes_with_timeouts(
    nodes: Mapping[str, NodeFunc],
    timeouts: TimeoutSettings | None,
    custom_timeouts: dict[str, float] | None = None,
) -> dict[str, NodeFunc]:
    """Wrap an entire mapping of nodes with their resolved §2.9 timeouts."""
    wrapped: dict[str, NodeFunc] = {}
    for name, fn in nodes.items():
        node_timeout = resolve_node_timeout(name, timeouts, custom_timeouts=custom_timeouts)
        wrapped[name] = with_node_timeout(fn, name, node_timeout)
    return wrapped


class StateTracker:
    """Tracks the latest State during graph execution across node transitions."""

    def __init__(self, initial_state: State) -> None:
        self.current_state: State = initial_state

    def on_node_enter(self, node_name: str, state: State) -> None:
        """Capture the current state as a node begins execution."""
        _ = node_name
        self.current_state = state


async def execute_emergency_escalation(
    state: State,
    deps: Deps,
    reason: str,
) -> RunRecord | None:
    """Clean up twins best-effort, escalate failure to PagerDuty, and persist ESCALATED run.

    Enforces architecture §2.9:
    Exceeding the incident timeout is an escalation, never a best-effort action.
    """
    logger = get_logger(incident_id=state.incident_id or "unknown")
    now = deps.clock.now()

    # 1. Best-effort mirror unregistration and fleet teardown
    await cleanup_incident_twins(state.twins, state.incident_id, deps)

    # 2. Escalate to PagerDuty
    try:
        await deps.notifier.escalate_pagerduty(
            incident_id=state.incident_id or "unknown",
            reason=reason,
            partial_evidence=state.evidence,
            context=state.context,
            plans=state.plans,
            verdict=state.verdict,
            tournament=state.tournament,
            urgency="high",
        )
    except Exception as exc:
        # Notification failure must not prevent run record persistence
        logger.error("watchdog_escalate_pagerduty_failed", error=str(exc))

    # 4. Write run record if context exists
    if state.context is not None:
        updates: dict[str, Any] = {
            "outcome": RunOutcome.ESCALATED,
            "finished_at": now,
            "escalation_reason": reason,
            "escalated": True,
        }
        escalated_state = state.model_copy(update=updates)
        record = escalated_state.to_run_record(now=now)
        try:
            await deps.run_store.record_run(record)
            return record
        except Exception as exc:
            logger.error("watchdog_record_run_failed", error=str(exc))
            return record

    return None


class IncidentWatchdog:
    """Watchdog task monitoring whole-incident execution against incident timeout.

    Enforces architecture §2.9 and build-plan step 5.6:
    Runs concurrently as an asyncio background task. If incident_seconds is reached,
    cancels the target execution, triggers emergency teardown & escalation, and
    raises IncidentTimeoutError.
    """

    def __init__(
        self,
        incident_id: str,
        timeout_seconds: float,
        deps: Deps,
        get_current_state: Callable[[], State] | None = None,
        raise_on_timeout: bool = True,
    ) -> None:
        self.incident_id = incident_id
        self.timeout_seconds = timeout_seconds
        self.deps = deps
        self.get_current_state = get_current_state
        self.raise_on_timeout = raise_on_timeout
        self._watchdog_task: asyncio.Task[None] | None = None
        self._target_task: asyncio.Task[Any] | None = None
        self._fired: bool = False
        self._escalated_record: RunRecord | None = None

    @property
    def fired(self) -> bool:
        """Return True if the watchdog timeout fired."""
        return self._fired

    @property
    def escalated_record(self) -> RunRecord | None:
        """Return the escalated RunRecord generated upon timeout, if available."""
        return self._escalated_record

    async def _watchdog_loop(self) -> None:
        """Asynchronous watchdog loop sleeping until the timeout expires."""
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(self.timeout_seconds)
            self._fired = True
            logger = get_logger(incident_id=self.incident_id)
            logger.error(
                "incident_watchdog_timeout_exceeded",
                incident_id=self.incident_id,
                timeout_seconds=self.timeout_seconds,
            )
            if self._target_task is not None and not self._target_task.done():
                self._target_task.cancel()

    async def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run the given coroutine supervised by the watchdog background task."""
        if self.timeout_seconds <= 0:
            return await coro

        target_task: asyncio.Task[T] = asyncio.create_task(coro)
        self._target_task = target_task
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

        try:
            return await target_task
        except asyncio.CancelledError as exc:
            if self._fired:
                state = (
                    self.get_current_state()
                    if self.get_current_state is not None
                    else State(incident_id=self.incident_id)
                )
                reason = f"Incident watchdog timeout exceeded after {self.timeout_seconds}s"
                self._escalated_record = await execute_emergency_escalation(
                    state=state,
                    deps=self.deps,
                    reason=reason,
                )
                if self.raise_on_timeout:
                    raise IncidentTimeoutError(
                        timeout_seconds=self.timeout_seconds,
                        message=reason,
                    ) from exc
            raise
        finally:
            if self._watchdog_task is not None and not self._watchdog_task.done():
                self._watchdog_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._watchdog_task

    async def __aenter__(self) -> IncidentWatchdog:
        current_task = asyncio.current_task()
        if current_task is None:
            raise OrchestratorTimeoutError(
                "IncidentWatchdog context manager must be called from an async task"
            )
        self._target_task = current_task
        if self.timeout_seconds > 0:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task

        if exc_type is asyncio.CancelledError and self._fired:
            state = (
                self.get_current_state()
                if self.get_current_state is not None
                else State(incident_id=self.incident_id)
            )
            reason = f"Incident watchdog timeout exceeded after {self.timeout_seconds}s"
            self._escalated_record = await execute_emergency_escalation(
                state=state,
                deps=self.deps,
                reason=reason,
            )
            if self.raise_on_timeout:
                raise IncidentTimeoutError(
                    timeout_seconds=self.timeout_seconds,
                    message=reason,
                ) from exc_val
            return True

        return False


__all__ = [
    "DEFAULT_NODE_TIMEOUT_MAP",
    "IncidentTimeoutError",
    "IncidentWatchdog",
    "NodeTimeoutError",
    "OrchestratorTimeoutError",
    "StateTracker",
    "execute_emergency_escalation",
    "resolve_node_timeout",
    "with_node_timeout",
    "wrap_nodes_with_timeouts",
]
