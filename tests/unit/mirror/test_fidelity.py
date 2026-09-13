"""Unit tests for mirror traffic fidelity evaluation (src/understudy/mirror/fidelity.py)."""

import pytest

from understudy.mirror.fidelity import evaluate_twin_fidelity


def test_evaluate_twin_fidelity_exact_match() -> None:
    """Perfect match between twin and prod results in OK status."""
    report = evaluate_twin_fidelity(
        twin_id="twin-1",
        prod_delivered=1000,
        twin_delivered=1000,
        drop_ratio=0.0,
        prod_paths={"/api/items": 800, "/api/checkout": 200},
        twin_paths={"/api/items": 800, "/api/checkout": 200},
    )
    assert report.twin_id == "twin-1"
    assert report.status == "OK"
    assert report.path_distribution_match is True
    assert report.delivered_delta_ratio == 0.0
    assert report.drop_ratio == 0.0


def test_evaluate_twin_fidelity_count_delta_within_threshold() -> None:
    """Delivered count within 2% is OK."""
    report = evaluate_twin_fidelity(
        twin_id="twin-1",
        prod_delivered=1000,
        twin_delivered=990,  # 1% delta
        drop_ratio=0.01,
        prod_paths={"/api/items": 800, "/api/checkout": 200},
        twin_paths={"/api/items": 792, "/api/checkout": 198},
    )
    assert report.status == "OK"
    assert report.delivered_delta_ratio == pytest.approx(0.01)


def test_evaluate_twin_fidelity_count_delta_exceeds_threshold() -> None:
    """Delivered count differing by more than 2% results in DEGRADED status."""
    report = evaluate_twin_fidelity(
        twin_id="twin-1",
        prod_delivered=1000,
        twin_delivered=950,  # 5% delta
        drop_ratio=0.01,
        prod_paths={"/api/items": 800, "/api/checkout": 200},
        twin_paths={"/api/items": 760, "/api/checkout": 190},
    )
    assert report.status == "DEGRADED"
    assert report.delivered_delta_ratio == pytest.approx(0.05)


def test_evaluate_twin_fidelity_drop_ratio_exceeds_threshold() -> None:
    """Drop ratio above 5% results in DEGRADED status even if delivered count matches."""
    report = evaluate_twin_fidelity(
        twin_id="twin-1",
        prod_delivered=1000,
        twin_delivered=1000,
        drop_ratio=0.08,  # 8% drop ratio
        prod_paths={"/api/items": 1000},
        twin_paths={"/api/items": 1000},
    )
    assert report.status == "DEGRADED"
    assert report.drop_ratio == 0.08


def test_evaluate_twin_fidelity_path_divergence() -> None:
    """Divergence in path distribution results in DEGRADED status."""
    report = evaluate_twin_fidelity(
        twin_id="twin-1",
        prod_delivered=1000,
        twin_delivered=1000,
        drop_ratio=0.0,
        prod_paths={"/api/items": 800, "/api/checkout": 200},
        twin_paths={"/api/items": 500, "/api/checkout": 500},  # 50% vs 80%/20%
    )
    assert report.status == "DEGRADED"
    assert report.path_distribution_match is False


def test_evaluate_twin_fidelity_unexpected_twin_path() -> None:
    """Twin receiving requests on paths prod never saw results in DEGRADED status."""
    report = evaluate_twin_fidelity(
        twin_id="twin-1",
        prod_delivered=1000,
        twin_delivered=1000,
        drop_ratio=0.0,
        prod_paths={"/api/items": 1000},
        twin_paths={"/api/items": 900, "/unexpected": 100},
    )
    assert report.status == "DEGRADED"
    assert report.path_distribution_match is False


def test_evaluate_twin_fidelity_zero_prod_delivered() -> None:
    """Zero prod delivered with zero twin delivered is OK; with twin > 0 is DEGRADED."""
    ok_report = evaluate_twin_fidelity(
        twin_id="twin-zero",
        prod_delivered=0,
        twin_delivered=0,
        drop_ratio=0.0,
        prod_paths={},
        twin_paths={},
    )
    assert ok_report.status == "OK"

    degraded_report = evaluate_twin_fidelity(
        twin_id="twin-ghost",
        prod_delivered=0,
        twin_delivered=10,
        drop_ratio=0.0,
        prod_paths={},
        twin_paths={"/api/items": 10},
    )
    assert degraded_report.status == "DEGRADED"
