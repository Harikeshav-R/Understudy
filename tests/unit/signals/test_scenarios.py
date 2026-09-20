"""Unit tests for scenario alert template resolution and synthetic alert factory."""

from pathlib import Path

import pytest

from understudy.common.clock import FrozenClock
from understudy.signals.scenarios import (
    SEED_SCENARIO_TEMPLATES,
    create_synthetic_alert,
    list_known_scenarios,
    load_scenario_template,
)


def test_list_known_scenarios() -> None:
    scenarios = list_known_scenarios()
    assert len(scenarios) == 12
    assert "bad_deploy_data_service" in scenarios
    assert "worker_backlog" in scenarios


def test_load_scenario_template_from_seed_registry() -> None:
    for scenario_id in SEED_SCENARIO_TEMPLATES:
        tmpl = load_scenario_template(scenario_id)
        assert tmpl.scenario_id == scenario_id
        assert tmpl.title
        assert tmpl.service in ("edge-gateway", "data-service", "auth-service", "worker")
        assert tmpl.severity in ("critical", "error", "warning")


def test_load_scenario_template_with_path_prefixes_and_suffixes() -> None:
    tmpl1 = load_scenario_template("seed/bad_deploy_data_service")
    assert tmpl1.scenario_id == "bad_deploy_data_service"

    tmpl2 = load_scenario_template("generated/worker_backlog.yaml")
    assert tmpl2.scenario_id == "worker_backlog"


def test_load_scenario_template_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown scenario ID 'non_existent_scenario'"):
        load_scenario_template("non_existent_scenario")


def test_load_scenario_template_from_disk(tmp_path: Path) -> None:
    seed_dir = tmp_path / "scenarios" / "seed"
    seed_dir.mkdir(parents=True)
    custom_yaml = seed_dir / "custom_test_scenario.yaml"
    custom_yaml.write_text(
        """
id: custom_test_scenario
alert_template:
  title: "Custom test alert"
  service: "auth-service"
  severity: "warning"
""",
        encoding="utf-8",
    )

    tmpl = load_scenario_template("custom_test_scenario", base_dir=tmp_path)
    assert tmpl.scenario_id == "custom_test_scenario"
    assert tmpl.title == "Custom test alert"
    assert tmpl.service == "auth-service"
    assert tmpl.severity == "warning"


def test_load_scenario_template_from_disk_defaults(tmp_path: Path) -> None:
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir(parents=True)
    custom_yaml = scenarios_dir / "sparse_scenario.yaml"
    custom_yaml.write_text(
        """
id: sparse_scenario
alert_template: {}
""",
        encoding="utf-8",
    )

    tmpl = load_scenario_template("sparse_scenario", base_dir=tmp_path)
    assert tmpl.scenario_id == "sparse_scenario"
    assert tmpl.title == "Alert for sparse_scenario"
    assert tmpl.service == "edge-gateway"
    assert tmpl.severity == "critical"


def test_load_scenario_template_corrupt_file(tmp_path: Path) -> None:
    bad_yaml = tmp_path / "corrupt.yaml"
    bad_yaml.write_text("invalid: yaml: [unclosed", encoding="utf-8")

    with pytest.raises(ValueError, match="Failed to parse scenario file"):
        load_scenario_template("corrupt", base_dir=tmp_path)


def test_create_synthetic_alert_default() -> None:
    clock = FrozenClock()
    alert = create_synthetic_alert("bad_deploy_data_service", clock=clock)

    assert alert.source == "synthetic"
    assert alert.alert_id.startswith("alt_")
    assert alert.title == "p99 latency SLO breach on edge-gateway"
    assert alert.service == "edge-gateway"
    assert alert.severity == "critical"
    assert alert.fired_at == clock.now()
    assert alert.raw["scenario_id"] == "bad_deploy_data_service"
    assert alert.raw["source"] == "synthetic_fallback"


def test_create_synthetic_alert_with_overrides() -> None:
    clock = FrozenClock()
    alert = create_synthetic_alert(
        "worker_backlog",
        clock=clock,
        title="Custom Overridden Title",
        service="edge-gateway",
        severity="critical",
    )

    assert alert.source == "synthetic"
    assert alert.title == "Custom Overridden Title"
    assert alert.service == "edge-gateway"
    assert alert.severity == "critical"


def test_create_synthetic_alert_normalizes_service_override() -> None:
    """A non-canonical service override is normalized like a real webhook alert."""
    clock = FrozenClock()
    alert = create_synthetic_alert(
        "worker_backlog",
        clock=clock,
        service="auth-service-k8s",
    )
    assert alert.service == "auth-service"


@pytest.mark.asyncio
async def test_scenario_deploy_history_invalid_base() -> None:
    """Non-DeployHistory base raises TypeError."""
    from understudy.signals.scenarios import ScenarioDeployHistory

    with pytest.raises(TypeError, match="must implement DeployHistory protocol"):
        ScenarioDeployHistory(base="not_a_deploy_history", scenario_id="bad_deploy")


@pytest.mark.asyncio
async def test_scenario_deploy_history_non_migration() -> None:
    """Non-migration scenario leaves deploys unchanged."""
    from datetime import UTC, datetime

    from understudy.contracts.incident import DeployRef
    from understudy.signals.fakes import FakeDeployHistory
    from understudy.signals.scenarios import ScenarioDeployHistory

    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    base = FakeDeployHistory(
        deploys=[
            DeployRef(
                commit_sha="c1",
                image_digests={"data-service": "tag1"},
                deployed_at=now,
                pr_number=1,
                contains_migration=False,
            )
        ]
    )
    history = ScenarioDeployHistory(base=base, scenario_id="bad_deploy_data_service")
    deploys = await history.recent_deploys(limit=5)
    assert len(deploys) == 1
    assert deploys[0].commit_sha == "c1"
    assert deploys[0].contains_migration is False


@pytest.mark.asyncio
async def test_scenario_deploy_history_non_migration_three_deploys() -> None:
    """Non-migration scenario with >= 3 deploys marks oldest commit as migration baseline."""
    from datetime import UTC, datetime, timedelta

    from understudy.contracts.incident import DeployRef
    from understudy.signals.fakes import FakeDeployHistory
    from understudy.signals.scenarios import ScenarioDeployHistory

    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    base = FakeDeployHistory(
        deploys=[
            DeployRef(
                commit_sha="c3",
                image_digests={"s": "t3"},
                deployed_at=now,
                contains_migration=False,
            ),
            DeployRef(
                commit_sha="c2",
                image_digests={"s": "t2"},
                deployed_at=now - timedelta(hours=1),
                contains_migration=False,
            ),
            DeployRef(
                commit_sha="c1",
                image_digests={"s": "t1"},
                deployed_at=now - timedelta(hours=2),
                contains_migration=False,
            ),
        ]
    )
    history = ScenarioDeployHistory(base=base, scenario_id="bad_deploy_data_service")
    deploys = await history.recent_deploys(limit=5)
    assert len(deploys) == 3
    assert deploys[0].commit_sha == "c3"
    assert deploys[0].contains_migration is False
    assert deploys[1].commit_sha == "c2"
    assert deploys[1].contains_migration is False
    assert deploys[2].commit_sha == "c1"
    assert deploys[2].contains_migration is True


@pytest.mark.asyncio
async def test_scenario_deploy_history_migration_two_deploys() -> None:
    """Migration scenario injects migration commit between head and target."""
    from datetime import UTC, datetime, timedelta

    from understudy.contracts.incident import DeployRef
    from understudy.signals.fakes import FakeDeployHistory
    from understudy.signals.scenarios import ScenarioDeployHistory

    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    t0 = now
    t1 = now - timedelta(minutes=10)
    base = FakeDeployHistory(
        deploys=[
            DeployRef(
                commit_sha="head",
                image_digests={"data-service": "reg"},
                deployed_at=t0,
                pr_number=1,
                contains_migration=False,
            ),
            DeployRef(
                commit_sha="target",
                image_digests={"data-service": "good"},
                deployed_at=t1,
                pr_number=2,
                contains_migration=False,
            ),
        ]
    )
    history = ScenarioDeployHistory(base=base, scenario_id="bad_deploy_with_migration")
    deploys = await history.recent_deploys(limit=5)
    assert len(deploys) == 3
    assert deploys[0].commit_sha == "head"
    assert deploys[1].commit_sha == "mig_boundary_commit"
    assert deploys[1].contains_migration is True
    assert t1 < deploys[1].deployed_at < t0
    assert deploys[2].commit_sha == "target"


@pytest.mark.asyncio
async def test_scenario_deploy_history_migration_head_older_than_target() -> None:
    """Migration scenario where head_time <= target_time offsets by 60s."""
    from datetime import UTC, datetime, timedelta

    from understudy.contracts.incident import DeployRef
    from understudy.signals.fakes import FakeDeployHistory
    from understudy.signals.scenarios import ScenarioDeployHistory

    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    base = FakeDeployHistory(
        deploys=[
            DeployRef(
                commit_sha="c1",
                image_digests={"data-service": "reg"},
                deployed_at=now,
                pr_number=1,
                contains_migration=False,
            ),
            DeployRef(
                commit_sha="c2",
                image_digests={"data-service": "good"},
                deployed_at=now + timedelta(minutes=5),
                pr_number=2,
                contains_migration=False,
            ),
        ]
    )
    history = ScenarioDeployHistory(base=base, scenario_id="bad_deploy_with_migration")
    deploys = await history.recent_deploys(limit=5)
    assert len(deploys) == 3
    assert deploys[1].contains_migration is True
    assert deploys[1].deployed_at == deploys[2].deployed_at + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_scenario_deploy_history_migration_single_deploy() -> None:
    """Migration scenario with 1 base deploy prepends migration deploy."""
    from datetime import UTC, datetime

    from understudy.contracts.incident import DeployRef
    from understudy.signals.fakes import FakeDeployHistory
    from understudy.signals.scenarios import ScenarioDeployHistory

    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    base = FakeDeployHistory(
        deploys=[
            DeployRef(
                commit_sha="c1",
                image_digests={"data-service": "reg"},
                deployed_at=now,
                pr_number=1,
                contains_migration=False,
            )
        ]
    )
    history = ScenarioDeployHistory(base=base, scenario_id="seed/bad_deploy_with_migration.yaml")
    deploys = await history.recent_deploys(limit=5)
    assert len(deploys) == 2
    assert deploys[0].commit_sha == "mig_boundary_commit"
    assert deploys[0].contains_migration is True


@pytest.mark.asyncio
async def test_scenario_deploy_history_migration_empty_deploys() -> None:
    """Migration scenario with empty base deploys generates fallback sequence."""
    from unittest.mock import AsyncMock

    from understudy.signals.api import DeployHistory
    from understudy.signals.scenarios import ScenarioDeployHistory

    base = AsyncMock(spec=DeployHistory)
    base.recent_deploys.return_value = []
    history = ScenarioDeployHistory(
        base=base,
        scenario_id="bad_deploy",
        force_migration=True,
    )
    deploys = await history.recent_deploys(limit=5)
    assert len(deploys) == 3
    assert any(d.contains_migration for d in deploys)


@pytest.mark.asyncio
async def test_scenario_deploy_history_already_has_migration() -> None:
    """If base deploys already have a migration, leaves them unchanged."""
    from datetime import UTC, datetime

    from understudy.contracts.incident import DeployRef
    from understudy.signals.fakes import FakeDeployHistory
    from understudy.signals.scenarios import ScenarioDeployHistory

    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    base = FakeDeployHistory(
        deploys=[
            DeployRef(
                commit_sha="c1",
                image_digests={},
                deployed_at=now,
                pr_number=1,
                contains_migration=True,
            )
        ]
    )
    history = ScenarioDeployHistory(base=base, scenario_id="bad_deploy_with_migration")
    deploys = await history.recent_deploys(limit=5)
    assert len(deploys) == 1
    assert deploys[0].commit_sha == "c1"


@pytest.mark.asyncio
async def test_scenario_deploy_history_context_manager() -> None:
    """ScenarioDeployHistory supports async context manager and close."""
    from understudy.contracts.incident import DeployRef
    from understudy.signals.api import DeployHistory
    from understudy.signals.scenarios import ScenarioDeployHistory

    class DummyContextDeployHistory(DeployHistory):
        def __init__(self) -> None:
            self.entered = False
            self.exited = False
            self.closed = False

        async def recent_deploys(self, _limit: int = 5) -> list[DeployRef]:
            return []

        async def close(self) -> None:
            self.closed = True

        async def __aenter__(self) -> "DummyContextDeployHistory":
            self.entered = True
            return self

        async def __aexit__(self, *args: object) -> None:
            self.exited = True

    base = DummyContextDeployHistory()
    history = ScenarioDeployHistory(base=base, scenario_id="test")
    async with history as h:
        assert h is history
    assert base.entered is True
    assert base.exited is True

    # Close directly
    await history.close()
    assert base.closed is True

    # Base with close only (falls back in __aexit__)
    class DummyCloseOnly(DeployHistory):
        def __init__(self) -> None:
            self.closed = False

        async def recent_deploys(self, _limit: int = 5) -> list[DeployRef]:
            return []

        async def close(self) -> None:
            self.closed = True

    close_base = DummyCloseOnly()
    close_history = ScenarioDeployHistory(base=close_base, scenario_id="test")
    async with close_history as h_close:
        assert h_close is close_history
    assert close_base.closed is True

    # Base without close or context manager
    class DummyPlain(DeployHistory):
        async def recent_deploys(self, _limit: int = 5) -> list[DeployRef]:
            return []

    plain_base = DummyPlain()
    plain_history = ScenarioDeployHistory(base=plain_base, scenario_id="test")
    await plain_history.close()
    async with plain_history as h_plain:
        assert h_plain is plain_history
