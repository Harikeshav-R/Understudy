"""Understudy command-line interface entrypoint."""

from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    name="ust",
    help=(
        "Understudy: autonomous incident response with twin rehearsal "
        "and safety kernel verification."
    ),
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Understudy: autonomous incident response with twin rehearsal."""


@app.command()
def version() -> None:
    """Print the Understudy version."""
    typer.echo("understudy 0.1.0")


@app.command()
def doctor() -> None:
    """Run environment preflight checks (Python 3.12, tools, Docker RAM, secrets)."""
    from understudy.doctor import run_doctor

    code = run_doctor()
    if code != 0:
        raise typer.Exit(code=code)


@app.command()
def demo(
    fake: bool = typer.Option(
        False,
        "--fake",
        help="Run demo using deterministic component fakes (Phase 1B).",
    ),
    seed: int = typer.Option(
        42,
        "--seed",
        help="Random seed for deterministic component fakes.",
    ),
    force_veto: bool = typer.Option(
        False,
        "--force-veto",
        help="Force safety kernel to issue a VETO verdict on invariant K3.",
    ),
) -> None:
    """Run the incident control loop demo."""
    if not fake:
        typer.echo(
            "Error: --fake flag is required (live demo is implemented in Phase 7).",
            err=True,
        )
        raise typer.Exit(code=1)

    import asyncio
    import sys

    from understudy.common.logging import configure_logging
    from understudy.contracts.enums import RunOutcome
    from understudy.orchestrator.api import run_demo

    # Route structured JSON logs to stderr so stdout presents node transitions
    # and terminal outcome cleanly.
    try:
        configure_logging(log_level="INFO", file=sys.stderr)
        transitions, record = asyncio.run(run_demo(seed=seed, force_veto=force_veto))
    finally:
        configure_logging(log_level="INFO")

    typer.echo(" -> ".join(transitions))
    if record.outcome == RunOutcome.EXECUTED:
        typer.echo(f"outcome=executed plan={record.prod_applied_plan_id}")
    elif record.outcome == RunOutcome.ESCALATED:
        reason = "K3"
        if record.verdict is not None:
            inv = next((r for r in record.verdict.results if not r.satisfied), None)
            if inv is not None:
                reason = f"{inv.invariant_id} {inv.reason}"
            else:
                reason = record.verdict.human_reason or "K3"
        typer.echo(f"outcome=escalated reason={reason}")
    else:
        typer.echo(f"outcome={record.outcome.value}")


@app.command()
def graph(
    render: Annotated[
        Path | None,
        typer.Option(
            "--render",
            "-r",
            help="Render control loop graph diagram (PNG) to path (e.g. docs/generated/graph.png).",
        ),
    ] = None,
) -> None:
    """Inspect or render the control loop StateGraph."""
    from understudy.orchestrator.api import render_graph_mermaid, render_graph_png

    if render is not None:
        render_graph_png(output_path=render)
        typer.echo(f"Rendered control loop graph to {render}")
    else:
        mermaid_code = render_graph_mermaid()
        typer.echo(mermaid_code)


if __name__ == "__main__":
    app()
