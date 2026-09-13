"""Mirror traffic fidelity comparison and evaluation models."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TwinFidelityReport:
    """Evaluation report comparing mirrored twin delivery and path distribution to production."""

    twin_id: str
    prod_delivered: int
    twin_delivered: int
    delivered_delta_ratio: float
    drop_ratio: float
    path_distribution_match: bool
    status: Literal["OK", "DEGRADED"]
    prod_paths: dict[str, int]
    twin_paths: dict[str, int]


def evaluate_twin_fidelity(
    twin_id: str,
    prod_delivered: int,
    twin_delivered: int,
    drop_ratio: float,
    prod_paths: dict[str, int],
    twin_paths: dict[str, int],
    delta_threshold: float = 0.02,
    max_drop_ratio: float = 0.05,
) -> TwinFidelityReport:
    """Evaluate whether a twin meets traffic fidelity requirements compared to production.

    Requires:
    - Per-twin delivered count within delta_threshold (default 2%) of production.
    - Drop ratio below max_drop_ratio (default 5%).
    - Path distribution matches production (relative path frequency within 2%).
    """
    if prod_delivered == 0:
        delta = 0.0 if twin_delivered == 0 else 1.0
    else:
        ratio = twin_delivered / prod_delivered
        delta = abs(1.0 - ratio)

    path_match = True
    if prod_delivered > 0:
        for path, prod_count in prod_paths.items():
            expected_fraction = prod_count / prod_delivered
            actual_count = twin_paths.get(path, 0)
            actual_fraction = (actual_count / twin_delivered) if twin_delivered > 0 else 0.0
            if abs(expected_fraction - actual_fraction) > 0.02:
                path_match = False
                break
        for path in twin_paths:
            if path not in prod_paths and twin_paths[path] > 0:
                path_match = False
                break
    elif twin_delivered > 0:
        path_match = False

    is_ok = delta <= delta_threshold and drop_ratio <= max_drop_ratio and path_match

    return TwinFidelityReport(
        twin_id=twin_id,
        prod_delivered=prod_delivered,
        twin_delivered=twin_delivered,
        delivered_delta_ratio=delta,
        drop_ratio=drop_ratio,
        path_distribution_match=path_match,
        status="OK" if is_ok else "DEGRADED",
        prod_paths=prod_paths,
        twin_paths=twin_paths,
    )
