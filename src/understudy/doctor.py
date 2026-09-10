"""Environment preflight checks for Understudy."""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import TextIO

from understudy.common.config import get_settings

REQUIRED_SECRETS = [
    ("OPENROUTER_API_KEY", "openrouter_api_key"),
    ("GITHUB_TOKEN", "github_token"),
    ("SLACK_BOT_TOKEN", "slack_bot_token"),
    ("PAGERDUTY_ROUTING_KEY", "pagerduty_routing_key"),
    ("PAGERDUTY_WEBHOOK_SECRET", "pagerduty_webhook_secret"),
]

OPTIONAL_SECRETS = [
    ("DATADOG_API_KEY", "datadog_api_key"),
]

TWELVE_GB_BYTES = 12 * 1000 * 1000 * 1000  # 12 GB decimal


@dataclass(frozen=True)
class CheckResult:
    """Individual preflight check assertion result."""

    name: str
    passed: bool
    message: str
    required: bool = True


def check_python_version() -> CheckResult:
    """Verify Python 3.12 runtime."""
    major, minor = sys.version_info[0], sys.version_info[1]
    version_str = ".".join(str(x) for x in sys.version_info[:3])
    if (major, minor) == (3, 12):
        return CheckResult(
            name="python",
            passed=True,
            message=f"Python 3.12 ({version_str})",
        )
    return CheckResult(
        name="python",
        passed=False,
        message=f"Found Python {version_str}, required 3.12",
    )


def check_tool_present(tool_name: str) -> CheckResult:
    """Verify presence of a required CLI tool in PATH."""
    path = shutil.which(tool_name)
    if path:
        return CheckResult(
            name=f"tool:{tool_name}",
            passed=True,
            message=f"{tool_name} found at {path}",
        )
    return CheckResult(
        name=f"tool:{tool_name}",
        passed=False,
        message=f"{tool_name} not found in PATH",
    )


def check_docker_memory() -> CheckResult:
    """Verify Docker Desktop memory allocation >= 12 GB."""
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return CheckResult(
            name="docker_memory",
            passed=False,
            message="docker command not found in PATH",
        )

    try:
        proc = subprocess.run(
            [docker_bin, "info", "--format", "{{.MemTotal}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if proc.returncode != 0:
            return CheckResult(
                name="docker_memory",
                passed=False,
                message=f"docker info failed: {proc.stderr.strip()}",
            )
        mem_bytes = int(proc.stdout.strip())
        mem_gb = mem_bytes / (1000 * 1000 * 1000)
        if mem_bytes >= TWELVE_GB_BYTES:
            return CheckResult(
                name="docker_memory",
                passed=True,
                message=f"Docker memory {mem_gb:.1f} GB >= 12 GB",
            )
        return CheckResult(
            name="docker_memory",
            passed=False,
            message=f"Docker memory {mem_gb:.1f} GB < 12 GB (ADR-003 requires >= 12 GB)",
        )
    except Exception as exc:
        return CheckResult(
            name="docker_memory",
            passed=False,
            message=f"Failed to inspect Docker daemon: {exc}",
        )


def check_secret(env_name: str, field_name: str, required: bool = True) -> CheckResult:
    """Verify a secret is configured in environment, settings, or keyring."""
    settings = get_settings()
    val = getattr(settings.secrets, field_name, None) or os.environ.get(env_name)
    if not val:
        try:
            import importlib

            keyring_mod = importlib.import_module("keyring")
            keyring_val = keyring_mod.get_password("understudy", env_name)
            if isinstance(keyring_val, str):
                val = keyring_val
        except Exception:
            val = None
    if val:
        return CheckResult(
            name=f"secret:{env_name}",
            passed=True,
            message=f"{env_name} is configured",
            required=required,
        )
    return CheckResult(
        name=f"secret:{env_name}",
        passed=False,
        message=f"{env_name} is not set",
        required=required,
    )


def run_all_checks() -> list[CheckResult]:
    """Execute all preflight environment assertions."""
    results: list[CheckResult] = []

    # 1. Python version
    results.append(check_python_version())

    # 2. CLI tools
    for tool in ["uv", "kubectl", "k3d"]:
        results.append(check_tool_present(tool))

    # 3. Docker memory
    results.append(check_docker_memory())

    # 4. Required secrets
    for env_name, field_name in REQUIRED_SECRETS:
        results.append(check_secret(env_name, field_name, required=True))

    # 5. Optional secrets
    for env_name, field_name in OPTIONAL_SECRETS:
        results.append(check_secret(env_name, field_name, required=False))

    return results


def run_doctor(output_stream: TextIO = sys.stdout) -> int:
    """Run all preflight checks, print output, and return exit code."""
    results = run_all_checks()

    all_passed = True
    output_stream.write("Understudy Preflight Doctor\n")
    output_stream.write("===========================\n")

    for res in results:
        status = "OK" if res.passed else ("WARN" if not res.required else "FAIL")
        if not res.passed and res.required:
            all_passed = False
        output_stream.write(f"[{status:4}] {res.name:<25}: {res.message}\n")

    output_stream.write("---------------------------\n")
    if all_passed:
        output_stream.write("All preflight lines OK.\n")
        return 0

    output_stream.write("Preflight failed. Address the FAIL items above.\n")
    return 1
