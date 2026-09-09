"""Unit tests for Understudy CLI entrypoint and package metadata."""

from typing import TYPE_CHECKING

from typer.testing import CliRunner

if TYPE_CHECKING:
    import pytest

import understudy
from understudy.cli import app

runner = CliRunner()


def test_package_version() -> None:
    """Ensure __version__ is defined."""
    assert understudy.__version__ == "0.1.0"


def test_cli_version() -> None:
    """Ensure ust version outputs expected version string."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "understudy 0.1.0" in result.stdout


def test_cli_help() -> None:
    """Ensure ust --help works and describes the tool."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Understudy" in result.stdout


def test_cli_doctor(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Ensure ust doctor runs and exits with status from run_doctor."""
    import understudy.doctor

    monkeypatch.setattr(understudy.doctor, "run_doctor", lambda: 0)
    res_ok = runner.invoke(app, ["doctor"])
    assert res_ok.exit_code == 0

    monkeypatch.setattr(understudy.doctor, "run_doctor", lambda: 1)
    res_fail = runner.invoke(app, ["doctor"])
    assert res_fail.exit_code == 1
