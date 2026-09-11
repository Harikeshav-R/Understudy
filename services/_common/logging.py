"""Structured JSON logging for demo services.

Deliberately independent of `understudy.common.logging`: `services/` ships in its own
minimal container images (see the `services` dependency group) and must not depend on
`src/understudy`.

Uses `structlog.wrap_logger` rather than the process-global `structlog.configure()`: the
latter is shared mutable state, and `understudy.common.logging.configure_logging` also
calls it (with an explicit, test-capture-time `sys.stdout` reference) -- whichever call
runs last during a test session wins for every logger in the process, including these.
Binding processors directly to each logger avoids that cross-module leakage entirely.
"""

import structlog


def get_logger(name: str) -> structlog.BoundLogger:
    """Return a structlog logger, bound to `name`, that emits one JSON event per line."""
    logger: structlog.BoundLogger = structlog.wrap_logger(
        structlog.PrintLogger(),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    ).bind(logger=name)
    return logger
