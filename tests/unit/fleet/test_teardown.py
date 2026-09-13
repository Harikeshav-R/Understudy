"""Unit tests for fleet teardown, garbage collection, and lifecycle handlers.

Enforces 100% line and branch coverage on understudy/fleet/teardown.py.
"""

import signal
import threading
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from understudy.common.clock import FrozenClock
from understudy.common.config import ClusterSettings, Settings
from understudy.common.errors import FleetError
from understudy.contracts.twin import TwinHandle
from understudy.fleet.teardown import (
    DEFAULT_API_TIMEOUT_SECONDS,
    DEFAULT_RETENTION_SECONDS,
    FleetTeardownManager,
    TeardownResult,
    _atexit_handler,
    _run_sync,
    _sigterm_handler,
    clear_active_incidents,
    get_active_incidents,
    register_active_incident,
    register_teardown_handlers,
    unregister_active_incident,
    unregister_teardown_handlers,
)


@pytest.fixture(autouse=True)
def reset_lifecycle_state() -> Generator[None, None, None]:
    """Ensure clean active incident state and unregister handlers for every test."""
    clear_active_incidents()
    unregister_teardown_handlers()
    yield
    clear_active_incidents()
    unregister_teardown_handlers()


def test_active_incidents_registry() -> None:
    """Verify active incidents registration, unregistration, and clearing."""
    assert get_active_incidents() == set()

    # Empty strings or whitespace ignored
    register_active_incident("")
    register_active_incident("   ")
    assert get_active_incidents() == set()

    register_active_incident("inc_1")
    register_active_incident(" inc_2 ")
    assert get_active_incidents() == {"inc_1", "inc_2"}

    # Unregister
    unregister_active_incident("inc_1")
    unregister_active_incident("")
    assert get_active_incidents() == {"inc_2"}

    clear_active_incidents()
    assert get_active_incidents() == set()


def test_lifecycle_handlers_registration_and_unregistration() -> None:
    """Verify idempotency of register/unregister teardown handlers."""
    mock_manager = MagicMock(spec=FleetTeardownManager)

    with (
        patch("understudy.fleet.teardown.atexit.register") as mock_atexit_reg,
        patch("understudy.fleet.teardown.atexit.unregister") as mock_atexit_unreg,
        patch("understudy.fleet.teardown.signal.signal") as mock_sig,
        patch("understudy.fleet.teardown.signal.getsignal") as mock_getsig,
    ):
        mock_getsig.return_value = signal.SIG_DFL

        # First registration with manager
        register_teardown_handlers(mock_manager)
        mock_atexit_reg.assert_called_once()
        mock_sig.assert_called_once()

        # Idempotent call
        register_teardown_handlers(mock_manager)
        assert mock_atexit_reg.call_count == 1

        # Unregister
        unregister_teardown_handlers()
        mock_atexit_unreg.assert_called_once()
        assert mock_sig.call_count == 2  # Restores previous handler

        # Idempotent unregister
        unregister_teardown_handlers()
        assert mock_atexit_unreg.call_count == 1

        # Register without manager
        register_teardown_handlers(None)
        assert mock_atexit_reg.call_count == 2
        unregister_teardown_handlers()


def test_lifecycle_handlers_non_main_thread() -> None:
    """Verify signal handlers are skipped when called outside main thread."""

    def worker() -> None:
        register_teardown_handlers()
        unregister_teardown_handlers()

    t = threading.Thread(target=worker)
    t.start()
    t.join()


def test_atexit_handler() -> None:
    """Verify _atexit_handler cleans up active incidents."""
    # When no active incidents, does nothing
    with patch("understudy.fleet.teardown.FleetTeardownManager") as mock_mgr_cls:
        _atexit_handler()
        mock_mgr_cls.assert_not_called()

    # When active incidents exist
    register_active_incident("inc_exit_1")
    mock_instance = MagicMock()

    async def fake_teardown(inc: str) -> TeardownResult:
        return TeardownResult(inc, [], [])

    mock_instance.teardown_incident = fake_teardown

    with (
        patch("understudy.fleet.teardown._GLOBAL_TEARDOWN_MANAGER", mock_instance),
        patch("understudy.fleet.teardown._run_sync") as mock_run_sync,
    ):
        mock_run_sync.side_effect = lambda coro: coro.close()
        _atexit_handler()
        assert mock_run_sync.call_count == 1


def test_sigterm_handler() -> None:
    """Verify _sigterm_handler executes cleanup and handles signal exit."""
    # Case 0: No active incidents
    clear_active_incidents()
    with (
        patch("understudy.fleet.teardown._GLOBAL_TEARDOWN_MANAGER") as mock_mgr,
        patch("understudy.fleet.teardown._PREVIOUS_SIGTERM_HANDLER", signal.SIG_IGN),
    ):
        _sigterm_handler(signal.SIGTERM, None)
        mock_mgr.assert_not_called()

    # Setup active incident
    register_active_incident("inc_sig_1")
    mock_instance = MagicMock()

    async def fake_teardown(inc: str) -> TeardownResult:
        return TeardownResult(inc, [], [])

    mock_instance.teardown_incident = fake_teardown

    # Case 1: Callable previous handler
    prev_handler = MagicMock()
    with (
        patch("understudy.fleet.teardown._GLOBAL_TEARDOWN_MANAGER", mock_instance),
        patch("understudy.fleet.teardown._PREVIOUS_SIGTERM_HANDLER", prev_handler),
        patch("understudy.fleet.teardown._run_sync") as mock_run_sync,
    ):
        mock_run_sync.side_effect = lambda coro: coro.close()
        _sigterm_handler(signal.SIGTERM, None)
        assert mock_run_sync.call_count == 1
        prev_handler.assert_called_once_with(signal.SIGTERM, None)

    # Case 2: SIG_IGN previous handler
    with (
        patch("understudy.fleet.teardown._GLOBAL_TEARDOWN_MANAGER", mock_instance),
        patch("understudy.fleet.teardown._PREVIOUS_SIGTERM_HANDLER", signal.SIG_IGN),
        patch("understudy.fleet.teardown._run_sync") as mock_run_sync,
    ):
        mock_run_sync.side_effect = lambda coro: coro.close()
        _sigterm_handler(signal.SIGTERM, None)

    # Case 3: Default handler -> sys.exit(128 + signum)
    with (
        patch("understudy.fleet.teardown._GLOBAL_TEARDOWN_MANAGER", mock_instance),
        patch("understudy.fleet.teardown._PREVIOUS_SIGTERM_HANDLER", signal.SIG_DFL),
        patch("understudy.fleet.teardown._run_sync") as mock_run_sync,
        pytest.raises(SystemExit) as exc_info,
    ):
        mock_run_sync.side_effect = lambda coro: coro.close()
        _sigterm_handler(signal.SIGTERM, None)
    assert exc_info.value.code == 128 + signal.SIGTERM


def test_run_sync_no_loop() -> None:
    """Verify _run_sync runs coroutine when no event loop is running."""

    async def sample() -> int:
        return 42

    result = _run_sync(sample())
    assert result == 42


@pytest.mark.asyncio
async def test_run_sync_with_running_loop() -> None:
    """Verify _run_sync delegates to thread pool when event loop is running."""

    async def sample() -> str:
        return "loop-running"

    result = _run_sync(sample())
    assert result == "loop-running"


@pytest.mark.asyncio
async def test_run_sync_with_running_loop_raises_exception() -> None:
    """Verify _run_sync re-raises exceptions from coroutine when running in thread."""

    async def failing() -> None:
        raise ValueError("test worker error")

    with pytest.raises(ValueError, match="test worker error"):
        _run_sync(failing())


@pytest.mark.asyncio
async def test_run_sync_with_running_loop_timeout() -> None:
    """Verify _run_sync raises TimeoutError when worker thread yields no result."""

    async def sample() -> str:
        return "ok"

    coro = sample()
    try:
        with (
            patch("threading.Thread.start"),
            patch("threading.Thread.join"),
            patch("threading.Event.wait"),
            pytest.raises(TimeoutError, match="Coroutine execution timed out in worker thread"),
        ):
            _run_sync(coro)
    finally:
        coro.close()


def test_manager_lazy_properties() -> None:
    """Verify lazy initialization of core_api and database_cloner."""
    with (
        patch("understudy.fleet.teardown.get_k8s_core_client") as mock_core,
        patch("understudy.fleet.teardown.KubectlDatabaseExecutor") as mock_exec,
        patch("understudy.fleet.teardown.PostgresDatabaseCloner") as mock_cloner,
    ):
        settings = Settings(cluster=ClusterSettings(context="k3d-test"))
        manager = FleetTeardownManager(settings=settings)

        _ = manager.core_api
        mock_core.assert_called_once_with(context="k3d-test")

        _ = manager.database_cloner
        mock_exec.assert_called_once_with(
            namespace=settings.cluster.system_namespace,
            deployment="twin-postgres",
            context="k3d-test",
        )
        mock_cloner.assert_called_once()


@pytest.mark.asyncio
async def test_teardown_twin_success() -> None:
    """Verify teardown_twin deletes namespace and drops DB."""
    core_api = MagicMock()
    database_cloner = AsyncMock()
    manager = FleetTeardownManager(
        core_api=core_api,
        database_cloner=database_cloner,
    )

    handle = TwinHandle(
        twin_id="twin_inc_1_0",
        incident_id="inc_1",
        candidate_index=0,
        namespace="ust-twin-inc_1-0",
        database="twin_inc_1_0",
        forked_from_snapshot_at=datetime.now(UTC),
        state="ready",
    )

    await manager.teardown_twin(handle)
    core_api.delete_namespace.assert_called_once_with(
        name="ust-twin-inc_1-0",
        _request_timeout=DEFAULT_API_TIMEOUT_SECONDS,
    )
    database_cloner.drop_twin_database.assert_called_once_with("twin_inc_1_0")


@pytest.mark.asyncio
async def test_teardown_twin_exceptions() -> None:
    """Verify teardown_twin ignores 404/409 and raises on other ApiExceptions."""
    core_api = MagicMock()
    database_cloner = AsyncMock()
    manager = FleetTeardownManager(
        core_api=core_api,
        database_cloner=database_cloner,
    )

    handle = TwinHandle(
        twin_id="twin_inc_1_0",
        incident_id="inc_1",
        candidate_index=0,
        namespace="ust-twin-inc_1-0",
        database="twin_inc_1_0",
        forked_from_snapshot_at=datetime.now(UTC),
        state="ready",
    )

    # 404 ignored
    core_api.delete_namespace.side_effect = ApiException(status=404, reason="Not Found")
    await manager.teardown_twin(handle)

    # 409 ignored
    core_api.delete_namespace.side_effect = ApiException(status=409, reason="Conflict")
    await manager.teardown_twin(handle)

    # 500 raises FleetError
    core_api.delete_namespace.side_effect = ApiException(status=500, reason="Server Error")
    with pytest.raises(FleetError, match="Failed to delete namespace"):
        await manager.teardown_twin(handle)


@pytest.mark.asyncio
async def test_teardown_incident_validation_and_success() -> None:
    """Verify teardown_incident validates input, deletes namespaces, drops DBs."""
    core_api = MagicMock()
    database_cloner = AsyncMock()
    manager = FleetTeardownManager(
        core_api=core_api,
        database_cloner=database_cloner,
    )

    # Empty input
    with pytest.raises(FleetError, match="Incident ID cannot be empty"):
        await manager.teardown_incident("")

    with pytest.raises(FleetError, match="Incident ID cannot be empty"):
        await manager.teardown_incident("   ")

    # Success scenario
    ns1 = MagicMock()
    ns1.metadata.name = "ust-twin-inc_1-0"
    ns2 = MagicMock()
    ns2.metadata.name = "ust-twin-inc_1-1"
    ns_empty = MagicMock()
    ns_empty.metadata = None

    core_api.list_namespace.return_value = MagicMock(items=[ns1, ns2, ns_empty])
    database_cloner.drop_all_incident_databases.return_value = [
        "twin_inc_1_0",
        "twin_inc_1_1",
    ]

    register_active_incident("inc_1")
    res = await manager.teardown_incident("inc_1")

    assert res == TeardownResult(
        incident_id="inc_1",
        deleted_namespaces=["ust-twin-inc_1-0", "ust-twin-inc_1-1"],
        dropped_databases=["twin_inc_1_0", "twin_inc_1_1"],
    )
    assert core_api.delete_namespace.call_count == 2
    database_cloner.drop_all_incident_databases.assert_called_once_with("inc_1")
    assert "inc_1" not in get_active_incidents()


@pytest.mark.asyncio
async def test_teardown_incident_exceptions() -> None:
    """Verify teardown_incident handles list and delete API exceptions."""
    core_api = MagicMock()
    database_cloner = AsyncMock()
    manager = FleetTeardownManager(
        core_api=core_api,
        database_cloner=database_cloner,
    )

    # list_namespace returns 404 -> treated as empty
    core_api.list_namespace.side_effect = ApiException(status=404, reason="Not Found")
    database_cloner.drop_all_incident_databases.return_value = []
    res = await manager.teardown_incident("inc_1")
    assert res.deleted_namespaces == []

    # list_namespace returns 500 -> raises FleetError
    core_api.list_namespace.side_effect = ApiException(status=500, reason="Error")
    with pytest.raises(FleetError, match="Failed to query or delete namespaces"):
        await manager.teardown_incident("inc_1")

    # delete_namespace returns 404 and 409 -> ignored
    ns1 = MagicMock()
    ns1.metadata.name = "ust-twin-inc_1-0"
    core_api.list_namespace.side_effect = None
    core_api.list_namespace.return_value = MagicMock(items=[ns1])
    core_api.delete_namespace.side_effect = ApiException(status=404, reason="Not Found")
    res = await manager.teardown_incident("inc_1")
    assert res.deleted_namespaces == ["ust-twin-inc_1-0"]

    core_api.delete_namespace.side_effect = ApiException(status=409, reason="Conflict")
    res = await manager.teardown_incident("inc_1")
    assert res.deleted_namespaces == ["ust-twin-inc_1-0"]

    # delete_namespace returns 500 -> raises FleetError
    core_api.delete_namespace.side_effect = ApiException(status=500, reason="Error")
    with pytest.raises(FleetError, match="Failed to query or delete namespaces"):
        await manager.teardown_incident("inc_1")


@pytest.mark.asyncio
async def test_gc_logic() -> None:
    """Verify garbage collection of expired namespaces and orphaned twin databases."""
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
    clock = FrozenClock(now)

    core_api = MagicMock()
    database_cloner = AsyncMock()
    manager = FleetTeardownManager(
        core_api=core_api,
        database_cloner=database_cloner,
        clock=clock,
    )

    # Case 1: list_namespace failure raises FleetError
    core_api.list_namespace.side_effect = ApiException(status=500, reason="List failed")
    with pytest.raises(FleetError, match="Failed to list namespaces for GC"):
        await manager.gc()

    core_api.list_namespace.side_effect = None

    # Namespaces:
    # 1. Expired namespace (creation 2 hours ago): ust-twin-expired-0
    ns_expired = MagicMock()
    ns_expired.metadata.name = "ust-twin-expired-0"
    ns_expired.metadata.labels = {"understudy.dev/incident": "inc_expired"}
    ns_expired.metadata.creation_timestamp = now - timedelta(seconds=7200)

    # 2. Fresh namespace (creation 10 mins ago): ust-twin-fresh-0
    ns_fresh = MagicMock()
    ns_fresh.metadata.name = "ust-twin-fresh-0"
    ns_fresh.metadata.labels = {"understudy.dev/incident": "inc_fresh"}
    ns_fresh.metadata.creation_timestamp = now - timedelta(seconds=600)

    # 3. Namespace with naive creation_timestamp (older than 1h)
    ns_naive = MagicMock()
    ns_naive.metadata.name = "ust-twin-naive-0"
    ns_naive.metadata.labels = {"understudy.dev/incident": "inc_naive"}
    ns_naive.metadata.creation_timestamp = (now - timedelta(seconds=4000)).replace(tzinfo=None)

    # 4. Namespace with no creation_timestamp (age = 0)
    ns_no_ts = MagicMock()
    ns_no_ts.metadata.name = "ust-twin-nots-0"
    ns_no_ts.metadata.labels = {"understudy.dev/incident": "inc_nots"}
    ns_no_ts.metadata.creation_timestamp = None

    # 5. Namespace expired without incident label
    ns_no_inc = MagicMock()
    ns_no_inc.metadata.name = "ust-twin-noinc-0"
    ns_no_inc.metadata.labels = {"other": "value"}
    ns_no_inc.metadata.creation_timestamp = now - timedelta(seconds=5000)

    # 6. Empty metadata
    ns_empty = MagicMock()
    ns_empty.metadata = None

    core_api.list_namespace.return_value = MagicMock(
        items=[ns_expired, ns_fresh, ns_naive, ns_no_ts, ns_no_inc, ns_empty]
    )

    # Twin databases on twin-postgres:
    # - twin_inc_expired_0 (dropped by incident teardown)
    # - twin_inc_fresh_0 (active namespace exists, not dropped)
    # - twin_inc_orphan_0 (orphan, dropped)
    # - twin_unmatched_name (ignored by regex)
    database_cloner.drop_all_incident_databases.side_effect = lambda inc: [f"twin_{inc}_0"]
    database_cloner.list_twin_databases.return_value = [
        "twin_inc_expired_0",
        "twin_inc_fresh_0",
        "twin_inc_orphan_0",
        "twin_unmatched_name",
    ]

    gc_res = await manager.gc(older_than_seconds=DEFAULT_RETENTION_SECONDS)

    assert "ust-twin-expired-0" in gc_res.reaped_namespaces
    assert "ust-twin-naive-0" in gc_res.reaped_namespaces
    assert "ust-twin-noinc-0" in gc_res.reaped_namespaces
    assert "ust-twin-fresh-0" not in gc_res.reaped_namespaces
    assert "ust-twin-nots-0" not in gc_res.reaped_namespaces

    # Orphaned database twin_inc_orphan_0 dropped
    database_cloner.drop_twin_database.assert_called_once_with("twin_inc_orphan_0")
    assert "twin_inc_orphan_0" in gc_res.dropped_databases
    assert "twin_inc_fresh_0" not in gc_res.dropped_databases
    assert "twin_unmatched_name" not in gc_res.dropped_databases

    # Case: delete_namespace error handling during GC (404/409 ignored, 500 raises)
    core_api.list_namespace.return_value = MagicMock(items=[ns_expired, ns_no_inc])
    core_api.delete_namespace.side_effect = ApiException(status=404, reason="Not found")
    res_404 = await manager.gc(older_than_seconds=DEFAULT_RETENTION_SECONDS)
    assert "ust-twin-expired-0" in res_404.reaped_namespaces

    core_api.delete_namespace.side_effect = ApiException(status=409, reason="Conflict")
    res_409 = await manager.gc(older_than_seconds=DEFAULT_RETENTION_SECONDS)
    assert "ust-twin-expired-0" in res_409.reaped_namespaces

    core_api.delete_namespace.side_effect = ApiException(status=500, reason="Error")
    with pytest.raises(FleetError, match="Failed to delete expired namespace"):
        await manager.gc(older_than_seconds=DEFAULT_RETENTION_SECONDS)


@pytest.mark.asyncio
async def test_gc_with_naive_clock() -> None:
    """Verify gc correctly handles clock returning naive datetime."""

    class NaiveClock:
        def now(self) -> datetime:
            return datetime(2026, 9, 13, 12, 0, 0)

        async def sleep(self, seconds: float) -> None:
            pass

    core_api = MagicMock()
    database_cloner = AsyncMock()
    manager = FleetTeardownManager(
        core_api=core_api,
        database_cloner=database_cloner,
        clock=NaiveClock(),
    )

    core_api.list_namespace.return_value = MagicMock(items=[])
    database_cloner.list_twin_databases.return_value = []
    gc_res = await manager.gc()
    assert gc_res.reaped_namespaces == []
