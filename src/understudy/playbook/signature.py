"""Playbook incident signature text construction and deterministic embeddings."""

import hashlib
import math
import random

from understudy.contracts.incident import IncidentContext


def build_signature_text(context: IncidentContext, max_fingerprints: int = 3) -> str:
    """Construct canonical incident signature text for vector embedding and matching.

    Per build-plan B3.5, combines:
    - Failure class
    - Top error fingerprints (ranked by event count)
    - Affected service
    - Deploy proximity (time elapsed between last deploy and incident alert)
    """
    failure_class = (
        context.inferred_failure_class.value
        if context.inferred_failure_class is not None
        else "unknown"
    )
    affected_service = context.alert.service or "unknown"

    # Top error fingerprints sorted by count descending, stable tie-break on fingerprint
    sorted_sigs = sorted(
        context.signatures,
        key=lambda s: (-s.count, s.fingerprint),
    )
    top_sigs = sorted_sigs[:max_fingerprints]
    if top_sigs:
        fingerprints_str = "; ".join(
            f"{sig.fingerprint} (count: {sig.count}, msg: {sig.message.strip()})"
            for sig in top_sigs
        )
    else:
        fingerprints_str = "none"

    # Deploy proximity: delta between alert fired_at and most recent deployment
    if context.recent_deploys:
        sorted_deploys = sorted(
            context.recent_deploys,
            key=lambda d: d.deployed_at,
            reverse=True,
        )
        recent_deploy = sorted_deploys[0]
        delta_sec = max(
            0,
            int((context.alert.fired_at - recent_deploy.deployed_at).total_seconds()),
        )
        proximity_str = f"{delta_sec}s (commit: {recent_deploy.commit_sha[:7]})"
    else:
        proximity_str = "none"

    return (
        f"failure_class: {failure_class}\n"
        f"affected_service: {affected_service}\n"
        f"error_fingerprints: {fingerprints_str}\n"
        f"deploy_proximity: {proximity_str}"
    )


def deterministic_signature_embedding(text: str, dim: int = 1024) -> list[float]:
    """Generate a deterministic unit-length embedding vector from signature text.

    Uses a seeded pseudo-random generator derived from the SHA-256 hash of the input text.
    Strictly deterministic and does not modify global random state (per AGENTS.md §5.7).
    """
    seed_bytes = hashlib.sha256(text.encode("utf-8")).digest()
    seed_int = int.from_bytes(seed_bytes[:8], "big")
    rng = random.Random(seed_int)

    vec = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


__all__ = [
    "build_signature_text",
    "deterministic_signature_embedding",
]
