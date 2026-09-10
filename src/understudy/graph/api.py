"""Graph component protocol interfaces."""

from typing import Protocol, runtime_checkable

from understudy.contracts.incident import DependencyGraphSnapshot


@runtime_checkable
class DependencyGraph(Protocol):
    """Service dependency topology graph."""

    def dependents(self, service: str) -> set[str]:
        """Return the immediate dependents of a service."""
        raise NotImplementedError

    def reachable_set(self, service: str) -> set[str]:
        """Return the transitive closure of dependents of a service."""
        raise NotImplementedError

    def request_share(self, service: str) -> float:
        """Return a service's share of total traffic in the baseline window."""
        raise NotImplementedError

    def snapshot(self) -> DependencyGraphSnapshot:
        """Export an immutable snapshot of the graph topology."""
        raise NotImplementedError


@runtime_checkable
class BlastRadiusCalculator(Protocol):
    """Calculator for remediation blast radius across service dependencies."""

    def calculate(self, target_service: str, affected_services: set[str]) -> float:
        """Calculate normalized blast radius score [0, 1]."""
        raise NotImplementedError
