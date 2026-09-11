#!/usr/bin/env python3
"""Validate bidirectional parity between # MOCKED: markers and docs/MOCKS.md.

Enforces AGENTS.md §7.3:
Every # MOCKED: marker has a row in docs/MOCKS.md with the module, the reason,
the real path, and the issue. make check runs a script that fails if a marker
exists without a row, or a row without a marker.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MOCKED_PATTERN = re.compile(r"#\s*MOCKED:\s*(.+)")


def scan_source_markers(search_dir: Path) -> dict[str, list[tuple[int, str]]]:
    """Scan Python files in search_dir for # MOCKED: markers."""
    markers: dict[str, list[tuple[int, str]]] = {}
    if not search_dir.exists():
        return markers

    for py_file in search_dir.rglob("*.py"):
        rel_parts = py_file.relative_to(search_dir).parts
        if any(part.startswith((".", "__pycache__", "venv")) for part in rel_parts):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
        except OSError:
            continue

        for idx, line in enumerate(content.splitlines(), start=1):
            match = MOCKED_PATTERN.search(line)
            if match:
                # posix separators: dotted module names are matched via "/", and this
                # must be stable across platforms (make check runs on Windows too).
                rel_path = py_file.resolve().relative_to(search_dir.resolve()).as_posix()
                markers.setdefault(rel_path, []).append((idx, match.group(1).strip()))

    return markers


def parse_mock_registry(registry_path: Path) -> list[str]:
    """Parse table rows in docs/MOCKS.md, returning registered modules."""
    if not registry_path.exists():
        return []

    lines = registry_path.read_text(encoding="utf-8").splitlines()
    modules: list[str] = []
    in_table = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            parts = [c.strip() for c in stripped.split("|")[1:-1]]
            if not parts:
                continue
            first_col = parts[0]
            if first_col in ("Module", "---", "") or first_col.startswith("---"):
                in_table = True
                continue
            if in_table:
                # Check for empty / sentinel placeholder
                if first_col == "_(none yet)_" or first_col.startswith("_"):
                    continue
                modules.append(first_col)
        elif in_table:
            # Reached end of markdown table
            in_table = False

    return modules


def _module_matches_file(reg_mod: str, file_path: str) -> bool:
    """Return True if a registered dotted module path exactly names a marker's file."""
    return file_path == reg_mod.replace(".", "/") + ".py"


def check_parity(repo_root: Path, mocks_file: Path | None = None) -> tuple[bool, list[str]]:
    """Verify exact parity between code markers and MOCKS.md table rows."""
    registry_file = mocks_file or (repo_root / "docs" / "MOCKS.md")
    src_dir = repo_root / "src"
    services_dir = repo_root / "services"

    code_markers = scan_source_markers(src_dir)
    # services/ is a separate package tree from src/understudy (no shared root_package),
    # so its own dotted module names include the "services" segment itself; re-key its
    # markers with that prefix rather than treating services_dir as the scan root.
    for file_path, occurrences in scan_source_markers(services_dir).items():
        code_markers[f"services/{file_path}"] = occurrences

    registered_modules = parse_mock_registry(registry_file)

    errors: list[str] = []

    # Check 1: Code markers missing in MOCKS.md
    for file_path, occurrences in code_markers.items():
        matched = any(_module_matches_file(reg_mod, file_path) for reg_mod in registered_modules)
        if not matched:
            for line_no, desc in occurrences:
                errors.append(
                    f"Unregistered # MOCKED: marker at {file_path}:{line_no} "
                    f"('{desc}') has no matching row in {registry_file.name}"
                )

    # Check 2: MOCKS.md rows with no code marker
    for reg_mod in registered_modules:
        matched = any(_module_matches_file(reg_mod, file_path) for file_path in code_markers)
        if not matched:
            errors.append(
                f"Row in {registry_file.name} for '{reg_mod}' has no corresponding "
                f"# MOCKED: marker in source code"
            )

    return len(errors) == 0, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify # MOCKED: markers against docs/MOCKS.md")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root path")
    parser.add_argument("--mocks-file", type=Path, default=None, help="Path to MOCKS.md")
    args = parser.parse_args()

    ok, errors = check_parity(args.root, args.mocks_file)
    if not ok:
        print("MOCK REGISTRY INTEGRITY CHECK FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("OK: Mock registry parity check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
