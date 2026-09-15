"""Unit tests for Safety Kernel catalogue generation and runtime invariants."""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
import z3

from understudy.common.errors import MissingFact
from understudy.contracts.enums import ActionType, InvariantTier
from understudy.contracts.kernel import Fact
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.kernel.catalogue import (
    CATALOGUE_INVARIANTS,
    generate_catalogue_markdown,
    get_catalogue_invariants,
    render_entry_markdown,
    update_docs_catalogue,
    verify_catalogue_matches_docs,
)
from understudy.kernel.dsl import Invariant, KernelContext
from understudy.kernel.invariants.k06_twin_egress import K6TwinEgressContainment
from understudy.kernel.invariants.k10_actuation_authorisation import K10ActuationAuthorisation


def _dummy_plan() -> RemediationPlan:
    return RemediationPlan(
        plan_id="plan_dummy",
        candidate_index=0,
        action=ActionType.NO_ACTION,
        params=ActionParams(workload="dummy-svc"),
        target_resources=[ResourceRef(kind="Deployment", name="dummy-svc", namespace="ust-prod")],
        declared_blast_set=["dummy-svc"],
        inverse=None,
        rationale="Dummy plan",
        origin="planner",
    )


def test_get_catalogue_invariants_ordering() -> None:
    """Verify get_catalogue_invariants returns all 10 invariants in numerical order."""
    invariants = get_catalogue_invariants()
    assert len(invariants) == 10
    expected_ids = ["K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8", "K9", "K10"]
    assert [inv.id for inv in invariants] == expected_ids


def test_get_catalogue_invariants_custom_sort_fallback() -> None:
    """Verify sort key fallback for non-digit IDs."""

    class NonDigitInv(Invariant):
        id: str = "CUSTOM"
        tier: InvariantTier = InvariantTier.PROOF
        statement: str = "Statement"
        required_facts: Sequence[str] = ()

        def build(self, ctx: KernelContext) -> z3.BoolRef:
            _ = ctx
            return z3.BoolVal(True)

    sorted_invs = sorted(
        [NonDigitInv(), K10ActuationAuthorisation(), K6TwinEgressContainment()],
        key=lambda inv: int(inv.id[1:]) if inv.id[1:].isdigit() else 999,
    )
    assert [inv.id for inv in sorted_invs] == ["K6", "K10", "CUSTOM"]


def test_k06_runtime_invariant_conformance_and_build() -> None:
    """Verify K6TwinEgressContainment conforms to Invariant protocol and builds formula."""
    inv = K6TwinEgressContainment()
    assert isinstance(inv, Invariant)
    assert inv.id == "K6"
    assert inv.tier == InvariantTier.RUNTIME
    assert inv.required_facts == ("twin_egress_policy_present",)

    # 1. With fact present
    plan = _dummy_plan()
    fact_present = Fact(
        name="twin_egress_policy_present",
        value=True,
        source="k8s",
        observed_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC),
    )
    ctx_ok = KernelContext(plan, [fact_present])
    formula = inv.build(ctx_ok)
    assert isinstance(formula, z3.BoolRef)

    # 2. With fact missing -> raises MissingFact (Rule 5.6)
    ctx_missing = KernelContext(plan, [])
    with pytest.raises(MissingFact) as exc_info:
        inv.build(ctx_missing)
    assert "twin_egress_policy_present" in exc_info.value.missing_facts


def test_k10_runtime_invariant_conformance_and_build() -> None:
    """Verify K10ActuationAuthorisation conforms to Invariant protocol and builds formula."""
    inv = K10ActuationAuthorisation()
    assert isinstance(inv, Invariant)
    assert inv.id == "K10"
    assert inv.tier == InvariantTier.RUNTIME
    assert inv.required_facts == ()

    plan = _dummy_plan()
    ctx = KernelContext(plan, [])
    formula = inv.build(ctx)
    assert z3.is_true(formula)


def test_render_entry_markdown_fallbacks_and_custom_variants() -> None:
    """Test render_entry_markdown fallbacks for attributes."""

    class MinimalInv(Invariant):
        id: str = "K99"
        tier: InvariantTier = InvariantTier.PROOF
        statement: str = "Minimal statement."
        required_facts: Sequence[str] = ()

        def build(self, ctx: KernelContext) -> z3.BoolRef:
            _ = ctx
            return z3.BoolVal(True)

    rendered = render_entry_markdown(MinimalInv())
    assert "### K99 — K99" in rendered
    assert "**Tier:** PROOF" in rendered
    assert "**Statement.** Minimal statement." in rendered
    assert "SMT shape" not in rendered

    # Custom runtime invariant without explicit why_runtime_label
    class RuntimeNoLabel(Invariant):
        id: str = "K100"
        tier: InvariantTier = InvariantTier.RUNTIME
        statement: str = "Runtime check."
        required_facts: Sequence[str] = ()
        why_runtime: str = "Runtime reasoning explanation."

        def build(self, ctx: KernelContext) -> z3.BoolRef:
            _ = ctx
            return z3.BoolVal(True)

    rendered_rt = render_entry_markdown(RuntimeNoLabel())
    assert "**Why it is RUNTIME.** Runtime reasoning explanation." in rendered_rt


def test_generate_catalogue_markdown_custom_list() -> None:
    """Verify generate_catalogue_markdown accepts custom invariant sequence."""
    invs = [CATALOGUE_INVARIANTS[0], CATALOGUE_INVARIANTS[1]]
    rendered = generate_catalogue_markdown(invs)
    assert "### K1 — Replica floor" in rendered
    assert "### K2 — Namespace scope" in rendered
    assert "### K3 — Migration boundary" not in rendered
    assert rendered.endswith("## 3.5 Coverage summary\n")


def test_update_docs_catalogue_missing_section_raises(tmp_path: Path) -> None:
    """Verify update_docs_catalogue raises ValueError if §3.4 header is absent."""
    bad_file = tmp_path / "bad.md"
    bad_file.write_text("# Title without catalogue section", encoding="utf-8")
    with pytest.raises(ValueError, match=r"Could not locate §3.4 invariant catalogue section"):
        update_docs_catalogue(bad_file)


def test_verify_catalogue_matches_docs_missing_section(tmp_path: Path) -> None:
    """Verify verify_catalogue_matches_docs returns False when section is absent."""
    bad_file = tmp_path / "bad.md"
    bad_file.write_text("# Title without catalogue section", encoding="utf-8")
    matches, msg = verify_catalogue_matches_docs(bad_file)
    assert matches is False
    assert "Could not locate §3.4 invariant catalogue section" in msg


def test_verify_catalogue_matches_docs_diff_output(tmp_path: Path) -> None:
    """Verify verify_catalogue_matches_docs returns unified diff when text diverges."""
    doc_path = Path("docs/03-invariants.md")
    content = doc_path.read_text(encoding="utf-8")
    altered = content.replace("Replica floor", "Divergent Floor")
    test_file = tmp_path / "03-invariants.md"
    test_file.write_text(altered, encoding="utf-8")

    matches, diff = verify_catalogue_matches_docs(test_file)
    assert matches is False
    assert "-### K1 — Divergent Floor" in diff
    assert "+### K1 — Replica floor" in diff
