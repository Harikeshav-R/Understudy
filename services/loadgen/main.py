"""CLI entry point for Understudy load generator."""

import argparse
import asyncio
import os
import sys

from services.loadgen.runner import LoadgenConfig, run_loadgen


def parse_args(args: list[str] | None = None) -> LoadgenConfig:
    """Parse CLI arguments with environment variable fallbacks."""
    parser = argparse.ArgumentParser(
        prog="loadgen",
        description="Seeded deterministic load generator for Understudy demo stack",
    )
    parser.add_argument(
        "--rps",
        type=float,
        default=float(os.getenv("LOADGEN_RPS", "20.0")),
        help="Requests per second (default: 20.0)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=float(os.getenv("LOADGEN_DURATION", "30.0")),
        help="Run duration in seconds (default: 30.0)",
    )
    parser.add_argument(
        "--target-url",
        type=str,
        default=os.getenv("LOADGEN_TARGET_URL", "http://localhost:8080"),
        help="Target base URL (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=int(os.getenv("LOADGEN_SEED", "42")),
        help="Random seed for request sequence (default: 42)",
    )
    parser.add_argument(
        "--auth-token",
        type=str,
        default=os.getenv("LOADGEN_AUTH_TOKEN", "valid-token"),
        help="Authentication bearer token to use (default: valid-token)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("LOADGEN_TIMEOUT", "10.0")),
        help="HTTP request timeout in seconds (default: 10.0)",
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
