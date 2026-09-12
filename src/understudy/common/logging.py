"""Structured JSON logging configuration using structlog."""

import logging
from typing import Any, TextIO

import structlog


def configure_logging(log_level: str = "INFO", file: TextIO | None = None) -> None:
    """Configure structlog to emit one JSON event per line to stdout or specified stream."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]

    factory = (
        structlog.PrintLoggerFactory(file=file)
        if file is not None
        else structlog.PrintLoggerFactory()
    )

    structlog.configure(
        processors=processors,
        logger_factory=factory,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=False,
    )


def get_logger(incident_id: str | None = None, **initial_values: Any) -> structlog.BoundLogger:
    """Return a configured structured logger, optionally bound with incident_id."""
    logger: structlog.BoundLogger = structlog.get_logger()
    values: dict[str, Any] = dict(initial_values)
    if incident_id is not None:
        values["incident_id"] = incident_id
    if values:
        logger = logger.bind(**values)
    return logger
