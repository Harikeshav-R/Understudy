"""Unit tests for safety kernel DSL and KernelContext."""

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
import z3

from understudy.common.errors import MissingFact
from understudy.contracts.enums import ActionType, InvariantTier
from understudy.contracts.kernel import Fact
from understudy.contracts.plan import ActionParams, RemediationPlan, ResourceRef
from understudy.kernel.dsl import Invariant, KernelContext


def _create_sample_plan(
    action: ActionType = ActionType.SCALE_WORKLOAD,
    target_service: str = "data-service",
) -> RemediationPlan:
    """Create a valid RemediationPlan for testing."""
    return RemediationPlan(
        plan_id="plan_test_1",
        candidate_index=0,
        action=action,
        params=ActionParams(workload=target_service, replica_delta=1),
        target_resources=[
            ResourceRef(
                kind="Deployment",
                name=target_service,
                namespace="ust-prod",
            )
        ],
        declared_blast_set=[target_service],
        rationale="Scale data-service to handle load",
        origin="planner",
    )


class DummyInvariant:
    """Dummy invariant satisfying Invariant Protocol."""

    def __init__(
        self,
        inv_id: str = "K_TEST",
        tier: InvariantTier = InvariantTier.PROOF,
        statement: str = "Test invariant statement",
        required_facts: Sequence[str] | None = None,
    ) -> None:
        self.id = inv_id
        self.tier = tier
        self.statement = statement
        self.required_facts = required_facts or ["replicas[data-service]"]

    def build(self, ctx: KernelContext) -> z3.BoolRef:
        replicas = ctx.int("replicas[data-service]")
        return replicas >= 1


class IncompleteDummy:
    """Class missing required Invariant attributes."""

    id = "K_INCOMPLETE"


def test_invariant_protocol() -> None:
    """Test runtime checking of Invariant protocol."""
    dummy = DummyInvariant()
    assert isinstance(dummy, Invariant)

    incomplete = IncompleteDummy()
    assert not isinstance(incomplete, Invariant)

    # Test that calling Invariant.build directly raises NotImplementedError
    with pytest.raises(NotImplementedError):
        plan = _create_sample_plan()
        ctx = KernelContext(plan)
        Invariant.build(dummy, ctx)


def test_kernel_context_initialization() -> None:
    """Test KernelContext initialization variants."""
    plan = _create_sample_plan()

    # None facts
    ctx_none = KernelContext(plan, None)
    assert ctx_none.plan == plan
    assert ctx_none.facts == {}
    assert ctx_none.constants == {}
    assert ctx_none.fact_assertions == []
    assert ctx_none.missing_facts == []

    # Sequence of Fact
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    f1 = Fact(name="f1", value=1, source="k8s", observed_at=now)
    f2 = Fact(name="f2", value="bar", source="config", observed_at=now)
    ctx_seq = KernelContext(plan, [f1, f2])
    assert len(ctx_seq.facts) == 2
    assert ctx_seq.facts["f1"].value == 1

    # Mapping of Fact
    ctx_map = KernelContext(plan, {"f1": f1, "f2": f2})
    assert len(ctx_map.facts) == 2

    # Verify copy protection on properties
    facts_copy = ctx_map.facts
    facts_copy["f3"] = f1
    assert "f3" not in ctx_map.facts


def test_kernel_context_subscript_and_direct_lookups() -> None:
    """Test direct and subscripted fact resolution."""
    plan = _create_sample_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)

    # 1. Direct subscripted fact
    f_direct = Fact(name="replicas[data-service]", value=2, source="k8s", observed_at=now)
    ctx1 = KernelContext(plan, [f_direct])
    assert ctx1.has_fact("replicas[data-service]")
    assert ctx1.get_int("replicas[data-service]") == 2

    # 2. Dictionary-based subscripted fact
    f_dict = Fact(
        name="replicas",
        value={"data-service": 3, "auth-service": 1},
        source="k8s",
        observed_at=now,
    )
    ctx2 = KernelContext(plan, [f_dict])
    assert ctx2.has_fact("replicas[data-service]")
    assert ctx2.has_fact("replicas[auth-service]")
    assert not ctx2.has_fact("replicas[unknown-svc]")
    assert ctx2.get_int("replicas[data-service]") == 3

    # Subscript target present in fact but base value is not a mapping
    f_scalar = Fact(name="min_replicas", value=1, source="config", observed_at=now)
    ctx3 = KernelContext(plan, [f_scalar])
    assert not ctx3.has_fact("min_replicas[data-service]")
    with pytest.raises(MissingFact) as exc3:
        ctx3.get_int("min_replicas[data-service]")
    assert exc3.value.fact_name == "min_replicas[data-service]"

    # Missing subscript in dictionary
    with pytest.raises(MissingFact) as exc_missing_key:
        ctx2.get_int("replicas[missing-service]")
    assert exc_missing_key.value.fact_name == "replicas[missing-service]"

    # Subscript target where base name is not in facts at all
    assert not ctx1.has_fact("nonexistent_base[key]")
    with pytest.raises(MissingFact) as exc_nonexistent_base:
        ctx1.get_int("nonexistent_base[key]")
    assert exc_nonexistent_base.value.fact_name == "nonexistent_base[key]"


def test_kernel_context_typed_accessors() -> None:
    """Test typed Python accessors with valid and invalid types."""
    plan = _create_sample_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)

    facts = [
        Fact(name="int_fact", value=42, source="k8s", observed_at=now),
        Fact(name="bool_fact", value=True, source="config", observed_at=now),
        Fact(name="float_fact", value=3.14, source="tournament", observed_at=now),
        Fact(name="str_fact", value="ust-prod", source="config", observed_at=now),
        Fact(name="dt_fact", value=now, source="github", observed_at=now),
        Fact(name="dt_str_fact", value=now.isoformat(), source="github", observed_at=now),
        Fact(name="set_fact", value={"a", "b"}, source="graph", observed_at=now),
        Fact(name="list_fact", value=["c", "d"], source="graph", observed_at=now),
        Fact(name="tuple_fact", value=("e", "f"), source="graph", observed_at=now),
        Fact(name="frozenset_fact", value=frozenset(["g", "h"]), source="graph", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    # Int
    assert ctx.get_int("int_fact") == 42
    with pytest.raises(TypeError, match="expected int"):
        ctx.get_int("bool_fact")  # bool is not treated as int
    with pytest.raises(TypeError, match="expected int"):
        ctx.get_int("str_fact")

    # Bool
    assert ctx.get_bool("bool_fact") is True
    with pytest.raises(TypeError, match="expected bool"):
        ctx.get_bool("int_fact")

    # Float
    assert ctx.get_float("float_fact") == 3.14
    assert ctx.get_float("int_fact") == 42.0  # int can be converted to float
    with pytest.raises(TypeError, match="expected float/int"):
        ctx.get_float("bool_fact")
    with pytest.raises(TypeError, match="expected float/int"):
        ctx.get_float("str_fact")

    # Str
    assert ctx.get_str("str_fact") == "ust-prod"
    with pytest.raises(TypeError, match="expected str"):
        ctx.get_str("int_fact")

    # Datetime
    assert ctx.get_datetime("dt_fact") == now
    assert ctx.get_datetime("dt_str_fact") == now
    with pytest.raises(TypeError, match="expected datetime or ISO-8601 str"):
        ctx.get_datetime("int_fact")

    # Set
    assert ctx.get_set("set_fact") == {"a", "b"}
    assert ctx.get_set("list_fact") == {"c", "d"}
    assert ctx.get_set("tuple_fact") == {"e", "f"}
    assert ctx.get_set("frozenset_fact") == {"g", "h"}
    with pytest.raises(TypeError, match="expected set/collection"):
        ctx.get_set("int_fact")


def test_kernel_context_missing_facts_tracking() -> None:
    """Test that querying missing facts raises MissingFact and tracks them."""
    plan = _create_sample_plan()
    ctx = KernelContext(plan)

    assert ctx.missing_facts == []
    with pytest.raises(MissingFact) as exc1:
        ctx.get_int("missing_1")
    assert exc1.value.fact_name == "missing_1"

    with pytest.raises(MissingFact) as exc2:
        ctx.get_str("missing_2")
    assert exc2.value.fact_name == "missing_2"

    # Querying the same missing fact again should not duplicate in missing_facts list
    with pytest.raises(MissingFact):
        ctx.get_bool("missing_1")

    assert ctx.missing_facts == ["missing_1", "missing_2"]


def test_kernel_context_z3_constants_and_assertions() -> None:
    """Test Z3 constant caching and assertion generation."""
    plan = _create_sample_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)

    facts = [
        Fact(name="replicas", value=3, source="k8s", observed_at=now),
        Fact(name="flag_enabled", value=False, source="config", observed_at=now),
        Fact(name="drop_ratio", value=0.02, source="tournament", observed_at=now),
        Fact(name="namespace", value="ust-prod", source="config", observed_at=now),
        Fact(name="commit_time", value=now, source="github", observed_at=now),
    ]
    ctx = KernelContext(plan, facts)

    # First call creates constant and appends assertion
    r_const = ctx.int("replicas")
    assert isinstance(r_const, z3.ArithRef)
    # Second call returns cached constant without duplicate assertion
    r_const_cached = ctx.int("replicas")
    assert r_const is r_const_cached

    b_const = ctx.bool("flag_enabled")
    assert isinstance(b_const, z3.BoolRef)
    assert ctx.bool("flag_enabled") is b_const

    f_const = ctx.real("drop_ratio")
    assert isinstance(f_const, z3.ArithRef)
    assert ctx.real("drop_ratio") is f_const

    s_const = ctx.string("namespace")
    assert isinstance(s_const, z3.SeqRef)
    assert ctx.string("namespace") is s_const

    dt_const = ctx.datetime("commit_time")
    assert isinstance(dt_const, z3.ArithRef)
    assert ctx.datetime("commit_time") is dt_const

    assert len(ctx.constants) == 5
    assert len(ctx.fact_assertions) == 5

    # Direct value helpers
    assert isinstance(ctx.int_val("replicas"), z3.IntNumRef)
    assert isinstance(ctx.bool_val("flag_enabled"), z3.BoolRef)
    assert isinstance(ctx.real_val("drop_ratio"), z3.RatNumRef)
    assert isinstance(ctx.string_val("namespace"), z3.SeqRef)
    assert isinstance(ctx.datetime_val("commit_time"), z3.RatNumRef)


def test_kernel_context_fact_validation_helpers() -> None:
    """Test check_required_facts and require_facts."""
    plan = _create_sample_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)

    ctx = KernelContext(
        plan,
        [
            Fact(name="f1", value=1, source="k8s", observed_at=now),
            Fact(name="f2", value=2, source="config", observed_at=now),
        ],
    )

    # check_required_facts
    missing = ctx.check_required_facts(["f1", "f2", "f3", "f4"])
    assert missing == ["f3", "f4"]

    # require_facts passing
    ctx.require_facts("f1", "f2")

    # require_facts failing
    with pytest.raises(MissingFact) as exc:
        ctx.require_facts("f1", "f3", "f4")
    assert exc.value.missing_facts == ["f3", "f4"]
    assert "f3" in ctx.missing_facts
    assert "f4" in ctx.missing_facts

    # Calling require_facts again with an already recorded missing fact
    with pytest.raises(MissingFact) as exc_again:
        ctx.require_facts("f3")
    assert exc_again.value.missing_facts == ["f3"]


def test_kernel_context_with_z3_solver_verification() -> None:
    """Test end-to-end Z3 verification workflow using KernelContext."""
    plan = _create_sample_plan()
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)

    ctx = KernelContext(
        plan,
        [
            Fact(name="replicas[data-service]", value=2, source="k8s", observed_at=now),
            Fact(name="min_replicas[data-service]", value=1, source="config", observed_at=now),
        ],
    )

    solver = z3.Solver()

    # Pre-condition: assert facts from context
    replicas = ctx.int("replicas[data-service]")
    min_replicas = ctx.int("min_replicas[data-service]")
    for assertion in ctx.fact_assertions:
        solver.add(assertion)

    # Case 1: Safe plan with delta = +1 -> post_replicas = 3 >= 1
    solver.push()
    delta_safe = z3.IntVal(1)
    post_replicas_safe = replicas + delta_safe
    invariant_safe = post_replicas_safe >= min_replicas

    # In SMT verification, assert negation of invariant
    solver.add(z3.Not(invariant_safe))
    # unsat means invariant cannot be violated -> PROVED SAFE!
    assert solver.check() == z3.unsat
    solver.pop()

    # Case 2: Unsafe plan with delta = -2 -> post_replicas = 0 < 1
    solver.push()
    delta_unsafe = z3.IntVal(-2)
    post_replicas_unsafe = replicas + delta_unsafe
    invariant_unsafe = post_replicas_unsafe >= min_replicas

    solver.add(z3.Not(invariant_unsafe))
    # sat means a counterexample exists -> VETO!
    assert solver.check() == z3.sat
    model = solver.model()
    # Confirm model exhibits violation
    assert model.eval(replicas).as_long() == 2
    solver.pop()


def test_package_exports() -> None:
    """Verify package __init__.py re-exports."""
    import understudy.kernel as kernel

    assert hasattr(kernel, "Invariant")
    assert hasattr(kernel, "KernelContext")
    assert hasattr(kernel, "MissingFact")
