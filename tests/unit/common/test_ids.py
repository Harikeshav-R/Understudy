"""Unit tests for ULID-based ID generators."""

import pytest

from understudy.common.ids import (
    generate_ulid,
    new_alert_id,
    new_incident_id,
    new_plan_id,
    new_run_id,
    new_twin_id,
)


def test_generate_ulid_format() -> None:
    ulid = generate_ulid()
    assert len(ulid) == 26
    assert ulid.isalnum()
    assert ulid.islower()


def test_generate_ulid_deterministic_params() -> None:
    fixed_ts = 1700000000000
    fixed_random = b"\x01" * 10
    u1 = generate_ulid(timestamp_ms=fixed_ts, random_bytes=fixed_random)
    u2 = generate_ulid(timestamp_ms=fixed_ts, random_bytes=fixed_random)
    assert u1 == u2


def test_generate_ulid_invalid_random_bytes_length() -> None:
    with pytest.raises(ValueError, match="must be exactly 10 bytes"):
        generate_ulid(random_bytes=b"short")


def test_new_id_prefixes() -> None:
    inc_id = new_incident_id()
    assert inc_id.startswith("inc_")
    assert len(inc_id) == 4 + 26

    plan_id = new_plan_id()
    assert plan_id.startswith("plan_")
    assert len(plan_id) == 5 + 26

    twin_id = new_twin_id()
    assert twin_id.startswith("twin_")
    assert len(twin_id) == 5 + 26

    run_id = new_run_id()
    assert run_id.startswith("run_")
    assert len(run_id) == 4 + 26

    alert_id = new_alert_id()
    assert alert_id.startswith("alt_")
    assert len(alert_id) == 4 + 26
