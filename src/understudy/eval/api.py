"""Evaluation harness component protocol interfaces."""

from typing import Any, Protocol, runtime_checkable

from understudy.contracts.run import RunRecord


@runtime_checkable
class EvalHarness(Protocol):
    """Corpus runner, metric calculator, and benchmark evaluator."""

    async def run_scenario(self, scenario_id: str) -> RunRecord:
        """Execute a single scenario against the system."""
        raise NotImplementedError

    async def run_corpus(self) -> dict[str, Any]:
        """Execute the full evaluation corpus and return aggregated metrics."""
        raise NotImplementedError
