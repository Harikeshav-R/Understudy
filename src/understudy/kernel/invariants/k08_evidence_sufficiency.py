"""Invariant K8: Evidence sufficiency and freshness formal proof verification.

Implements build-plan step B4.3h:
A plan may only be cleared on evidence that is fresh, dense, and high-fidelity.
SMT shape (docs/03-invariants.md §3.4):
evidence_age_seconds ≤ MAX_EVIDENCE_AGE (default 300)
∧ probe_sample_count  ≥ MIN_PROBE_SAMPLES (default 60)
∧ max_drop_ratio      ≤ MIRROR_DROP_CEILING (default 0.05)
"""

from collections.abc import Sequence

import z3

from understudy.contracts.enums import InvariantTier
from understudy.kernel.dsl import Invariant, KernelContext

DEFAULT_MAX_EVIDENCE_AGE: float = 300.0
DEFAULT_MIN_PROBE_SAMPLES: int = 60
DEFAULT_MIRROR_DROP_CEILING: float = 0.05


class K8EvidenceSufficiency(Invariant):
    """Safety invariant proving that candidate plans are cleared on sufficient evidence.

    SMT shape (docs/03-invariants.md §3.4):
    evidence_age_seconds <= MAX_EVIDENCE_AGE (default 300)
    ^ probe_sample_count >= MIN_PROBE_SAMPLES (default 60)
    ^ max_drop_ratio <= MIRROR_DROP_CEILING (default 0.05)
    """

    id: str = "K8"
    name: str = "Evidence sufficiency and freshness"
    tier: InvariantTier = InvariantTier.PROOF
    tier_display: str = "PROOF"
    statement: str = (
        "A plan may only be cleared on evidence that is fresh, dense, and\nhigh-fidelity."
    )
    required_facts: Sequence[str] = (
        "evidence_age_seconds",
        "probe_sample_count",
        "max_drop_ratio",
    )
    smt_shape: str | None = (
        "```\n"
        "evidence_age_seconds ≤ MAX_EVIDENCE_AGE (default 300)\n"
        "∧ probe_sample_count  ≥ MIN_PROBE_SAMPLES (default 60)\n"
        "∧ max_drop_ratio      ≤ MIRROR_DROP_CEILING (default 0.05)\n"
        "```"
    )
    why_it_exists: str | None = (
        "This is the invariant that turns the fidelity-gap limitation\n"
        "(`docs/00-product.md` §0.7) from an acknowledged weakness into a checked "
        "precondition. The\n"
        "kernel refuses to launder a bad rehearsal into a production action."
    )

    def __init__(
        self,
        *,
        max_evidence_age: float = DEFAULT_MAX_EVIDENCE_AGE,
        min_probe_samples: int = DEFAULT_MIN_PROBE_SAMPLES,
        mirror_drop_ceiling: float = DEFAULT_MIRROR_DROP_CEILING,
    ) -> None:
        self.max_evidence_age = max_evidence_age
        self.min_probe_samples = min_probe_samples
        self.mirror_drop_ceiling = mirror_drop_ceiling

    def build(self, ctx: KernelContext) -> z3.BoolRef:
        """Build SMT formula asserting evidence freshness, density, and fidelity.

        Retrieves:
        - 'evidence_age_seconds' (Real constant asserted to fact value)
        - 'probe_sample_count' (Int constant asserted to fact value)
        - 'max_drop_ratio' (Real constant asserted to fact value)

        Missing facts raise MissingFact immediately (Rule 5.6 zero defaults).

        Asserts:
        evidence_age_seconds <= max_evidence_age
        ^ probe_sample_count >= min_probe_samples
        ^ max_drop_ratio <= mirror_drop_ceiling
        """
        evidence_age = ctx.real("evidence_age_seconds")
        sample_count = ctx.int("probe_sample_count")
        drop_ratio = ctx.real("max_drop_ratio")

        return z3.And(
            evidence_age <= z3.RealVal(self.max_evidence_age),
            sample_count >= z3.IntVal(self.min_probe_samples),
            drop_ratio <= z3.RealVal(self.mirror_drop_ceiling),
        )


__all__ = [
    "DEFAULT_MAX_EVIDENCE_AGE",
    "DEFAULT_MIN_PROBE_SAMPLES",
    "DEFAULT_MIRROR_DROP_CEILING",
    "K8EvidenceSufficiency",
]
