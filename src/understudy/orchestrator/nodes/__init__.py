"""Orchestrator control loop nodes implementing architecture §2.2 and §2.9."""

from collections.abc import Callable, Coroutine
from typing import Any

from understudy.orchestrator.api import Deps
from understudy.orchestrator.nodes.actuate import actuate
from understudy.orchestrator.nodes.apply_candidates import apply_candidates
from understudy.orchestrator.nodes.escalate_pagerduty import escalate_pagerduty
from understudy.orchestrator.nodes.fork_fleet import fork_fleet
from understudy.orchestrator.nodes.gather_context import gather_context
from understudy.orchestrator.nodes.handle_failure import handle_failure
from understudy.orchestrator.nodes.ingest import ingest
from understudy.orchestrator.nodes.notify_slack import notify_slack
from understudy.orchestrator.nodes.observe import observe
from understudy.orchestrator.nodes.plan_candidates import plan_candidates
from understudy.orchestrator.nodes.record_run import record_run
from understudy.orchestrator.nodes.register_mirrors import register_mirrors
from understudy.orchestrator.nodes.safety_kernel import safety_kernel
from understudy.orchestrator.nodes.teardown_fleet import teardown_fleet
from understudy.orchestrator.nodes.tournament import tournament
from understudy.orchestrator.state import State

NodeFunc = Callable[[State, Deps], Coroutine[Any, Any, dict[str, Any]]]

ALL_NODES: dict[str, NodeFunc] = {
    "ingest": ingest,
    "gather_context": gather_context,
    "plan_candidates": plan_candidates,
    "fork_fleet": fork_fleet,
    "register_mirrors": register_mirrors,
    "apply_candidates": apply_candidates,
    "observe": observe,
    "tournament": tournament,
    "safety_kernel": safety_kernel,
    "actuate": actuate,
    "notify_slack": notify_slack,
    "escalate_pagerduty": escalate_pagerduty,
    "teardown_fleet": teardown_fleet,
    "record_run": record_run,
    "handle_failure": handle_failure,
}

__all__ = [
    "ALL_NODES",
    "NodeFunc",
    "actuate",
    "apply_candidates",
    "escalate_pagerduty",
    "fork_fleet",
    "gather_context",
    "handle_failure",
    "ingest",
    "notify_slack",
    "observe",
    "plan_candidates",
    "record_run",
    "register_mirrors",
    "safety_kernel",
    "teardown_fleet",
    "tournament",
]
