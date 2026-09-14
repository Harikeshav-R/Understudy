"""Understudy command-line interface entrypoint."""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from understudy.common.logging import get_logger

if TYPE_CHECKING:
    from understudy.planner.api import Planner
    from understudy.signals.api import ObservabilityAdapter

logger = get_logger(__name__)

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
    with_datadog: bool = typer.Option(
        False,
        "--with-datadog",
        help="Run demo with Datadog observability telemetry.",
    ),
) -> None:
    """Run the incident control loop demo."""
    if with_datadog:
        typer.echo("Datadog telemetry mirroring enabled (ADR-025 optics).")
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


graph_app = typer.Typer(
    name="graph",
    help="Control loop graph rendering and service dependency DAG inspection.",
    invoke_without_command=True,
)
app.add_typer(graph_app, name="graph")


@graph_app.callback(invoke_without_command=True)
def graph_default(
    ctx: typer.Context,
    render: Annotated[
        Path | None,
        typer.Option(
            "--render",
            "-r",
            help="Render control loop graph diagram (PNG) to path (e.g. docs/generated/graph.png).",
        ),
    ] = None,
) -> None:
    """Inspect or render the control loop StateGraph when called without subcommands."""
    if ctx.invoked_subcommand is not None:
        return

    from understudy.orchestrator.api import render_graph_mermaid, render_graph_png

    if render is not None:
        render_graph_png(output_path=render)
        typer.echo(f"Rendered control loop graph to {render}")
    else:
        mermaid_code = render_graph_mermaid()
        typer.echo(mermaid_code)


@graph_app.command("show")
def graph_show(
    dependencies_file: Annotated[
        Path,
        typer.Option(
            "--dependencies-file",
            "-f",
            help="Path to dependencies.yaml file.",
        ),
    ] = Path("deploy/prod/dependencies.yaml"),
    prometheus_url: Annotated[
        str | None,
        typer.Option(
            "--prometheus-url",
            help="Prometheus base URL (defaults to settings).",
        ),
    ] = None,
    namespace: Annotated[
        str,
        typer.Option(
            "--namespace",
            "-n",
            help="Kubernetes namespace to check traffic for.",
        ),
    ] = "ust-prod",
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help="Skip Prometheus traffic observation check.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Output graph topology and cross-check report as JSON.",
        ),
    ] = False,
) -> None:
    """Display the declared dependency DAG and cross-check against observed traffic."""
    import asyncio
    import json

    from understudy.common.errors import GraphError
    from understudy.graph.service_graph import ServiceDependencyGraph

    try:
        dep_graph = ServiceDependencyGraph.from_yaml(dependencies_file)
    except GraphError as exc:
        typer.echo(f"Error loading dependency graph: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    chains_str = dep_graph.format_chains()

    report = None
    if not offline:
        try:
            report = asyncio.run(
                dep_graph.cross_check_prometheus(
                    prometheus_url=prometheus_url,
                    namespace=namespace,
                    raise_on_mismatch=False,
                )
            )
        except Exception as exc:
            typer.echo(f"Error cross-checking observed traffic: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        if report.status != "OK":
            typer.echo(f"Error: {report.message}", err=True)
            raise typer.Exit(code=1)

    if json_output:
        out = {
            "chains": chains_str,
            "nodes": dep_graph.snapshot().nodes,
            "edges": [{"source": e.source, "target": e.target} for e in dep_graph.snapshot().edges],
            "cross_check": (
                {
                    "status": report.status,
                    "declared_services": sorted(report.declared_services),
                    "observed_services": sorted(report.observed_services),
                    "declared_edges": [
                        {"source": s, "target": t} for s, t in sorted(report.declared_edges)
                    ],
                    "observed_edges": [
                        {"source": s, "target": t} for s, t in sorted(report.observed_edges)
                    ],
                    "missing_services": sorted(report.missing_services),
                    "missing_edges": [
                        {"source": s, "target": t} for s, t in sorted(report.missing_edges)
                    ],
                    "message": report.message,
                }
                if report
                else {"status": "SKIPPED"}
            ),
        }
        typer.echo(json.dumps(out, indent=2))
    else:
        typer.echo(chains_str)
        if report:
            typer.echo(report.message)
            typer.echo("declared matches observed: OK")
        else:
            typer.echo("declared graph traffic check: SKIPPED (offline)")


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

    try:
        session = TunnelSession(
            port=port,
            provider=provider,
            fake=fake,
            secret=secret,
        )
    except ValueError as exc:
        typer.echo(f"Error starting webhook receiver: {exc}", err=True)
        raise typer.Exit(code=1) from exc

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
        try:
            asyncio.run(_run())
        except (RuntimeError, TimeoutError) as exc:
            typer.echo(f"Error starting webhook receiver: {exc}", err=True)
            raise typer.Exit(code=1) from exc


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


signals_app = typer.Typer(
    name="signals",
    help="Telemetry, metrics, and incident context commands.",
    no_args_is_help=True,
)
app.add_typer(signals_app, name="signals")


@signals_app.command("context")
def signals_context(
    service: str = typer.Option(
        "data-service",
        "--service",
        "-s",
        help="Target microservice name (e.g. data-service).",
    ),
    minutes: int = typer.Option(
        10,
        "--minutes",
        "-m",
        help="Incident context telemetry window in minutes.",
    ),
    namespace: str = typer.Option(
        "ust-prod",
        "--namespace",
        "-n",
        help="Kubernetes namespace to query (default: ust-prod).",
    ),
    incident_id: str | None = typer.Option(
        None,
        "--incident-id",
        help="Optional incident ID override.",
    ),
    json_output: bool = typer.Option(
        True,
        "--json/--no-json",
        help="Print full IncidentContext JSON.",
    ),
    prometheus_url: str | None = typer.Option(
        None,
        "--prometheus-url",
        help="Optional Prometheus base URL override.",
    ),
    loki_url: str | None = typer.Option(
        None,
        "--loki-url",
        help="Optional Loki base URL override.",
    ),
    adapter: str = typer.Option(
        "prometheus",
        "--adapter",
        "-a",
        help="Telemetry adapter to use: prometheus (default) or datadog.",
    ),
    datadog_site: str | None = typer.Option(
        None,
        "--datadog-site",
        help="Optional Datadog site override (e.g. datadoghq.com, datadoghq.eu).",
    ),
) -> None:
    """Fetch real Prometheus or Datadog metrics and error signatures into an IncidentContext."""
    import asyncio
    from datetime import timedelta

    from understudy.common.clock import SystemClock
    from understudy.common.ids import new_alert_id, new_incident_id
    from understudy.contracts.incident import (
        Alert,
        DependencyGraphSnapshot,
        IncidentContext,
    )

    clock = SystemClock()
    now = clock.now()
    window_minutes = max(1, minutes)
    since = now - timedelta(minutes=window_minutes)
    inc_id = incident_id or new_incident_id()

    obs_adapter: ObservabilityAdapter
    if adapter == "datadog":
        from understudy.signals.datadog import DatadogAdapter, DatadogClient

        dd_client = DatadogClient(site=datadog_site, clock=clock)
        obs_adapter = DatadogAdapter(datadog_client=dd_client, clock=clock)
    else:
        from understudy.signals.loki import LokiClient
        from understudy.signals.prometheus import PrometheusClient, PrometheusLokiAdapter

        prom_client = PrometheusClient(base_url=prometheus_url, clock=clock)
        loki_client = LokiClient(base_url=loki_url, clock=clock)
        obs_adapter = PrometheusLokiAdapter(
            prometheus_client=prom_client,
            loki_client=loki_client,
            clock=clock,
        )

    alert = Alert(
        alert_id=new_alert_id(),
        source="synthetic",
        title=f"Telemetry context query for {service} in {namespace}",
        service=service,
        severity="critical",
        fired_at=since,
        raw={"query_minutes": window_minutes, "namespace": namespace},
    )

    async def _gather() -> IncidentContext:
        try:
            metric_window = await obs_adapter.metric_window(
                service=service,
                since=since,
                namespace=namespace,
            )
            signatures = await obs_adapter.error_signatures(
                service=service,
                since=since,
                namespace=namespace,
            )
            from understudy.signals.github import GitHubDeployHistory

            github_history = GitHubDeployHistory()
            try:
                recent_deploys = await github_history.recent_deploys(limit=5)
            except Exception as exc:
                logger.warning("github_deploys_fetch_failed", error=str(exc))
                recent_deploys = []
            finally:
                await github_history.close()

            try:
                from understudy.graph.service_graph import ServiceDependencyGraph

                dep_graph = ServiceDependencyGraph.from_yaml(
                    "deploy/prod/dependencies.yaml"
                ).snapshot()
            except Exception as exc:
                logger.warning("dependency_graph_load_failed", error=str(exc))
                dep_graph = DependencyGraphSnapshot(
                    nodes=[service],
                    edges=[],
                    observed_at=now,
                )
            return IncidentContext(
                incident_id=inc_id,
                alert=alert,
                signatures=signatures,
                metrics_window=metric_window,
                recent_deploys=recent_deploys,
                dependency_graph=dep_graph,
                inferred_failure_class=None,
                gathered_at=now,
            )
        finally:
            if hasattr(obs_adapter, "close"):
                await obs_adapter.close()

    context = asyncio.run(_gather())

    if json_output:
        typer.echo(context.model_dump_json(indent=2))
    else:
        typer.echo(
            f"IncidentContext gathered for {service} ({namespace}): "
            f"requests={context.metrics_window.request_count}, "
            f"p99={context.metrics_window.p99_latency_ms}ms, "
            f"errors={context.metrics_window.error_rate}, "
            f"signatures={len(context.signatures)}, "
            f"deploys={len(context.recent_deploys)}"
        )


@signals_app.command("deploys")
def signals_deploys(
    limit: int = typer.Option(
        5,
        "--limit",
        "-l",
        help="Number of recent deployments to fetch from version control.",
    ),
    repo: str | None = typer.Option(
        None,
        "--repo",
        "-r",
        help="GitHub repository in owner/repo format.",
    ),
    branch: str | None = typer.Option(
        None,
        "--branch",
        "-b",
        help="Optional branch or ref override.",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        "-t",
        help="Optional GitHub personal access token override.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json/--no-json",
        help="Print raw DeployRef list JSON.",
    ),
) -> None:
    """Fetch recent deployment metadata from version control."""
    import asyncio
    import json

    from understudy.signals.github import GitHubDeployHistory

    history = GitHubDeployHistory(
        token=token,
        repo=repo,
        branch=branch,
    )

    async def _fetch() -> list[Any]:
        try:
            return await history.recent_deploys(limit=limit)
        finally:
            await history.close()

    try:
        deploys = asyncio.run(_fetch())
    except Exception as exc:
        typer.echo(f"Error fetching deploy history: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        raw_list = [d.model_dump(mode="json") for d in deploys]
        typer.echo(json.dumps(raw_list, indent=2))
    else:
        typer.echo(f"Recent {len(deploys)} deployment(s) from {history.repo}:")
        for d in deploys:
            pr_str = f"#{d.pr_number}" if d.pr_number is not None else "-"
            mig_str = "YES" if d.contains_migration else "NO"
            date_str = d.deployed_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            typer.echo(f"  {d.commit_sha[:7]} | {date_str} | PR: {pr_str} | migration: {mig_str}")
            if d.image_digests:
                for svc, digest in sorted(d.image_digests.items()):
                    typer.echo(f"    {svc}: {digest}")


@app.command("plan")
def plan_cmd(
    context_file: Annotated[
        Path,
        typer.Option(
            "--context",
            "-c",
            help="Path to IncidentContext JSON fixture.",
        ),
    ],
    count: Annotated[
        int,
        typer.Option(
            "--count",
            "-n",
            help="Number of active candidate plans to generate.",
        ),
    ] = 3,
    seed: Annotated[
        int | None,
        typer.Option(
            "--seed",
            help="Optional random seed for deterministic planner / variance evaluation.",
        ),
    ] = None,
    twice: Annotated[
        bool,
        typer.Option(
            "--twice",
            help="Run planner twice to evaluate action-type stability across calls.",
        ),
    ] = False,
    fake: Annotated[
        bool,
        typer.Option(
            "--fake",
            help="Use FakePlanner instead of live LLMPlanner.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json/--no-json",
            help="Output full RemediationPlan JSON array.",
        ),
    ] = False,
) -> None:
    """Generate candidate remediation plans, guaranteeing NO_ACTION on the ballot."""
    import asyncio
    import json

    from understudy.contracts.incident import IncidentContext
    from understudy.planner.fakes import FakePlanner
    from understudy.planner.validate import LLMPlanner

    if not context_file.exists():
        typer.echo(f"Context file not found: {context_file}", err=True)
        raise typer.Exit(code=1)

    try:
        raw_text = context_file.read_text(encoding="utf-8")
        context = IncidentContext.model_validate_json(raw_text)
    except Exception as exc:
        typer.echo(f"Failed to parse IncidentContext from {context_file}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    cluster_workloads: set[str] = set(context.dependency_graph.nodes)
    try:
        from understudy.common.errors import GraphError
        from understudy.graph.k8s import get_cluster_workloads

        live_workloads = get_cluster_workloads()
        if live_workloads:
            cluster_workloads = live_workloads
    except GraphError:
        # Fall back cleanly to dependency graph nodes when live cluster is unavailable
        pass

    def _execute_planner(planner_seed: int | None) -> list[Any]:
        p: Planner
        if fake:
            p = FakePlanner(seed=planner_seed if planner_seed is not None else 42)
        else:
            p = LLMPlanner(cluster_workloads=cluster_workloads)

        async def _run() -> list[Any]:
            return await p.generate_candidates(context, count=count)

        return asyncio.run(_run())

    try:
        plans = _execute_planner(seed)
    except Exception as exc:
        typer.echo(f"Planner error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if twice:
        try:
            plans2 = _execute_planner(seed)
        except Exception as exc:
            typer.echo(f"Second planner run failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        actions1 = [p.action.value for p in plans]
        actions2 = [p.action.value for p in plans2]
        matching = sum(1 for a, b in zip(actions1, actions2, strict=False) if a == b)
        total = max(len(actions1), len(actions2), 1)
        stability = matching / total

        typer.echo(f"Planner run 1 ({len(plans)} plans): {actions1}")
        typer.echo(f"Planner run 2 ({len(plans2)} plans): {actions2}")
        typer.echo(f"Action-type stability: {stability:.2f} ({matching}/{total} matching)")
        return

    if json_output:
        raw_plans = [p.model_dump(mode="json") for p in plans]
        typer.echo(json.dumps(raw_plans, indent=2))
    else:
        typer.echo(f"Generated {len(plans)} candidate plan(s) for {context.incident_id}:")
        for plan in plans:
            inv_str = f"inverse={plan.inverse.action.value}" if plan.inverse else "inverse=none"
            typer.echo(
                f"  [{plan.candidate_index}] {plan.plan_id}: {plan.action.value} "
                f"workload={plan.params.workload} ({inv_str}) - {plan.rationale}"
            )


playbook_app = typer.Typer(
    name="playbook",
    help="Incident playbook library, matching, and seeding.",
    no_args_is_help=True,
)
app.add_typer(playbook_app, name="playbook")


@playbook_app.command("seed")
def playbook_seed(
    from_file: Annotated[
        Path,
        typer.Option(
            "--from",
            "-f",
            help="Path to JSON file containing seed playbooks.",
        ),
    ],
    fake: Annotated[
        bool,
        typer.Option(
            "--fake",
            help="Use in-memory fake store and deterministic embeddings.",
        ),
    ] = False,
) -> None:
    """Seed historical or synthetic playbooks into the playbook store."""
    import asyncio
    import json

    from understudy.contracts.enums import FailureClass
    from understudy.contracts.plan import RemediationPlan
    from understudy.playbook.signature import deterministic_signature_embedding
    from understudy.store.fakes import FakePlaybookStore
    from understudy.store.postgres import PostgresPlaybookStore

    if not from_file.exists():
        typer.echo(f"Seed file not found: {from_file}", err=True)
        raise typer.Exit(code=1)

    try:
        raw = json.loads(from_file.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raw = [raw]
    except Exception as exc:
        typer.echo(f"Failed to parse seed playbooks JSON: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    store = FakePlaybookStore() if fake else PostgresPlaybookStore()

    async def _seed_all() -> int:
        count = 0
        for item in raw:
            pb_id = str(item["playbook_id"])
            fc_raw = item["failure_class"]
            fc = FailureClass(fc_raw) if fc_raw in FailureClass._value2member_map_ else fc_raw
            sig_text = str(item["signature_text"])
            plan = RemediationPlan.model_validate(item["plan"])
            evidence_refs = list(item.get("evidence_refs", []))
            origin = str(item.get("origin", "seed"))
            emb = item.get("embedding")
            if not emb or not isinstance(emb, list):
                emb = deterministic_signature_embedding(sig_text)

            await store.save_playbook(
                playbook_id=pb_id,
                failure_class=fc,
                signature_text=sig_text,
                embedding=emb,
                plan=plan,
                evidence_refs=evidence_refs,
                origin=origin,
            )
            count += 1
        return count

    try:
        seeded_count = asyncio.run(_seed_all())
        typer.echo(f"Seeded {seeded_count} playbook(s) from {from_file}")
    except Exception as exc:
        typer.echo(f"Failed to seed playbooks into store: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@playbook_app.command("match")
def playbook_match(
    context_file: Annotated[
        Path,
        typer.Option(
            "--context",
            "-c",
            help="Path to IncidentContext JSON fixture.",
        ),
    ],
    top_k: Annotated[
        int,
        typer.Option(
            "--top-k",
            "-k",
            help="Number of nearest candidates to retrieve from store.",
        ),
    ] = 3,
    fake: Annotated[
        bool,
        typer.Option(
            "--fake",
            help="Use FakePlaybookLibrary or deterministic matching.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json/--no-json",
            help="Output raw PlaybookMatchResult JSON.",
        ),
    ] = False,
) -> None:
    """Find and confirm matching candidate playbook for the active incident context."""
    import asyncio
    import json

    from understudy.contracts.enums import FailureClass
    from understudy.contracts.incident import IncidentContext
    from understudy.contracts.plan import RemediationPlan
    from understudy.playbook.confirmation import PlaybookConfirmer
    from understudy.playbook.embeddings import OpenRouterEmbeddingClient
    from understudy.playbook.retriever import PlaybookMatchResult, PlaybookRetriever
    from understudy.playbook.signature import deterministic_signature_embedding
    from understudy.store.fakes import FakePlaybookStore
    from understudy.store.postgres import PostgresPlaybookStore

    if not context_file.exists():
        typer.echo(f"Context file not found: {context_file}", err=True)
        raise typer.Exit(code=1)

    try:
        raw_text = context_file.read_text(encoding="utf-8")
        context = IncidentContext.model_validate_json(raw_text)
    except Exception as exc:
        typer.echo(f"Failed to parse IncidentContext: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    async def _match() -> PlaybookMatchResult:
        store: FakePlaybookStore | PostgresPlaybookStore
        if fake:
            store = FakePlaybookStore()
            seed_path = Path("fixtures/playbooks_seed.json")
            if seed_path.exists():
                seed_data = json.loads(seed_path.read_text(encoding="utf-8"))
                for s in seed_data:
                    fc_raw = s["failure_class"]
                    fc = (
                        FailureClass(fc_raw)
                        if fc_raw in FailureClass._value2member_map_
                        else fc_raw
                    )
                    await store.save_playbook(
                        playbook_id=s["playbook_id"],
                        failure_class=fc,
                        signature_text=s["signature_text"],
                        embedding=s.get("embedding")
                        or deterministic_signature_embedding(s["signature_text"]),
                        plan=RemediationPlan.model_validate(s["plan"]),
                        evidence_refs=s.get("evidence_refs", []),
                        origin=s.get("origin", "seed"),
                    )

            async def _fake_embed(text: str) -> list[float]:
                return deterministic_signature_embedding(text)

            async def _fake_confirm(messages: list[dict[str, str]]) -> str:
                _ = messages
                return json.dumps(
                    {
                        "retained_playbook_id": "pb_bad_deploy_data_service",
                        "confidence": 0.95,
                        "reason": (
                            "Matches N+1 query regression on data-service; "
                            "rollback to deadbeef is safe and verified."
                        ),
                    }
                )

            embedder = OpenRouterEmbeddingClient(embed_caller=_fake_embed)
            confirmer = PlaybookConfirmer(llm_caller=_fake_confirm)
            retriever = PlaybookRetriever(
                store=store,
                embedder=embedder,
                confirmer=confirmer,
            )
        else:
            store = PostgresPlaybookStore()
            retriever = PlaybookRetriever(store=store)

        return await retriever.match_playbook(context, top_k=top_k)

    from understudy.common.logging import configure_logging

    try:
        if json_output:
            configure_logging(log_level="WARNING")
        result = asyncio.run(_match())
    except Exception as exc:
        typer.echo(f"Playbook match error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        if json_output:
            configure_logging(log_level="INFO")

    if json_output:
        typer.echo(result.model_dump_json(indent=2))
        return

    if result.matched and result.plan is not None:
        typer.echo(
            f"Matched playbook '{result.playbook_id}' "
            f"(cosine: {result.similarity:.2f} > 0.8):\n"
            f"  Action: {result.plan.action.value}\n"
            f"  Workload: {result.plan.params.workload}\n"
            f"  Confirmation Reason: {result.confirmation_reason}\n"
            f"  Plan ID: {result.plan.plan_id}"
        )
    else:
        typer.echo(
            f"No playbook match confirmed: {result.confirmation_reason} "
            f"(top similarity: {result.similarity:.2f})"
        )


@playbook_app.command("list")
def playbook_list(
    fake: Annotated[
        bool,
        typer.Option(
            "--fake",
            help="List playbooks from fake store.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json/--no-json",
            help="Output JSON array.",
        ),
    ] = False,
) -> None:
    """List all registered playbooks in the library with success/failure counters."""
    import asyncio
    import json

    from understudy.store.api import PlaybookSearchResult
    from understudy.store.fakes import FakePlaybookStore
    from understudy.store.postgres import PostgresPlaybookStore

    async def _list() -> list[PlaybookSearchResult]:
        store: FakePlaybookStore | PostgresPlaybookStore
        if fake:
            store = FakePlaybookStore()
            seed_path = Path("fixtures/playbooks_seed.json")
            if seed_path.exists():
                from understudy.contracts.enums import FailureClass
                from understudy.contracts.plan import RemediationPlan
                from understudy.playbook.signature import deterministic_signature_embedding

                seed_data = json.loads(seed_path.read_text(encoding="utf-8"))
                for s in seed_data:
                    fc_raw = s["failure_class"]
                    fc = (
                        FailureClass(fc_raw)
                        if fc_raw in FailureClass._value2member_map_
                        else fc_raw
                    )
                    await store.save_playbook(
                        playbook_id=s["playbook_id"],
                        failure_class=fc,
                        signature_text=s["signature_text"],
                        embedding=s.get("embedding")
                        or deterministic_signature_embedding(s["signature_text"]),
                        plan=RemediationPlan.model_validate(s["plan"]),
                        evidence_refs=s.get("evidence_refs", []),
                        origin=s.get("origin", "seed"),
                    )
        else:
            store = PostgresPlaybookStore()

        return await store.list_playbooks()

    try:
        playbooks = asyncio.run(_list())
    except Exception as exc:
        typer.echo(f"Failed to list playbooks: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        raw_list = [p.model_dump(mode="json") for p in playbooks]
        typer.echo(json.dumps(raw_list, indent=2))
        return

    typer.echo(f"Stored Playbooks ({len(playbooks)}):")
    for pb in playbooks:
        typer.echo(
            f"  {pb.playbook_id} | class={pb.failure_class} | "
            f"action={pb.plan.action.value} | successes={pb.successes} | failures={pb.failures}"
        )


@playbook_app.command("write")
def playbook_write(
    context_file: Annotated[
        Path,
        typer.Option(
            "--context",
            "-c",
            help="Path to IncidentContext JSON fixture.",
        ),
    ],
    plan_file: Annotated[
        Path,
        typer.Option(
            "--plan",
            "-p",
            help="Path to RemediationPlan JSON fixture.",
        ),
    ],
    run_id: Annotated[
        str,
        typer.Option(
            "--run-id",
            "-r",
            help="Run identifier of the successful resolution.",
        ),
    ],
    origin: Annotated[
        str,
        typer.Option(
            "--origin",
            help="Origin of the resolution ('incident' or 'shadow').",
        ),
    ] = "incident",
    fake: Annotated[
        bool,
        typer.Option(
            "--fake",
            help="Use in-memory fake store.",
        ),
    ] = False,
) -> None:
    """Upsert a playbook on successful run resolution, keyed by incident signature."""
    import asyncio
    import json

    from understudy.contracts.incident import IncidentContext
    from understudy.contracts.plan import RemediationPlan
    from understudy.playbook.write import write_playbook
    from understudy.store.fakes import FakePlaybookStore
    from understudy.store.postgres import PostgresPlaybookStore

    if not context_file.exists():
        typer.echo(f"Context file not found: {context_file}", err=True)
        raise typer.Exit(code=1)

    if not plan_file.exists():
        typer.echo(f"Plan file not found: {plan_file}", err=True)
        raise typer.Exit(code=1)

    try:
        context = IncidentContext.model_validate_json(context_file.read_text(encoding="utf-8"))
    except Exception as exc:
        typer.echo(f"Failed to parse IncidentContext: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    try:
        plan = RemediationPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))
    except Exception as exc:
        typer.echo(f"Failed to parse RemediationPlan: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    async def _write() -> str:
        store: FakePlaybookStore | PostgresPlaybookStore
        if fake:
            store = FakePlaybookStore()
            seed_path = Path("fixtures/playbooks_seed.json")
            if seed_path.exists():
                from understudy.contracts.enums import FailureClass
                from understudy.playbook.signature import deterministic_signature_embedding

                seed_data = json.loads(seed_path.read_text(encoding="utf-8"))
                for s in seed_data:
                    fc_raw = s["failure_class"]
                    fc = (
                        FailureClass(fc_raw)
                        if fc_raw in FailureClass._value2member_map_
                        else fc_raw
                    )
                    await store.save_playbook(
                        playbook_id=s["playbook_id"],
                        failure_class=fc,
                        signature_text=s["signature_text"],
                        embedding=s.get("embedding")
                        or deterministic_signature_embedding(s["signature_text"]),
                        plan=RemediationPlan.model_validate(s["plan"]),
                        evidence_refs=s.get("evidence_refs", []),
                        origin=s.get("origin", "seed"),
                    )
        else:
            store = PostgresPlaybookStore()

        return await write_playbook(
            context=context,
            plan=plan,
            run_id=run_id,
            store=store,
            origin=origin,
        )

    try:
        pb_id = asyncio.run(_write())
        typer.echo(f"Playbook write-back completed: {pb_id}")
    except Exception as exc:
        typer.echo(f"Playbook write error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
