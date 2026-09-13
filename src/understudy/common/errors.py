"""Typed error hierarchy for Understudy."""

from typing import Any


class UnderstudyError(Exception):
    """Base exception for all domain errors in Understudy."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(message={self.message!r}, details={self.details!r})"


class ConfigError(UnderstudyError):
    """Raised when configuration is missing, invalid, or inconsistent."""


class FleetError(UnderstudyError):
    """Raised on failures in twin lifecycle, cloning, or readiness."""


class KernelError(UnderstudyError):
    """Raised on invariant evaluation or solver failures in safety kernel."""


class ActuationError(UnderstudyError):
    """Raised on failure to actuate or revert a plan in production or twin."""


class EvidenceError(UnderstudyError):
    """Raised when tournament evidence fails fidelity or observation checks."""


class OrchestratorError(UnderstudyError):
    """Raised on orchestrator state transition or control loop failure."""


class StoreError(UnderstudyError):
    """Raised on persistence, query, or constraint failures in the store."""
