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


class MissingFact(KernelError):
    """Raised when a required fact is missing in the safety kernel context."""

    def __init__(self, fact_name: str | list[str], message: str | None = None) -> None:
        if isinstance(fact_name, list):
            self.missing_facts = list(fact_name)
            name_str = ", ".join(fact_name)
        else:
            self.missing_facts = [fact_name]
            name_str = fact_name
        self.fact_name = self.missing_facts[0] if self.missing_facts else ""
        msg = message or f"Missing required fact: {name_str}"
        super().__init__(
            msg,
            details={"fact_name": self.fact_name, "missing_facts": self.missing_facts},
        )


class ActuationError(UnderstudyError):
    """Raised on failure to actuate or revert a plan in production or twin."""


class EvidenceError(UnderstudyError):
    """Raised when tournament evidence fails fidelity or observation checks."""


class OrchestratorError(UnderstudyError):
    """Raised on orchestrator state transition or control loop failure."""


class StoreError(UnderstudyError):
    """Raised on persistence, query, or constraint failures in the store."""


class SignalsError(UnderstudyError):
    """Raised on failure to fetch telemetry, ingest signals, or communicate with signal sources."""


class ObservabilityError(SignalsError):
    """Raised on failure to query metrics or logs from Prometheus or Loki."""


class DatadogError(ObservabilityError):
    """Raised on failure to communicate with or query Datadog API."""


class GitHubError(SignalsError):
    """Raised on failure to communicate with or parse responses from GitHub API."""


class GraphError(UnderstudyError):
    """Raised on failure to construct, validate, or cross-check the dependency graph."""


class GraphDiscrepancyError(GraphError):
    """Raised when observed traffic contradicts declared dependency graph topology."""


class PlannerError(UnderstudyError):
    """Raised on failure to generate, parse, or validate remediation plans."""


class PlaybookError(UnderstudyError):
    """Raised on playbook retrieval, embedding, confirmation, or storage failures."""


class PlaybookEmbeddingError(PlaybookError):
    """Raised on failure to generate vector embeddings."""


class PlaybookConfirmationError(PlaybookError):
    """Raised on failure during LLM playbook confirmation."""


class MirrorError(UnderstudyError):
    """Raised on failure to communicate with or perform operations against the mirror gateway."""


class TwinNotFoundError(MirrorError):
    """Raised when a requested twin is not registered with the mirror gateway."""


class NotificationError(UnderstudyError):
    """Raised on failure to post notifications or escalations."""


class SlackNotificationError(NotificationError):
    """Raised on failure to deliver a Slack notification."""
