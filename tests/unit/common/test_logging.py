"""Unit tests for structured JSON logging."""

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from understudy.common.logging import configure_logging, get_logger


def test_get_logger_binding(capsys: "pytest.CaptureFixture[str]") -> None:
    configure_logging(log_level="DEBUG")
    logger = get_logger(incident_id="inc_test_123", extra_key="extra_val")
    logger.info("twin_forked", twin_id="twin_1")

    captured = capsys.readouterr()
    log_line = captured.out.strip()
    data = json.loads(log_line)

    assert data["event"] == "twin_forked"
    assert data["incident_id"] == "inc_test_123"
    assert data["extra_key"] == "extra_val"
    assert data["twin_id"] == "twin_1"
    assert data["level"] == "info"
    assert "timestamp" in data


def test_get_logger_unbound(capsys: "pytest.CaptureFixture[str]") -> None:
    configure_logging(log_level="INFO")
    logger = get_logger()
    logger.info("system_booted")

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "system_booted"
    assert "incident_id" not in data


def test_configure_logging_custom_stream() -> None:
    import io

    stream = io.StringIO()
    try:
        configure_logging(log_level="INFO", file=stream)
        logger = get_logger(incident_id="inc_stderr")
        logger.info("custom_stream_event")

        data = json.loads(stream.getvalue().strip())
        assert data["event"] == "custom_stream_event"
        assert data["incident_id"] == "inc_stderr"
    finally:
        configure_logging(log_level="INFO")
