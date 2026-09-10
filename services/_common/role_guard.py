"""Role guard and twin egress checks for Understudy demo services."""

import os
from urllib.parse import urlparse

from fastapi import HTTPException


def get_service_role() -> str:
    """Return the configured Understudy role (e.g. 'prod', 'twin', 'agent')."""
    return os.getenv("UNDERSTUDY_ROLE", "agent").strip().lower()


def is_fault_injection_enabled() -> bool:
    """Check if fault injection is explicitly allowed in production."""
    val = os.getenv("UNDERSTUDY_FAULT_INJECTION_ENABLED", "false").strip().lower()
    return val in ("true", "1", "yes")


def ensure_fault_injection_permitted() -> None:
    """Verify that the current environment permits fault injection mutations.

    Refuses in 'prod' role unless UNDERSTUDY_FAULT_INJECTION_ENABLED=true.
    """
    role = get_service_role()
    if role == "prod" and not is_fault_injection_enabled():
        raise HTTPException(
            status_code=403,
            detail="Fault injection is disabled in production",
        )


def check_twin_outbound_target(url: str) -> None:
    """Verify outbound call destination when running in twin role.

    Under ADR-014, twin services may not call production endpoints (ust-prod).
    """
    role = get_service_role()
    if role != "twin":
        return

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if "ust-prod" in hostname or "ust-prod" in url.lower():
        raise HTTPException(
            status_code=403,
            detail=f"Twin role denied egress to production destination: {hostname}",
        )
