"""Shared PyYAML dumping helpers for deploy/generate_*_manifests.py.

Not a manifest-generation module itself -- see generate_postgres_manifests.py and
generate_prod_manifests.py for the actual per-manifest-family generators. Factored out
so both share one indentation/literal-block-scalar convention rather than duplicating
it.
"""

from __future__ import annotations

from typing import Any

import yaml

PART_OF_LABEL = {"app.kubernetes.io/part-of": "understudy"}


class IndentedDumper(yaml.SafeDumper):
    """PyYAML's default dump doesn't indent list items under their parent key, which
    reads worse than every hand-written manifest elsewhere in deploy/. Force it."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


class LiteralStr(str):
    """A str that IndentedDumper renders as a literal block scalar (`|`) instead of
    PyYAML's default single-/double-quoted style for a string containing a newline."""


def _represent_literal_str(dumper: IndentedDumper, data: "LiteralStr") -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


IndentedDumper.add_representer(LiteralStr, _represent_literal_str)


def dump_documents(docs: list[dict[str, Any]]) -> str:
    """Render a list of manifest documents as one `---`-separated YAML text."""
    return "---\n".join(
        yaml.dump(doc, Dumper=IndentedDumper, default_flow_style=False, sort_keys=False)
        for doc in docs
    )
