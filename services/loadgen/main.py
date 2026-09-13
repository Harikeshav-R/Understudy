"""CLI entry point for Understudy load generator."""

import argparse
import asyncio
import sys

from services._common.settings import load_services_settings
from services.loadgen.runner import LoadgenConfig, run_loadgen


def parse_args(args: list[str] | None = None) -> LoadgenConfig:
    """Parse CLI arguments, defaulting to a fresh load_services_settings().loadgen --
    the same source LoadgenConfig itself defaults from -- so a flag omitted here and a
    LoadgenConfig() constructed directly can never silently drift apart. Loaded fresh
    (not the cached singleton) so an env var set right before invoking the CLI is
    always honored."""
    loadgen_defaults = load_services_settings().loadgen
    parser = argparse.ArgumentParser(
        prog="loadgen",
        description="Seeded deterministic load generator for Understudy demo stack",
    )
    parser.add_argument(
        "--rps",
        type=float,
        default=loadgen_defaults.rps,
        help=f"Requests per second (default: {loadgen_defaults.rps})",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=loadgen_defaults.duration_seconds,
        help=f"Run duration in seconds (default: {loadgen_defaults.duration_seconds})",
    )
    parser.add_argument(
        "--target-url",
        type=str,
        default=loadgen_defaults.target_url,
        help=f"Target base URL (default: {loadgen_defaults.target_url})",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=loadgen_defaults.seed,
        help=f"Random seed for request sequence (default: {loadgen_defaults.seed})",
    )
    parser.add_argument(
        "--auth-token",
        type=str,
        default=loadgen_defaults.auth_token,
        help=f"Authentication bearer token to use (default: {loadgen_defaults.auth_token})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=loadgen_defaults.http_timeout_seconds,
        help=f"HTTP request timeout in seconds (default: {loadgen_defaults.http_timeout_seconds})",
    )

    parsed = parser.parse_args(args)
    return LoadgenConfig(
        rps=parsed.rps,
        duration_seconds=parsed.duration,
        target_url=parsed.target_url,
        seed=parsed.seed,
        auth_token=parsed.auth_token,
        http_timeout_seconds=parsed.timeout,
    )


def main(args: list[str] | None = None) -> int:
    """Execute loadgen and print the summary line."""
    config = parse_args(args)
    metrics = asyncio.run(run_loadgen(config))
    print(metrics.summary_line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
