"""Unit tests for role guard and twin egress security."""

import pytest
from fastapi import HTTPException

from services._common.role_guard import (
    check_twin_outbound_target,
    ensure_fault_injection_permitted,
    get_service_role,
    is_fault_injection_enabled,
)


def test_get_service_role_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNDERSTUDY_ROLE", raising=False)
    assert get_service_role() == "agent"


def test_get_service_role_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERSTUDY_ROLE", "PROD ")
    assert get_service_role() == "prod"


@pytest.mark.parametrize(
    ("env_val", "expected"),
    [
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("", False),
    ],
)
def test_is_fault_injection_enabled(
    monkeypatch: pytest.MonkeyPatch,
    env_val: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("UNDERSTUDY_FAULT_INJECTION_ENABLED", env_val)
    assert is_fault_injection_enabled() is expected


def test_ensure_fault_injection_permitted_in_non_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERSTUDY_ROLE", "twin")
    monkeypatch.setenv("UNDERSTUDY_FAULT_INJECTION_ENABLED", "false")
    # Should not raise
    ensure_fault_injection_permitted()


def test_ensure_fault_injection_permitted_in_prod_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNDERSTUDY_ROLE", "prod")
    monkeypatch.setenv("UNDERSTUDY_FAULT_INJECTION_ENABLED", "false")
    with pytest.raises(HTTPException) as exc_info:
        ensure_fault_injection_permitted()
    assert exc_info.value.status_code == 403
    assert "Fault injection is disabled in production" in str(exc_info.value.detail)


def test_ensure_fault_injection_permitted_in_prod_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNDERSTUDY_ROLE", "prod")
    monkeypatch.setenv("UNDERSTUDY_FAULT_INJECTION_ENABLED", "true")
    # Should not raise
    ensure_fault_injection_permitted()


def test_check_twin_outbound_target_non_twin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERSTUDY_ROLE", "prod")
    # In non-twin role, calling ust-prod is allowed
    check_twin_outbound_target("http://data-service.ust-prod.svc.cluster.local:8000")


def test_check_twin_outbound_target_twin_denies_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERSTUDY_ROLE", "twin")
    with pytest.raises(HTTPException) as exc_info:
        check_twin_outbound_target("http://data-service.ust-prod.svc.cluster.local:8000")
    assert exc_info.value.status_code == 403
    assert "Twin role denied egress" in str(exc_info.value.detail)


def test_check_twin_outbound_target_twin_allows_local_twin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNDERSTUDY_ROLE", "twin")
    # Twin calling twin service is allowed
    check_twin_outbound_target("http://data-service.ust-twin-inc-0.svc.cluster.local:8000")
