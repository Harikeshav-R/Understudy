"""CI parity tests asserting that documentation and invariant catalogue code agree.

Enforces build-plan step B4.5 and docs/03-invariants.md §3.4 / §3.5:
- §3.4 Invariant catalogue in docs/03-invariants.md is strictly in sync with the code.
- §3.5 Coverage summary table counts and coverage ratios match the code.
- CLI ust kernel catalogue generates identical Markdown output and supports --write.
"""

import re
import shutil
from pathlib import Path

from typer.testing import CliRunner

from understudy.cli import app
from understudy.contracts.enums import InvariantTier
from understudy.kernel.api import (
    CATALOGUE_INVARIANTS,
    generate_catalogue_markdown,
    get_invariant_counts,
    verify_catalogue_matches_docs,
)

runner = CliRunner()
DOCS_PATH = Path("docs/03-invariants.md")


def test_catalogue_matches_docs_section_3_4() -> None:
    """Assert docs/03-invariants.md §3.4 is bit-for-bit identical to code catalogue."""
    matches, diff = verify_catalogue_matches_docs(DOCS_PATH)
    assert matches, f"Invariant catalogue and docs/03-invariants.md §3.4 diverge:\n{diff}"


def test_catalogue_matches_docs_section_3_5_counts() -> None:
    """Assert §3.5 Coverage summary counts strictly match the invariant code base."""
    text = DOCS_PATH.read_text(encoding="utf-8")
    counts = get_invariant_counts()

    assert counts["PROOF"] == 8
    assert counts["RUNTIME"] == 2
    assert counts["TOTAL"] == 10

    # Match PROOF row: | PROOF | K1, K2, ... | 8 |
    proof_match = re.search(r"\|\s*PROOF\s*\|\s*([^|]+)\|\s*(\d+)\s*\|", text)
    assert proof_match is not None, "Missing PROOF row in §3.5 table"
    proof_invariants = [inv.strip() for inv in proof_match.group(1).split(",")]
    assert len(proof_invariants) == counts["PROOF"]
    assert int(proof_match.group(2)) == counts["PROOF"]
    assert proof_invariants == ["K1", "K2", "K3", "K4", "K5", "K7", "K8", "K9"]

    # Match RUNTIME row: | RUNTIME | K6, K10 | 2 |
    runtime_match = re.search(r"\|\s*RUNTIME\s*\|\s*([^|]+)\|\s*(\d+)\s*\|", text)
    assert runtime_match is not None, "Missing RUNTIME row in §3.5 table"
    runtime_invariants = [inv.strip() for inv in runtime_match.group(1).split(",")]
    assert len(runtime_invariants) == counts["RUNTIME"]
    assert int(runtime_match.group(2)) == counts["RUNTIME"]
    assert runtime_invariants == ["K6", "K10"]

    # Check reported coverage statement: "8/10 proof-backed"
    assert "8/10 proof-backed" in text


def test_all_catalogue_invariants_have_required_metadata() -> None:
    """Validate that every registered invariant satisfies catalogue contract metadata."""
    assert len(CATALOGUE_INVARIANTS) == 10
    ids = [inv.id for inv in CATALOGUE_INVARIANTS]
    assert ids == ["K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8", "K9", "K10"]

    for inv in CATALOGUE_INVARIANTS:
        assert inv.id.startswith("K")
        assert getattr(inv, "name", None)
        assert inv.tier in (InvariantTier.PROOF, InvariantTier.RUNTIME)
        assert getattr(inv, "tier_display", None)
        assert inv.statement
        if inv.tier == InvariantTier.PROOF:
            assert getattr(inv, "smt_shape", None)
            assert getattr(inv, "why_it_exists", None)
        else:
            assert getattr(inv, "enforcement", None)
            assert getattr(inv, "why_runtime", None)


def test_cli_kernel_catalogue_markdown() -> None:
    """CLI ust kernel catalogue --markdown prints identical §3.4 markdown."""
    result = runner.invoke(app, ["kernel", "catalogue", "--markdown"])
    assert result.exit_code == 0
    expected = generate_catalogue_markdown()
    assert result.stdout == expected


def test_cli_kernel_catalogue_write(tmp_path: Path) -> None:
    """CLI ust kernel catalogue --write updates target documentation file idempotently."""
    test_doc = tmp_path / "03-invariants.md"
    shutil.copyfile(DOCS_PATH, test_doc)

    # 1. Writing to a synchronized file reports already matching
    res1 = runner.invoke(app, ["kernel", "catalogue", "--write", "--docs-path", str(test_doc)])
    assert res1.exit_code == 0
    assert "already matches invariant catalogue" in res1.stdout

    # 2. Alter §3.4 content to simulate stale docs
    content = test_doc.read_text(encoding="utf-8")
    stale_content = content.replace("Replica floor", "Old Stale Replica Floor")
    test_doc.write_text(stale_content, encoding="utf-8")

    # 3. Running --write regenerates and reports update
    res2 = runner.invoke(app, ["kernel", "catalogue", "--write", "--docs-path", str(test_doc)])
    assert res2.exit_code == 0
    assert f"Updated §3.4 in {test_doc}" in res2.stdout

    # 4. Verify file is restored to match golden
    matches, _ = verify_catalogue_matches_docs(test_doc)
    assert matches is True


def test_cli_kernel_catalogue_missing_file(tmp_path: Path) -> None:
    """CLI ust kernel catalogue --write reports error when target file does not exist."""
    missing = tmp_path / "nonexistent.md"
    result = runner.invoke(app, ["kernel", "catalogue", "--write", "--docs-path", str(missing)])
    assert result.exit_code != 0
    assert "documentation file not found" in result.stderr
