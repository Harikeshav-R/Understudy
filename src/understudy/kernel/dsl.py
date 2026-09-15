"""Domain-specific language and execution context for safety kernel invariants."""

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import z3

from understudy.common.errors import MissingFact
from understudy.contracts.enums import InvariantTier
from understudy.contracts.kernel import Fact
from understudy.contracts.plan import RemediationPlan

_SUBSCRIPT_PATTERN = re.compile(r"^([a-zA-Z0-9_]+)\[([a-zA-Z0-9_-]+)\]$")


@runtime_checkable
class Invariant(Protocol):
    """Protocol for a safety kernel invariant."""

    id: str
    tier: InvariantTier
    statement: str
    required_facts: Sequence[str]

    def build(self, ctx: "KernelContext") -> z3.BoolRef:
        """Build the Z3 boolean formula representing this invariant.

        The solver will check the *negation* of this formula.
        If the negation is unsat, the invariant is proved safe.
        If the negation is sat, the invariant is violated (veto).
        """
        raise NotImplementedError


class KernelContext:
    """Evaluation context holding system facts, plan diffs, and Z3 constants."""

    def __init__(
        self,
        plan: RemediationPlan,
        facts: Sequence[Fact] | Mapping[str, Fact] | None = None,
    ) -> None:
        self._plan = plan
        self._facts: dict[str, Fact] = {}
        if facts is not None:
            if isinstance(facts, Mapping):
                self._facts = dict(facts)
            else:
                self._facts = {f.name: f for f in facts}

        self._constants: dict[str, z3.ExprRef] = {}
        self._assertions: list[z3.BoolRef] = []
        self._missing_accessed: list[str] = []

    @property
    def plan(self) -> RemediationPlan:
        """Return the remediation plan being evaluated."""
        return self._plan

    @property
    def facts(self) -> dict[str, Fact]:
        """Return a copy of the raw fact dictionary."""
        return dict(self._facts)

    @property
    def constants(self) -> dict[str, z3.ExprRef]:
        """Return a copy of registered Z3 constants."""
        return dict(self._constants)

    @property
    def fact_assertions(self) -> list[z3.BoolRef]:
        """Return list of Z3 equality assertions binding constants to fact values."""
        return list(self._assertions)

    @property
    def missing_facts(self) -> list[str]:
        """Return list of unique fact names requested that were missing."""
        return list(dict.fromkeys(self._missing_accessed))

    def has_fact(self, name: str) -> bool:
        """Check if a fact exists in the context without raising."""
        if name in self._facts:
            return True
        match = _SUBSCRIPT_PATTERN.match(name)
        if match:
            base, key = match.group(1), match.group(2)
            if base in self._facts:
                val = self._facts[base].value
                if isinstance(val, Mapping) and key in val:
                    return True
        return False

    def get_fact(self, name: str) -> Fact:
        """Retrieve a Fact by name, raising MissingFact if absent."""
        if name in self._facts:
            return self._facts[name]

        match = _SUBSCRIPT_PATTERN.match(name)
        if match:
            base, key = match.group(1), match.group(2)
            if base in self._facts:
                container = self._facts[base].value
                if isinstance(container, Mapping) and key in container:
                    return Fact(
                        name=name,
                        value=container[key],
                        source=self._facts[base].source,
                        observed_at=self._facts[base].observed_at,
                    )

        self._missing_accessed.append(name)
        raise MissingFact(name)

    def get_raw(self, name: str) -> Any:
        """Retrieve the raw Python value of a fact."""
        return self.get_fact(name).value

    # --- Typed Python accessors ---

    def get_int(self, name: str) -> int:
        """Retrieve an integer fact value."""
        val = self.get_raw(name)
        if isinstance(val, bool) or not isinstance(val, int):
            raise TypeError(f"Fact '{name}' expected int, got {type(val).__name__}")
        return val

    def get_bool(self, name: str) -> bool:
        """Retrieve a boolean fact value."""
        val = self.get_raw(name)
        if not isinstance(val, bool):
            raise TypeError(f"Fact '{name}' expected bool, got {type(val).__name__}")
        return val

    def get_float(self, name: str) -> float:
        """Retrieve a float fact value."""
        val = self.get_raw(name)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise TypeError(f"Fact '{name}' expected float/int, got {type(val).__name__}")
        return float(val)

    def get_str(self, name: str) -> str:
        """Retrieve a string fact value."""
        val = self.get_raw(name)
        if not isinstance(val, str):
            raise TypeError(f"Fact '{name}' expected str, got {type(val).__name__}")
        return val

    def get_datetime(self, name: str) -> datetime:
        """Retrieve a datetime fact value."""
        val = self.get_raw(name)
        if isinstance(val, datetime):
            return val
        if isinstance(val, str):
            return datetime.fromisoformat(val)
        raise TypeError(
            f"Fact '{name}' expected datetime or ISO-8601 str, got {type(val).__name__}"
        )

    def get_set(self, name: str) -> set[Any]:
        """Retrieve a set fact value."""
        val = self.get_raw(name)
        if isinstance(val, (set, frozenset, list, tuple)):
            return set(val)
        raise TypeError(f"Fact '{name}' expected set/collection, got {type(val).__name__}")

    # --- Typed Z3 Constant Accessors ---

    def int(self, name: str) -> z3.ArithRef:
        """Return a Z3 Int constant for this fact, asserting const == fact_val."""
        val = self.get_int(name)
        if name not in self._constants:
            const = z3.Int(name)
            self._constants[name] = const
            self._assertions.append(const == z3.IntVal(val))
        return self._constants[name]

    def bool(self, name: str) -> z3.BoolRef:
        """Return a Z3 Bool constant for this fact, asserting const == fact_val."""
        val = self.get_bool(name)
        if name not in self._constants:
            const = z3.Bool(name)
            self._constants[name] = const
            self._assertions.append(const == z3.BoolVal(val))
        return self._constants[name]

    def real(self, name: str) -> z3.ArithRef:
        """Return a Z3 Real constant for this fact, asserting const == fact_val."""
        val = self.get_float(name)
        if name not in self._constants:
            const = z3.Real(name)
            self._constants[name] = const
            self._assertions.append(const == z3.RealVal(val))
        return self._constants[name]

    def string(self, name: str) -> z3.SeqRef:
        """Return a Z3 String constant for this fact, asserting const == fact_val."""
        val = self.get_str(name)
        if name not in self._constants:
            const = z3.String(name)
            self._constants[name] = const
            self._assertions.append(const == z3.StringVal(val))
        return self._constants[name]

    def datetime(self, name: str) -> z3.ArithRef:
        """Return a Z3 Real timestamp constant for this datetime fact."""
        dt = self.get_datetime(name)
        ts = dt.timestamp()
        if name not in self._constants:
            const = z3.Real(name)
            self._constants[name] = const
            self._assertions.append(const == z3.RealVal(ts))
        return self._constants[name]

    # --- Direct Z3 Value Helpers ---

    def int_val(self, name: str) -> z3.IntNumRef:
        """Return a concrete Z3 IntVal for this fact."""
        return z3.IntVal(self.get_int(name))

    def bool_val(self, name: str) -> z3.BoolRef:
        """Return a concrete Z3 BoolVal for this fact."""
        return z3.BoolVal(self.get_bool(name))

    def real_val(self, name: str) -> z3.RatNumRef:
        """Return a concrete Z3 RealVal for this fact."""
        return z3.RealVal(self.get_float(name))

    def string_val(self, name: str) -> z3.SeqRef:
        """Return a concrete Z3 StringVal for this fact."""
        return z3.StringVal(self.get_str(name))

    def datetime_val(self, name: str) -> z3.RatNumRef:
        """Return a concrete Z3 RealVal timestamp for this datetime fact."""
        return z3.RealVal(self.get_datetime(name).timestamp())

    # --- Fact Requirement Validation ---

    def check_required_facts(self, required_facts: Sequence[str]) -> list[str]:
        """Return list of required fact names missing from context."""
        return [f for f in required_facts if not self.has_fact(f)]

    def require_facts(self, *names: str) -> None:
        """Assert all required facts exist, raising MissingFact if any are absent."""
        missing = self.check_required_facts(names)
        if missing:
            for m in missing:
                if m not in self._missing_accessed:
                    self._missing_accessed.append(m)
            raise MissingFact(missing)


__all__ = [
    "Invariant",
    "KernelContext",
    "MissingFact",
]
