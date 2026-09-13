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
