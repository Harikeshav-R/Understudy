"""Idempotent fleet teardown, garbage collection, and lifecycle handlers.

Implements build-plan step A2.5:
- Idempotent teardown by label selector `understudy.dev/incident=<id>`, dropping the twin database.
- `ust fleet gc`: reaps anything labelled and older than 1 hour (configurable).
- Process-level `atexit` and SIGTERM handlers registered to prevent orphaned environments.
"""

import asyncio
import atexit
import contextlib
import re
import signal
import sys
import threading
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from understudy.common.clock import Clock, resolve_clock
from understudy.common.config import Settings, get_settings
from understudy.common.errors import FleetError
from understudy.common.logging import get_logger
from understudy.contracts.twin import TwinHandle
from understudy.fleet.api import DatabaseCloner
from understudy.fleet.database import KubectlDatabaseExecutor, PostgresDatabaseCloner
from understudy.fleet.k8s import get_k8s_core_client

logger = get_logger(__name__)

DEFAULT_RETENTION_SECONDS = 3600.0  # 1 hour
DEFAULT_API_TIMEOUT_SECONDS = 10.0
TWIN_DB_REGEX = re.compile(r"^twin_(.+)_\d+$")


@dataclass(frozen=True)
class TeardownResult:
    """Summary of torn down resources for an incident."""

    incident_id: str
    deleted_namespaces: list[str]
    dropped_databases: list[str]


@dataclass(frozen=True)
class GcResult:
    """Summary of garbage-collected resources."""

    reaped_namespaces: list[str]
    dropped_databases: list[str]
    scanned_namespaces: int
    scanned_databases: int


# Process-level tracking of active incidents
_ACTIVE_INCIDENTS: set[str] = set()
_ACTIVE_INCIDENTS_LOCK = threading.Lock()

_HANDLERS_REGISTERED = False
_PREVIOUS_SIGTERM_HANDLER: Any = None
_GLOBAL_TEARDOWN_MANAGER: "FleetTeardownManager | None" = None
_HANDLER_LOCK = threading.Lock()


def register_active_incident(incident_id: str) -> None:
    """Register an active incident created in this process."""
    clean = incident_id.strip()
    if clean:
        with _ACTIVE_INCIDENTS_LOCK:
            _ACTIVE_INCIDENTS.add(clean)


def unregister_active_incident(incident_id: str) -> None:
    """Unregister an incident after full teardown."""
    clean = incident_id.strip()
    if clean:
        with _ACTIVE_INCIDENTS_LOCK:
            _ACTIVE_INCIDENTS.discard(clean)


def get_active_incidents() -> set[str]:
    """Return a copy of the currently active incidents."""
    with _ACTIVE_INCIDENTS_LOCK:
        return set(_ACTIVE_INCIDENTS)


def clear_active_incidents() -> None:
    """Clear all active incident registrations (primarily for test resets)."""
    with _ACTIVE_INCIDENTS_LOCK:
        _ACTIVE_INCIDENTS.clear()


def _run_sync[T](coro: Coroutine[Any, Any, T], timeout: float = 30.0) -> T:
    """Run an async coroutine synchronously, handling running loop edge cases."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        result: list[T] = []
        error: list[BaseException] = []
        event = threading.Event()

        def _worker() -> None:
            new_loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(new_loop)
                res = new_loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
                result.append(res)
            except BaseException as exc:
                error.append(exc)
            finally:
                new_loop.close()
                asyncio.set_event_loop(None)
                event.set()

        thread = threading.Thread(target=_worker)
        thread.start()
        event.wait(timeout=timeout + 5.0)
        thread.join(timeout=1.0)
        if error:
            raise error[0]
        if not result:
            raise TimeoutError("Coroutine execution timed out in worker thread")
        return result[0]

    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


def _atexit_handler() -> None:
    """Teardown active twins upon normal or unexpected process exit."""
    active = get_active_incidents()
    if not active:
        return

    logger.info("fleet_atexit_cleanup_started", active_incidents=list(active))
    manager = _GLOBAL_TEARDOWN_MANAGER or FleetTeardownManager()
    for incident_id in active:
        try:
            _run_sync(manager.teardown_incident(incident_id))
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "fleet_atexit_cleanup_failed",
                incident_id=incident_id,
                error=str(exc),
            )


def _sigterm_handler(signum: int, frame: Any) -> None:
    """Teardown active twins upon receiving SIGTERM, then chain or exit."""
    active = get_active_incidents()
    logger.info("fleet_sigterm_received", signal=signum, active_incidents=list(active))
    if active:
        manager = _GLOBAL_TEARDOWN_MANAGER or FleetTeardownManager()
        for incident_id in active:
            try:
                _run_sync(manager.teardown_incident(incident_id))
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "fleet_sigterm_cleanup_failed",
                    incident_id=incident_id,
                    error=str(exc),
                )

    prev = _PREVIOUS_SIGTERM_HANDLER
    if callable(prev):
        prev(signum, frame)
    elif prev == signal.SIG_IGN:
        return
    else:
        sys.exit(128 + signum)


def register_teardown_handlers(
    teardown_manager: "FleetTeardownManager | None" = None,
) -> None:
    """Register process-level atexit and SIGTERM cleanup handlers."""
    global _HANDLERS_REGISTERED, _PREVIOUS_SIGTERM_HANDLER, _GLOBAL_TEARDOWN_MANAGER

    with _HANDLER_LOCK:
        if teardown_manager is not None:
            _GLOBAL_TEARDOWN_MANAGER = teardown_manager

        if _HANDLERS_REGISTERED:
            return

        atexit.register(_atexit_handler)

        if threading.current_thread() is threading.main_thread():
            try:
                _PREVIOUS_SIGTERM_HANDLER = signal.getsignal(signal.SIGTERM)
                signal.signal(signal.SIGTERM, _sigterm_handler)
            except (ValueError, OSError) as exc:  # pragma: no cover
                logger.warning("fleet_sigterm_registration_failed", error=str(exc))

        _HANDLERS_REGISTERED = True


def unregister_teardown_handlers() -> None:
    """Unregister handlers and restore prior state (useful in test teardown)."""
    global _HANDLERS_REGISTERED, _PREVIOUS_SIGTERM_HANDLER, _GLOBAL_TEARDOWN_MANAGER

    with _HANDLER_LOCK:
        if not _HANDLERS_REGISTERED:
            return

        atexit.unregister(_atexit_handler)

        if (
            _PREVIOUS_SIGTERM_HANDLER is not None
            and threading.current_thread() is threading.main_thread()
        ):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, _PREVIOUS_SIGTERM_HANDLER)
            _PREVIOUS_SIGTERM_HANDLER = None

        _GLOBAL_TEARDOWN_MANAGER = None
        _HANDLERS_REGISTERED = False


class FleetTeardownManager:
    """Manages twin environment destruction and age-based garbage collection."""

    def __init__(
        self,
        core_api: client.CoreV1Api | None = None,
        database_cloner: DatabaseCloner | None = None,
        settings: Settings | None = None,
        clock: Clock | None = None,
        api_timeout_seconds: float = DEFAULT_API_TIMEOUT_SECONDS,
    ) -> None:
        self.settings = settings or get_settings()
        self.clock = resolve_clock(clock)
        self.api_timeout_seconds = api_timeout_seconds
        self._core_api = core_api
        self._database_cloner = database_cloner

    @property
    def core_api(self) -> client.CoreV1Api:
        """Lazily initialize Kubernetes CoreV1Api client."""
        if self._core_api is None:
            self._core_api = get_k8s_core_client(context=self.settings.cluster.context)
        return self._core_api

    @property
    def database_cloner(self) -> DatabaseCloner:
        """Lazily initialize twin PostgreSQL database cloner."""
        if self._database_cloner is None:
            executor = KubectlDatabaseExecutor(
                namespace=self.settings.cluster.system_namespace,
                deployment="twin-postgres",
                context=self.settings.cluster.context,
            )
            self._database_cloner = PostgresDatabaseCloner(
                executor=executor,
                clock=self.clock,
            )
        return self._database_cloner

    async def teardown_twin(self, twin_handle: TwinHandle) -> None:
        """Tear down a specific twin namespace and its cloned database."""
        log = get_logger(incident_id=twin_handle.incident_id)
        log.info(
            "twin_teardown_started",
            twin_id=twin_handle.twin_id,
            namespace=twin_handle.namespace,
        )

        try:
            await asyncio.to_thread(
                self.core_api.delete_namespace,
                name=twin_handle.namespace,
                _request_timeout=self.api_timeout_seconds,
            )
        except ApiException as exc:
            if exc.status not in (404, 409):
                raise FleetError(
                    f"Failed to delete namespace {twin_handle.namespace!r}: {exc.reason}"
                ) from exc

        await self.database_cloner.drop_twin_database(twin_handle.database)
        log.info("twin_teardown_completed", twin_id=twin_handle.twin_id)

    async def teardown_incident(self, incident_id: str) -> TeardownResult:
        """Idempotently tear down all twin environments associated with an incident."""
        if not incident_id or not incident_id.strip():
            raise FleetError(f"Incident ID cannot be empty, got: {incident_id!r}")

        log = get_logger(incident_id=incident_id)
        log.info("fleet_teardown_all_started", incident_id=incident_id)

        label_selector = f"understudy.dev/incident={incident_id}"
        deleted_namespaces: list[str] = []

        try:
            ns_resp = await asyncio.to_thread(
                self.core_api.list_namespace,
                label_selector=label_selector,
                _request_timeout=self.api_timeout_seconds,
            )
            for ns in ns_resp.items or []:
                if ns.metadata and ns.metadata.name:
                    name = ns.metadata.name
                    try:
                        await asyncio.to_thread(
                            self.core_api.delete_namespace,
                            name=name,
                            _request_timeout=self.api_timeout_seconds,
                        )
                        deleted_namespaces.append(name)
                    except ApiException as exc:
                        if exc.status not in (404, 409):
                            raise
                        deleted_namespaces.append(name)
        except ApiException as exc:
            if exc.status != 404:
                raise FleetError(
                    f"Failed to query or delete namespaces for incident {incident_id!r}: "
                    f"{exc.reason}"
                ) from exc

        dropped_dbs = await self.database_cloner.drop_all_incident_databases(incident_id)
        unregister_active_incident(incident_id)

        log.info(
            "fleet_teardown_all_completed",
            incident_id=incident_id,
            namespaces=deleted_namespaces,
            databases=dropped_dbs,
        )
        return TeardownResult(
            incident_id=incident_id,
            deleted_namespaces=deleted_namespaces,
            dropped_databases=dropped_dbs,
        )

    async def gc(self, older_than_seconds: float = DEFAULT_RETENTION_SECONDS) -> GcResult:
        """Garbage collect twin namespaces and databases older than the retention threshold."""
        logger.info("fleet_gc_started", older_than_seconds=older_than_seconds)

        label_selector = "understudy.dev/incident"
        try:
            ns_resp = await asyncio.to_thread(
                self.core_api.list_namespace,
                label_selector=label_selector,
                _request_timeout=self.api_timeout_seconds,
            )
        except ApiException as exc:
            raise FleetError(f"Failed to list namespaces for GC: {exc.reason}") from exc

        now = self.clock.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        reaped_namespaces: list[str] = []
        reaped_incidents: set[str] = set()
        active_incident_namespaces: dict[str, set[str]] = {}

        items = list(ns_resp.items or [])
        for ns in items:
            if not ns.metadata or not ns.metadata.name:
                continue
            name = ns.metadata.name
            labels = ns.metadata.labels or {}
            inc_id = labels.get("understudy.dev/incident")

            if inc_id:
                active_incident_namespaces.setdefault(inc_id, set()).add(name)

            creation_ts = ns.metadata.creation_timestamp
            if creation_ts:
                if creation_ts.tzinfo is None:
                    creation_ts = creation_ts.replace(tzinfo=UTC)
                age = (now - creation_ts).total_seconds()
            else:
                age = 0.0

            if age >= older_than_seconds:
                try:
                    await asyncio.to_thread(
                        self.core_api.delete_namespace,
                        name=name,
                        _request_timeout=self.api_timeout_seconds,
                    )
                    reaped_namespaces.append(name)
                    if inc_id:
                        reaped_incidents.add(inc_id)
                except ApiException as exc:
                    if exc.status not in (404, 409):
                        raise FleetError(
                            f"Failed to delete expired namespace {name!r}: {exc.reason}"
                        ) from exc
                    reaped_namespaces.append(name)
                    if inc_id:
                        reaped_incidents.add(inc_id)

        # Drop databases for incidents whose namespaces were fully or partially reaped
        dropped_databases: list[str] = []
        for inc_id in sorted(reaped_incidents):
            dbs = await self.database_cloner.drop_all_incident_databases(inc_id)
            dropped_databases.extend(dbs)
            unregister_active_incident(inc_id)

        # Reap orphaned twin databases that have no surviving namespaces in the cluster
        all_twin_dbs = await self.database_cloner.list_twin_databases()
        reaped_set = set(reaped_namespaces)

        for db in all_twin_dbs:
            if db in dropped_databases:
                continue

            match = TWIN_DB_REGEX.match(db)
            if match:
                db_incident_clean = match.group(1)
                # Check if any namespace for this incident is still active and not reaped
                has_active_ns = any(
                    ns_inc.replace("-", "_").strip() == db_incident_clean
                    and bool(active_incident_namespaces[ns_inc] - reaped_set)
                    for ns_inc in active_incident_namespaces
                )
                if not has_active_ns:
                    await self.database_cloner.drop_twin_database(db)
                    dropped_databases.append(db)

        logger.info(
            "fleet_gc_completed",
            reaped_namespaces=reaped_namespaces,
            dropped_databases=dropped_databases,
            scanned_namespaces=len(items),
            scanned_databases=len(all_twin_dbs),
        )
        return GcResult(
            reaped_namespaces=reaped_namespaces,
            dropped_databases=dropped_databases,
            scanned_namespaces=len(items),
            scanned_databases=len(all_twin_dbs),
        )


__all__ = [
    "DEFAULT_API_TIMEOUT_SECONDS",
    "DEFAULT_RETENTION_SECONDS",
    "FleetTeardownManager",
    "GcResult",
    "TeardownResult",
    "clear_active_incidents",
    "get_active_incidents",
    "register_active_incident",
    "register_teardown_handlers",
    "unregister_active_incident",
    "unregister_teardown_handlers",
]
