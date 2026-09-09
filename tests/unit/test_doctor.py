"""Unit tests for ust doctor preflight checks."""

import io
import shutil
import subprocess
import sys

import pytest

from understudy.common.config import reset_settings
from understudy.doctor import (
    TWELVE_GB_BYTES,
    check_docker_memory,
    check_python_version,
    check_secret,
    check_tool_present,
    run_doctor,
)


def test_check_python_version(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real Python 3.12
    res = check_python_version()
    assert res.passed is True
    assert "3.12" in res.message

    # Simulated Python 3.11
    monkeypatch.setattr(sys, "version_info", (3, 11, 5))
    res_bad = check_python_version()
    assert res_bad.passed is False
    assert "required 3.12" in res_bad.message


def test_check_tool_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/" + cmd if cmd == "uv" else None)
    res_uv = check_tool_present("uv")
    assert res_uv.passed is True
    assert "/usr/bin/uv" in res_uv.message

    res_missing = check_tool_present("nonexistent_tool")
    assert res_missing.passed is False
    assert "not found in PATH" in res_missing.message


def test_check_docker_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. docker CLI missing
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
    res_no_docker = check_docker_memory()
    assert res_no_docker.passed is False
    assert "not found in PATH" in res_no_docker.message

    # 2. docker CLI present, command succeeds with >= 12GB
    monkeypatch.setattr(shutil, "which", lambda _cmd: "/usr/bin/docker")

    def mock_run_ok(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["docker"], returncode=0, stdout=str(TWELVE_GB_BYTES + 1024), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", mock_run_ok)
    res_ok = check_docker_memory()
    assert res_ok.passed is True
    assert ">= 12 GB" in res_ok.message

    # 3. docker memory < 12GB
    def mock_run_low(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["docker"], returncode=0, stdout=str(8 * 1024 * 1024 * 1024), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", mock_run_low)
    res_low = check_docker_memory()
    assert res_low.passed is False
    assert "< 12 GB" in res_low.message

    # 4. docker info fails with non-zero exit
    def mock_run_err(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["docker"], returncode=1, stdout="", stderr="daemon not running"
        )

    monkeypatch.setattr(subprocess, "run", mock_run_err)
    res_err = check_docker_memory()
    assert res_err.passed is False
    assert "docker info failed" in res_err.message

    # 5. subprocess.run raises exception
    def mock_run_exc(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("Subprocess crashed")

    monkeypatch.setattr(subprocess, "run", mock_run_exc)
    res_exc = check_docker_memory()
    assert res_exc.passed is False
    assert "Failed to inspect Docker daemon" in res_exc.message


def test_check_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_settings()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    res_missing = check_secret("OPENROUTER_API_KEY", "openrouter_api_key", required=True)
    assert res_missing.passed is False
    assert "not set" in res_missing.message

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    reset_settings()
    res_set = check_secret("OPENROUTER_API_KEY", "openrouter_api_key", required=True)
    assert res_set.passed is True
    assert "configured" in res_set.message


def test_run_doctor_all_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _cmd: "/usr/local/bin/test")
    monkeypatch.setattr(sys, "version_info", (3, 12, 0))

    def mock_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["docker"], returncode=0, stdout=str(14 * 1024 * 1024 * 1024), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", mock_run)

    for env_name in [
        "OPENROUTER_API_KEY",
        "GITHUB_TOKEN",
        "SLACK_BOT_TOKEN",
        "PAGERDUTY_ROUTING_KEY",
        "PAGERDUTY_WEBHOOK_SECRET",
    ]:
        monkeypatch.setenv(env_name, "test_val")
    reset_settings()

    out = io.StringIO()
    code = run_doctor(output_stream=out)
    assert code == 0
    assert "All preflight lines OK." in out.getvalue()


def test_run_doctor_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    # Missing all tools and secrets
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
    for env_name in [
        "OPENROUTER_API_KEY",
        "GITHUB_TOKEN",
        "SLACK_BOT_TOKEN",
        "PAGERDUTY_ROUTING_KEY",
        "PAGERDUTY_WEBHOOK_SECRET",
    ]:
        monkeypatch.delenv(env_name, raising=False)
    reset_settings()

    out = io.StringIO()
    code = run_doctor(output_stream=out)
    assert code == 1
    assert "Preflight failed." in out.getvalue()
