"""Integration tests for k3d cluster configuration and deployment."""

from pathlib import Path
from typing import Any

import pytest
import yaml


@pytest.mark.integration
def test_cluster_config_validity() -> None:
    config_path = Path("deploy/k3d/cluster.yaml")
    assert config_path.exists(), "deploy/k3d/cluster.yaml must exist"

    with config_path.open() as f:
        data: dict[str, Any] = yaml.safe_load(f)

    assert data["apiVersion"] == "k3d.io/v1alpha5"
    assert data["kind"] == "Simple"
    assert data["metadata"]["name"] == "ust"
    assert data["servers"] == 1
    assert data["registries"]["create"]["hostPort"] == "5001"

    # Ports check
    port_entries = [p["port"] for p in data["ports"]]
    assert any("8080:8080" in p for p in port_entries)
    assert any("8081:8081" in p for p in port_entries)
    assert any("9090:9090" in p for p in port_entries)
    assert any("3100:3100" in p for p in port_entries)
    assert any("5432:5432" in p for p in port_entries)

    # ADR-003 memory budget: Traefik disabled
    extra_args = data.get("options", {}).get("k3s", {}).get("extraArgs", [])
    assert any("--disable=traefik" in str(arg) for arg in extra_args)
