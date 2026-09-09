"""Unit tests for Understudy CLI entrypoint and package metadata."""

from typer.testing import CliRunner

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
