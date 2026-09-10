"""Shared components for demo services: metrics, health, faults, db, and guards."""

from services._common.db import create_pool, get_connection, get_default_conninfo, ping_db
from services._common.faults import (
    FaultKind,
    FaultManager,
    FaultRequest,
    setup_fault_middleware,
    setup_fault_routes,
)
from services._common.health import setup_health_routes
from services._common.metrics import setup_metrics
from services._common.role_guard import (
    check_twin_outbound_target,
    ensure_fault_injection_permitted,
    get_service_role,
    is_fault_injection_enabled,
)

__all__ = [
    "FaultKind",
    "FaultManager",
    "FaultRequest",
    "check_twin_outbound_target",
    "create_pool",
    "ensure_fault_injection_permitted",
    "get_connection",
    "get_default_conninfo",
    "get_service_role",
    "is_fault_injection_enabled",
    "ping_db",
    "setup_fault_middleware",
    "setup_fault_routes",
    "setup_health_routes",
    "setup_metrics",
]
