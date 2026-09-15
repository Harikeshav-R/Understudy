"""Safety kernel invariant catalogue and documentation synchronisation.

Implements build-plan step B4.5:
`ust kernel catalogue --markdown` regenerating §3.4 of docs/03-invariants.md,
plus the CI test asserting docs and code agree.
"""

from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING

from understudy.contracts.enums import InvariantTier
from understudy.kernel.invariants import (
    K1ReplicaFloor,
    K2NamespaceScope,
    K3MigrationBoundary,
    K4BlastContainment,
    K5SingleWriter,
    K6TwinEgressContainment,
    K7MutationBudget,
    K8EvidenceSufficiency,
    K9Reversibility,
    K10ActuationAuthorisation,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from understudy.kernel.dsl import Invariant

# All 10 formal safety invariants in canonical order
CATALOGUE_INVARIANTS: tuple[Invariant, ...] = (
    K1ReplicaFloor(),
    K2NamespaceScope(),
    K3MigrationBoundary(),
    K4BlastContainment(),
    K5SingleWriter(),
    K6TwinEgressContainment(),
    K7MutationBudget(),
    K8EvidenceSufficiency(),
    K9Reversibility(),
    K10ActuationAuthorisation(),
)

_CATALOGUE_SECTION_PATTERN = re.compile(
    r"(### K1\b.*?## 3.5 Coverage summary\n)",
    re.DOTALL,
)


def get_catalogue_invariants() -> list[Invariant]:
    """Return all catalogue invariants sorted by canonical numeric identifier."""
    return sorted(
        CATALOGUE_INVARIANTS,
        key=lambda inv: int(inv.id[1:]) if inv.id[1:].isdigit() else 999,
    )


def render_entry_markdown(inv: Invariant) -> str:
    """Render a single invariant into its §3.4 Markdown entry representation."""
    inv_id = inv.id
    name = getattr(inv, "name", inv_id)
    tier_display = getattr(inv, "tier_display", inv.tier.value.upper())
    statement = getattr(inv, "doc_statement", None) or inv.statement

    lines: list[str] = [
        f"### {inv_id} — {name}",
        f"**Tier:** {tier_display}",
        f"**Statement.** {statement}",
    ]

    smt_shape = getattr(inv, "smt_shape", None)
    if smt_shape:
        if smt_shape.startswith("```"):
            lines.append(f"**SMT shape.**\n{smt_shape}")
        else:
            lines.append(f"**SMT shape.** {smt_shape}")

    enforcement = getattr(inv, "enforcement", None)
    if enforcement:
        lines.append(f"**Enforcement.** {enforcement}")

    why_runtime = getattr(inv, "why_runtime", None)
    if why_runtime:
        why_label = getattr(inv, "why_runtime_label", "Why it is RUNTIME.")
        lines.append(f"**{why_label}** {why_runtime}")

    why_it_exists = getattr(inv, "why_it_exists", None)
    if why_it_exists:
        lines.append(f"**Why it exists.** {why_it_exists}")

    return "\n".join(lines) + "\n"


def generate_catalogue_markdown(invariants: Sequence[Invariant] | None = None) -> str:
    """Generate Markdown for the §3.4 Invariant Catalogue.

    The generated text matches the section in docs/03-invariants.md starting with
    `### K1 — Replica floor` through `## 3.5 Coverage summary\\n`.
    """
    inv_list = list(invariants) if invariants is not None else get_catalogue_invariants()
    rendered_entries = [render_entry_markdown(inv) for inv in inv_list]
    body = "\n---\n\n".join(rendered_entries)
    return f"{body}\n---\n\n## 3.5 Coverage summary\n"


def update_docs_catalogue(
    docs_path: Path,
    markdown: str | None = None,
) -> bool:
    """Regenerate §3.4 in docs/03-invariants.md in place.

    Returns True if the document was updated, or False if it was already identical.
    Raises ValueError if the catalogue section header cannot be found.
    """
    content = markdown if markdown is not None else generate_catalogue_markdown()
    text = docs_path.read_text(encoding="utf-8")

    match = _CATALOGUE_SECTION_PATTERN.search(text)
    if not match:
        raise ValueError(f"Could not locate §3.4 invariant catalogue section in {docs_path}")

    start_idx, end_idx = match.span(1)
    existing_section = text[start_idx:end_idx]
    if existing_section == content:
        return False

    updated_text = text[:start_idx] + content + text[end_idx:]
    docs_path.write_text(updated_text, encoding="utf-8")
    return True


def verify_catalogue_matches_docs(docs_path: Path) -> tuple[bool, str]:
    """Verify that docs/03-invariants.md matches the current invariant code.

    Returns (True, "") if docs and code agree, or (False, diff) with unified diff.
    """
    text = docs_path.read_text(encoding="utf-8")
    match = _CATALOGUE_SECTION_PATTERN.search(text)
    if not match:
        return False, f"Could not locate §3.4 invariant catalogue section in {docs_path}"

    actual_section = match.group(1)
    expected_section = generate_catalogue_markdown()

    if actual_section == expected_section:
        return True, ""

    diff = difflib.unified_diff(
        actual_section.splitlines(keepends=True),
        expected_section.splitlines(keepends=True),
        fromfile=f"{docs_path}:§3.4",
        tofile="generated:§3.4",
    )
    return False, "".join(diff)


def get_invariant_counts() -> dict[str, int]:
    """Return count summary of formal invariants by tier."""
    invariants = get_catalogue_invariants()
    proof_count = sum(1 for inv in invariants if inv.tier == InvariantTier.PROOF)
    runtime_count = sum(1 for inv in invariants if inv.tier == InvariantTier.RUNTIME)
    return {
        "PROOF": proof_count,
        "RUNTIME": runtime_count,
        "TOTAL": len(invariants),
    }


__all__ = [
    "CATALOGUE_INVARIANTS",
    "generate_catalogue_markdown",
    "get_catalogue_invariants",
    "get_invariant_counts",
    "render_entry_markdown",
    "update_docs_catalogue",
    "verify_catalogue_matches_docs",
]
