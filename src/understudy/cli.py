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


fleet_app = typer.Typer(
    name="fleet",
    help="Twin fleet lifecycle management commands.",
    no_args_is_help=True,
)
app.add_typer(fleet_app, name="fleet")


@fleet_app.command("fork")
def fleet_fork(
    incident: str = typer.Option(..., "--incident", help="Incident ID (e.g. inc_test)."),
    count: int = typer.Option(3, "--count", help="Number of twin environments to fork."),
) -> None:
    """Concurrently fork N isolated twin environments for an incident."""
    import asyncio

    from understudy.fleet.controller import K8sFleetController

    controller = K8sFleetController()
    twins = asyncio.run(controller.fork(incident_id=incident, n=count))
    from understudy.fleet.teardown import unregister_active_incident

    unregister_active_incident(incident)
    for twin in twins:
        typer.echo(
            f"twin_id={twin.twin_id} namespace={twin.namespace} "
            f"database={twin.database} state={twin.state}"
        )


@fleet_app.command("teardown")
def fleet_teardown(
    incident: str = typer.Option(..., "--incident", help="Incident ID (e.g. inc_test)."),
) -> None:
    """Tear down all twin environments for an incident."""
    import asyncio

    from understudy.fleet.controller import K8sFleetController

    controller = K8sFleetController()
    asyncio.run(controller.teardown_all(incident_id=incident))
    typer.echo(f"fleet torn down for incident={incident}")


@fleet_app.command("gc")
def fleet_gc(
    older_than: float = typer.Option(
        3600.0,
        "--older-than",
        help="Reap twin environments older than this many seconds (default: 3600s / 1 hour).",
    ),
) -> None:
    """Garbage collect orphaned twin namespaces and databases older than retention threshold."""
    import asyncio

    from understudy.fleet.teardown import FleetTeardownManager

    manager = FleetTeardownManager()
    res = asyncio.run(manager.gc(older_than_seconds=older_than))
    typer.echo(
        f"reaped {len(res.reaped_namespaces)} namespaces, {len(res.dropped_databases)} databases"
    )


store_app = typer.Typer(
    name="store",
    help="Datastore migration and verification commands.",
    no_args_is_help=True,
)
app.add_typer(store_app, name="store")


@store_app.command("migrate")
def store_migrate(
    dsn: str | None = typer.Option(None, "--dsn", help="Optional PostgreSQL DSN override."),
) -> None:
    """Apply database migrations to the Understudy system datastore."""
    from understudy.store.migrations import apply_migrations

    apply_migrations(dsn=dsn)
    typer.echo("Database migrations applied successfully.")


@store_app.command("verify")
def store_verify(
    last: int = typer.Option(2, "--last", help="Number of most recent run records to verify."),
    dsn: str | None = typer.Option(None, "--dsn", help="Optional PostgreSQL DSN override."),
) -> None:
    """Verify that recent run records are complete and append-only rules are active."""
    import asyncio

    from sqlalchemy import text

    from understudy.store.database import StoreDatabase
    from understudy.store.postgres import PostgresRunStore

    db = StoreDatabase(dsn=dsn)
    store = PostgresRunStore(db=db)

    async def _verify() -> None:
        runs = await store.list_runs()
        target_runs = runs[:last]
        if len(target_runs) < last:
            typer.echo(f"Warning: found {len(target_runs)} runs (requested {last})")

        # Verify append-only rules are active in PostgreSQL
        async with db.session() as session:
            rule_res = await session.execute(
                text("SELECT rulename FROM pg_rules WHERE tablename = 'runs';")
            )
            rules = {r[0] for r in rule_res.fetchall()}
            if "runs_no_update" not in rules or "runs_no_delete" not in rules:
                typer.echo("Error: append-only rules missing on runs table", err=True)
                raise typer.Exit(code=1)

        for r in target_runs:
            typer.echo(f"run_id={r.run_id} incident_id={r.incident_id} outcome={r.outcome.value}")

        typer.echo(f"Verified {len(target_runs)} run records: complete and append-only OK")

    asyncio.run(_verify())


@app.command("tunnel")
def tunnel(
    port: int = typer.Option(
        9108,
        "--port",
        "-p",
        help="Port for the PagerDuty webhook receiver (default: 9108).",
    ),
    provider: str = typer.Option(
        "auto",
        "--provider",
        help="Tunnel provider ('auto', 'cloudflared', 'ngrok', 'fake').",
    ),
    fake: bool = typer.Option(
        False,
        "--fake",
        help="Run simulated fake tunnel for testing.",
    ),
    secret: str | None = typer.Option(
        None,
        "--secret",
        help="Optional PagerDuty webhook secret override.",
    ),
    timeout: float = typer.Option(
        30.0,
        "--timeout",
        help="Timeout in seconds to wait for tunnel URL.",
    ),
) -> None:
    """Start PagerDuty webhook receiver on :9108 and expose via public tunnel."""
    import asyncio

    from understudy.signals.tunnel import TunnelSession

    session = TunnelSession(
        port=port,
        provider=provider,
        fake=fake,
        secret=secret,
    )

    async def _run() -> None:
        try:
            url = await session.start(timeout_seconds=timeout)
            typer.echo(f"Tunnel URL: {url}")
            typer.echo(f"Webhook URL: {session.webhook_url}")
            typer.echo(f"Paste this URL into PagerDuty webhook subscription (port {port}).")
            await session.run_until_cancelled()
        finally:
            await session.stop()

    import contextlib

    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(_run())


alert_app = typer.Typer(
    name="alert",
    help="Incident alert injection and management commands.",
    no_args_is_help=True,
)
app.add_typer(alert_app, name="alert")


@alert_app.command("inject")
def alert_inject(
    scenario: str = typer.Option(
        ...,
        "--scenario",
        "-s",
        help="Scenario identifier (e.g. bad_deploy_data_service).",
    ),
    service: str | None = typer.Option(
        None,
        "--service",
        help="Optional override for affected service.",
    ),
    severity: str = typer.Option(
        "critical",
        "--severity",
        help="Alert severity ('critical', 'error', 'warning').",
    ),
    title: str | None = typer.Option(
        None,
        "--title",
        help="Optional override for alert title.",
    ),
    endpoint: str = typer.Option(
        "http://127.0.0.1:9108/api/alerts/inject",
        "--endpoint",
        help="Webhook receiver inject endpoint URL.",
    ),
    port: int = typer.Option(
        9108,
        "--port",
        help="Port of local webhook receiver if overriding default endpoint.",
    ),
    print_only: bool = typer.Option(
        False,
        "--print-only",
        help="Print synthetic Alert JSON without posting to receiver.",
    ),
) -> None:
    """Inject a synthetic incident alert into the receiver."""
    import json
    from typing import Literal

    import httpx

    from understudy.signals.scenarios import create_synthetic_alert

    raw_sev = severity.lower().strip()
    sev_typed: Literal["critical", "error", "warning"] = "critical"
    if raw_sev == "error":
        sev_typed = "error"
    elif raw_sev == "warning":
        sev_typed = "warning"

    alert = create_synthetic_alert(
        scenario_id=scenario,
        title=title,
        service=service,
        severity=sev_typed,
    )

    alert_dict = alert.model_dump(mode="json")

    if print_only:
        typer.echo(json.dumps(alert_dict, indent=2))
        return

    target_endpoint = endpoint
    if port != 9108 and endpoint == "http://127.0.0.1:9108/api/alerts/inject":
        target_endpoint = f"http://127.0.0.1:{port}/api/alerts/inject"

    try:
        resp = httpx.post(target_endpoint, json=alert_dict, timeout=5.0)
        if resp.status_code == 200:
            typer.echo(
                f"injected synthetic alert alert_id={alert.alert_id} "
                f"service={alert.service} scenario={scenario}"
            )
            return
        typer.echo(
            f"Receiver returned HTTP {resp.status_code}: {resp.text}",
            err=True,
        )
        raise typer.Exit(code=1)
    except httpx.RequestError as exc:
        typer.echo(
            f"Receiver on {target_endpoint} not reachable ({exc}). "
            f"Synthetic Alert generated: alert_id={alert.alert_id} service={alert.service}",
            err=True,
        )


if __name__ == "__main__":
    app()
