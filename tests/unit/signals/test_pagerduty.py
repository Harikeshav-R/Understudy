"""Unit tests for PagerDuty webhook receiver, HMAC verification, and AlertSource."""

import hashlib
import hmac
from datetime import UTC

import httpx
import pytest

from understudy.common.clock import FrozenClock
from understudy.contracts.incident import Alert
from understudy.signals.api import AlertSource
from understudy.signals.pagerduty import (
    PagerDutyAlertSource,
    WebhookReceiverServer,
    _map_severity,
    _normalize_service_name,
    _parse_timestamp,
    create_webhook_app,
    parse_pagerduty_webhook,
    verify_pagerduty_signature,
)


def _compute_pd_sig(body: bytes, secret: str) -> str:
    clean_secret = secret.strip().removeprefix("v1=").encode("utf-8")
    hex_digest = hmac.new(clean_secret, body, hashlib.sha256).hexdigest()
    return f"v1={hex_digest}"


def test_verify_pagerduty_signature_valid() -> None:
    secret = "secret123"
    body = b'{"hello": "world"}'
    sig = _compute_pd_sig(body, secret)

    assert verify_pagerduty_signature(body, sig, secret) is True
    # Comma-separated list with stale signature
    multi_sig = f"v1=stalehex123, {sig}"
    assert verify_pagerduty_signature(body, multi_sig, secret) is True
    # Secret with v1= prefix in config
    assert verify_pagerduty_signature(body, sig, f"v1={secret}") is True


def test_verify_pagerduty_signature_invalid() -> None:
    body = b'{"hello": "world"}'
    assert verify_pagerduty_signature(body, "v1=badhex", "secret123") is False
    assert verify_pagerduty_signature(body, None, "secret123") is False
    assert verify_pagerduty_signature(body, "v1=somehex", "") is False
    assert verify_pagerduty_signature(body, "v1=somehex", None) is False
    assert verify_pagerduty_signature(body, "", "secret123") is False


def test_normalize_service_name() -> None:
    assert _normalize_service_name("edge-gateway") == "edge-gateway"
    assert _normalize_service_name("ust-edge-gateway-prod") == "edge-gateway"
    assert _normalize_service_name("auth-service-k8s") == "auth-service"
    assert _normalize_service_name("data-service-db") == "data-service"
    assert _normalize_service_name("worker-queue") == "worker"
    assert _normalize_service_name("Custom Service Name") == "custom-service-name"
    assert _normalize_service_name("custom--test---service") == "custom-test-service"
    assert _normalize_service_name("---!@#$---") == "edge-gateway"


def test_map_severity() -> None:
    assert _map_severity(explicit="critical") == "critical"
    assert _map_severity(explicit="error") == "error"
    assert _map_severity(explicit="warning") == "warning"
    assert _map_severity(priority="P1") == "critical"
    assert _map_severity(priority="P2") == "critical"
    assert _map_severity(priority="P3") == "error"
    assert _map_severity(priority="P4") == "warning"
    assert _map_severity(priority="P5") == "warning"
    assert _map_severity(urgency="high") == "critical"
    assert _map_severity(urgency="low") == "warning"
    assert _map_severity() == "critical"


def test_parse_timestamp() -> None:
    clock = FrozenClock()
    assert _parse_timestamp(None, clock) == clock.now()
    assert _parse_timestamp("invalid-date", clock) == clock.now()

    iso_z = "2026-09-13T09:00:00Z"
    dt1 = _parse_timestamp(iso_z, clock)
    assert dt1.tzinfo is not None

    iso_no_tz = "2026-09-13T09:00:00"
    dt2 = _parse_timestamp(iso_no_tz, clock)
    assert dt2.tzinfo == UTC


def test_parse_pagerduty_webhook_v3_trigger() -> None:
    clock = FrozenClock()
    payload = {
        "event": {
            "id": "evt_001",
            "event_type": "incident.triggered",
            "occurred_at": "2026-09-13T09:00:00Z",
            "data": {
                "id": "PD123",
                "title": "High latency on edge-gateway",
                "service": {"summary": "edge-gateway"},
                "urgency": "high",
                "priority": {"summary": "P1"},
            },
        }
    }

    alert = parse_pagerduty_webhook(payload, clock=clock)
    assert alert is not None
    assert alert.source == "pagerduty"
    assert alert.alert_id == "alt_pd_PD123"
    assert alert.title == "High latency on edge-gateway"
    assert alert.service == "edge-gateway"
    assert alert.severity == "critical"


def test_parse_pagerduty_webhook_v3_reopened_string_service() -> None:
    clock = FrozenClock()
    payload = {
        "event": {
            "id": "evt_002",
            "event_type": "incident.reopened",
            "data": {
                "id": "alt_custom_id",
                "title": "Worker queue stalled",
                "service": "worker",
                "urgency": "low",
                "priority": "P4",
            },
        }
    }

    alert = parse_pagerduty_webhook(payload, clock=clock)
    assert alert is not None
    assert alert.alert_id == "alt_custom_id"
    assert alert.service == "worker"
    assert alert.severity == "warning"


def test_parse_pagerduty_webhook_v3_non_trigger() -> None:
    payload = {
        "event": {
            "event_type": "incident.resolved",
            "data": {"id": "PD123"},
        }
    }
    assert parse_pagerduty_webhook(payload) is None


def test_parse_pagerduty_webhook_v2() -> None:
    clock = FrozenClock()
    payload = {
        "messages": [
            "non-dict",
            {"event": "incident.resolve"},
            {
                "event": "incident.trigger",
                "created_on": "2026-09-13T09:00:00Z",
                "incident": {
                    "id": "PD_V2_456",
                    "title": "Auth error spike",
                    "service": {"name": "auth-service"},
                    "urgency": "high",
                },
            },
        ]
    }

    alert = parse_pagerduty_webhook(payload, clock=clock)
    assert alert is not None
    assert alert.alert_id == "alt_pd_PD_V2_456"
    assert alert.service == "auth-service"
    assert alert.severity == "critical"


def test_parse_pagerduty_webhook_v2_empty_or_non_trigger() -> None:
    payload = {"messages": [{"event": "incident.resolve"}]}
    assert parse_pagerduty_webhook(payload) is None


def test_parse_pagerduty_webhook_direct() -> None:
    clock = FrozenClock()
    payload = {
        "alert_id": "custom_1",
        "title": "Direct test alert",
        "service": "data-service",
        "severity": "error",
    }
    alert = parse_pagerduty_webhook(payload, clock=clock)
    assert alert is not None
    assert alert.alert_id == "alt_custom_1"
    assert alert.service == "data-service"
    assert alert.severity == "error"


def test_parse_pagerduty_webhook_unrecognized() -> None:
    assert parse_pagerduty_webhook({"unrelated": "data"}) is None


@pytest.mark.asyncio
async def test_pagerduty_alert_source() -> None:
    clock = FrozenClock()
    source = PagerDutyAlertSource(clock=clock)
    assert isinstance(source, AlertSource)
    assert source.empty() is True
    assert source.qsize() == 0

    alert = Alert(
        alert_id="alt_test_1",
        source="synthetic",
        title="Test synthetic",
        service="edge-gateway",
        severity="critical",
        fired_at=clock.now(),
        raw={},
    )
    await source.inject_synthetic_alert(alert)

    assert source.empty() is False
    assert source.qsize() == 1

    received = await source.receive_alert()
    assert received == alert
    assert source.empty() is True


@pytest.mark.asyncio
async def test_webhook_app_health() -> None:
    source = PagerDutyAlertSource()
    app = create_webhook_app(alert_source=source, secret="sec123")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_webhook_app_hmac_and_ingest() -> None:
    secret = "secret_key"
    source = PagerDutyAlertSource()
    app = create_webhook_app(alert_source=source, secret=secret)

    payload = {
        "event": {
            "event_type": "incident.triggered",
            "data": {
                "id": "PD_APP_1",
                "title": "Webhook app incident",
                "service": {"summary": "edge-gateway"},
                "urgency": "high",
            },
        }
    }
    import json

    body_bytes = json.dumps(payload).encode("utf-8")
    sig = _compute_pd_sig(body_bytes, secret)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. Missing signature
        resp_missing = await client.post("/webhook", content=body_bytes)
        assert resp_missing.status_code == 401

        # 2. Bad signature
        resp_bad = await client.post(
            "/webhook",
            content=body_bytes,
            headers={"x-pagerduty-signature": "v1=invalid"},
        )
        assert resp_bad.status_code == 401

        # 3. Valid signature on /webhook
        resp_ok = await client.post(
            "/webhook",
            content=body_bytes,
            headers={"x-pagerduty-signature": sig},
        )
        assert resp_ok.status_code == 200
        assert resp_ok.json() == {"status": "accepted", "alert_id": "alt_pd_PD_APP_1"}
        assert source.qsize() == 1

        # 4. Valid signature on /
        resp_root = await client.post(
            "/",
            content=body_bytes,
            headers={"x-pagerduty-signature": sig},
        )
        assert resp_root.status_code == 200
        assert source.qsize() == 2

        # 5. Non-trigger event ignored
        ignore_payload = {"event": {"event_type": "incident.resolved"}}
        ignore_bytes = json.dumps(ignore_payload).encode("utf-8")
        ignore_sig = _compute_pd_sig(ignore_bytes, secret)
        resp_ignore = await client.post(
            "/webhook",
            content=ignore_bytes,
            headers={"x-pagerduty-signature": ignore_sig},
        )
        assert resp_ignore.status_code == 200
        assert resp_ignore.json()["status"] == "ignored"

        # 6. Bad JSON
        bad_json = b"not json"
        bad_sig = _compute_pd_sig(bad_json, secret)
        resp_bad_json = await client.post(
            "/webhook",
            content=bad_json,
            headers={"x-pagerduty-signature": bad_sig},
        )
        assert resp_bad_json.status_code == 400


@pytest.mark.asyncio
async def test_webhook_app_inject_endpoints() -> None:
    source = PagerDutyAlertSource()
    app = create_webhook_app(alert_source=source, secret="sec", require_signature=False)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Inject by scenario ID
        resp_scen = await client.post(
            "/api/alerts/inject",
            json={"scenario": "bad_deploy_data_service"},
        )
        assert resp_scen.status_code == 200
        assert resp_scen.json()["status"] == "injected"
        assert source.qsize() == 1

        # Inject raw alert payload on /alert/inject
        resp_raw = await client.post(
            "/alert/inject",
            json={
                "title": "Raw synthetic alert",
                "service": "worker",
                "severity": "error",
            },
        )
        assert resp_raw.status_code == 200
        assert resp_raw.json()["status"] == "injected"
        assert source.qsize() == 2

        # Invalid json to inject endpoint
        resp_bad = await client.post(
            "/api/alerts/inject",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp_bad.status_code == 400


@pytest.mark.asyncio
async def test_webhook_app_without_signature_check() -> None:
    source = PagerDutyAlertSource()
    app = create_webhook_app(alert_source=source, secret="sec", require_signature=False)

    payload = {
        "event": {
            "event_type": "incident.triggered",
            "data": {
                "id": "PD_NO_SIG",
                "title": "Incident without sig check",
                "service": "edge-gateway",
            },
        }
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/webhook", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_webhook_receiver_server_lifecycle() -> None:
    source = PagerDutyAlertSource()
    server = WebhookReceiverServer(
        alert_source=source,
        host="127.0.0.1",
        port=19108,
        secret="test_secret",
        require_signature=False,
    )

    async with server, httpx.AsyncClient(base_url="http://127.0.0.1:19108") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_parse_pagerduty_webhook_v2_string_service() -> None:
    clock = FrozenClock()
    payload = {
        "messages": [
            {
                "event": "incident.trigger",
                "created_on": "2026-09-13T09:00:00Z",
                "incident": {
                    "id": "PD_V2_STR",
                    "title": "Data degradation",
                    "service": "data-service",
                    "urgency": "low",
                },
            }
        ]
    }
    alert = parse_pagerduty_webhook(payload, clock=clock)
    assert alert is not None
    assert alert.service == "data-service"


@pytest.mark.asyncio
async def test_webhook_receiver_server_stop_without_start() -> None:
    source = PagerDutyAlertSource()
    server = WebhookReceiverServer(alert_source=source, require_signature=False)
    await server.stop()
    assert server._serve_task is None


@pytest.mark.asyncio
async def test_webhook_receiver_server_start_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    source = PagerDutyAlertSource()
    server = WebhookReceiverServer(alert_source=source, require_signature=False)

    async def _hanging_serve() -> None:
        # Never sets server.started and never completes, forcing the poll loop to
        # exhaust its retries.
        await asyncio.Event().wait()

    monkeypatch.setattr(server.server, "serve", _hanging_serve)

    real_sleep = asyncio.sleep

    async def _fast_sleep(_s: float) -> None:
        await real_sleep(0.001)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    with pytest.raises(TimeoutError, match="did not start"):
        await server.start()
    assert server._serve_task is not None
    server._serve_task.cancel()
    await server.stop()


@pytest.mark.asyncio
async def test_webhook_receiver_server_start_exits_before_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    source = PagerDutyAlertSource()
    server = WebhookReceiverServer(alert_source=source, require_signature=False)

    async def _failing_serve() -> None:
        raise OSError("Address already in use")

    monkeypatch.setattr(server.server, "serve", _failing_serve)

    real_sleep = asyncio.sleep

    async def _fast_sleep(_s: float) -> None:
        await real_sleep(0.001)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    with pytest.raises(OSError, match="Address already in use"):
        await server.start()
    await server.stop()


@pytest.mark.asyncio
async def test_webhook_receiver_server_start_exits_cleanly_without_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    source = PagerDutyAlertSource()
    server = WebhookReceiverServer(alert_source=source, require_signature=False)

    async def _noop_serve() -> None:
        return None

    monkeypatch.setattr(server.server, "serve", _noop_serve)

    real_sleep = asyncio.sleep

    async def _fast_sleep(_s: float) -> None:
        await real_sleep(0.001)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    with pytest.raises(RuntimeError, match="exited before starting"):
        await server.start()
    await server.stop()


@pytest.mark.asyncio
async def test_webhook_receiver_server_start_bind_failure_system_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    source = PagerDutyAlertSource()
    server = WebhookReceiverServer(alert_source=source, require_signature=False)

    async def _failing_serve() -> None:
        raise SystemExit(3)

    monkeypatch.setattr(server.server, "serve", _failing_serve)

    real_sleep = asyncio.sleep

    async def _fast_sleep(_s: float) -> None:
        await real_sleep(0.001)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    with pytest.raises(RuntimeError, match="Webhook server failed to start"):
        await server.start()
    await server.stop()
