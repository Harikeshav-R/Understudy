"""Unit tests validating package import boundaries via AST analysis.

Enforces AGENTS.md §5.2:
- No package may import a sibling's internals — only its api.py and contracts.
"""

import ast
from pathlib import Path

UNDERSTUDY_SRC = Path(__file__).resolve().parent.parent.parent / "src" / "understudy"

PACKAGES = [
    "actuator",
    "eval",
    "fleet",
    "graph",
    "kernel",
    "mirror",
    "notify",
    "orchestrator",
    "planner",
    "playbook",
    "shadow",
    "signals",
    "store",
    "tournament",
]


def test_no_sibling_internal_imports() -> None:
    """Verify that no package imports internal modules of sibling packages."""
    violations: list[str] = []

    for py_file in UNDERSTUDY_SRC.rglob("*.py"):
        rel_path = py_file.relative_to(UNDERSTUDY_SRC)
        parts = rel_path.parts
        if not parts:
            continue
        current_pkg = parts[0]
        if current_pkg not in PACKAGES:
            continue

        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_module = node.module
                if not imported_module.startswith("understudy."):
                    continue
                mod_parts = imported_module.split(".")
                if len(mod_parts) >= 2:
                    target_pkg = mod_parts[1]
                    # If targeting a sibling package
                    if target_pkg in PACKAGES and target_pkg != current_pkg:
                        # Allowed targets: api, contracts, or fakes (for test/fake builders)
                        allowed_submodules = {"api"}
                        if py_file.name == "fakes.py":
                            allowed_submodules.add("fakes")
                        if len(mod_parts) > 2:
                            submod = mod_parts[2]
                            if submod not in allowed_submodules:
                                violations.append(
                                    f"{rel_path}:{node.lineno} imports forbidden sibling internal "
                                    f"'{imported_module}'"
                                )

    assert not violations, "Sibling internal import violations found:\n" + "\n".join(violations)
