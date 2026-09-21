"""Twin fleet cleanup and mirror unregistration utility."""

from __future__ import annotations

from typing import TYPE_CHECKING

from understudy.common.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from understudy.contracts.twin import TwinHandle
    from understudy.orchestrator.api import Deps


async def cleanup_incident_twins(
    twins: Sequence[TwinHandle],
    incident_id: str | None,
    deps: Deps,
) -> list[str]:
    """Unregister mirrors and tear down twins best-effort, returning cleanup error descriptions.

    Architecture §2.9: cleanup is best-effort; one leaked twin must not stop the remaining
    teardown or run record persistence. Teardown is idempotent and retried by `ust fleet gc`.
    """
    logger = get_logger(incident_id=incident_id or "unknown")
    cleanup_errors: list[str] = []

    for twin in twins:
        try:
            await deps.mirror_registry.unregister_twin(twin.twin_id)
        except Exception as exc:  # Emergency cleanup resilience (AGENTS.md §5.4; #47)
            msg = f"unregister_twin({twin.twin_id}) failed: {exc}"
            cleanup_errors.append(msg)
            logger.warning("cleanup_unregister_twin_failed", twin_id=twin.twin_id, error=str(exc))

    if incident_id:
        try:
            await deps.fleet_controller.teardown_all(incident_id)
        except Exception as exc:  # Emergency cleanup resilience (AGENTS.md §5.4; #47)
            msg = f"teardown_all({incident_id}) failed: {exc}"
            cleanup_errors.append(msg)
            logger.warning("cleanup_teardown_all_failed", incident_id=incident_id, error=str(exc))

    return cleanup_errors


__all__ = ["cleanup_incident_twins"]
