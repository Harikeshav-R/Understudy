"""Role guard and twin egress checks for Understudy demo services."""

import ipaddress
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


def _is_ip_literal(hostname: str) -> bool:
    """Return True if hostname is an IPv4/IPv6 literal rather than a DNS name."""
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def check_twin_outbound_target(url: str) -> None:
    """Verify outbound call destination when running in twin role.

    Under ADR-014, twin services may not call production endpoints (ust-prod). A
    legitimate in-cluster call always addresses a service by Kubernetes DNS name, so a
    missing hostname or a bare IP literal is denied outright rather than treated as an
    unmatched (and therefore allowed) destination; this closes the ClusterIP bypass of a
    plain substring check. The production namespace is matched as an exact DNS label
    (split on ".") rather than a substring, so a name that merely contains "ust-prod" is
    not misclassified as production.
    """
    role = get_service_role()
    if role != "twin":
        return

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise HTTPException(
            status_code=403,
            detail=f"Twin role denied egress to target with no resolvable hostname: {url}",
        )
    if _is_ip_literal(hostname):
        raise HTTPException(
            status_code=403,
            detail=f"Twin role denied egress to IP literal destination: {hostname}",
        )
    if "ust-prod" in hostname.split("."):
        raise HTTPException(
            status_code=403,
            detail=f"Twin role denied egress to production destination: {hostname}",
        )
