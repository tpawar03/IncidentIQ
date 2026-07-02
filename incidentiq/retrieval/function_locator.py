"""tree-sitter: given a (file, line), find the smallest enclosing function and its callers.

Every supported grammar exposes the function-like node's identifier via the `name` field,
so one walk + one field lookup works uniformly across languages (verified against real
parses for all five grammars) — no per-language name-extraction logic needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node, Parser

from incidentiq.retrieval.ast_grammars import load_grammars

# Node types that represent a callable definition, per language. Shared with
# keyword_locator.py, which scores every function in a file rather than one containing a line.
FUNCTION_NODE_TYPES: dict[str, set[str]] = {
    "python": {"function_definition"},
    "javascript": {
        "function_declaration", "function_expression", "arrow_function", "method_definition",
        "generator_function_declaration",
    },
    "go": {"function_declaration", "method_declaration"},
    "csharp": {"method_declaration", "local_function_statement", "constructor_declaration"},
    "rust": {"function_item"},
}


@dataclass
class LocatedFunction:
    function_name: str
    start_line: int             # 1-indexed, inclusive
    end_line: int                # 1-indexed, inclusive
    source_excerpt: str


def _enclosing_function(root: Node, row: int, function_types: set[str]) -> Node | None:
    """Smallest node of a function type whose span contains `row` (0-indexed)."""
    best: Node | None = None
    stack = [root]
    while stack:
        node = stack.pop()
        if node.start_point[0] <= row <= node.end_point[0]:
            if node.type in function_types:
                best = node                              # last found on the path = innermost
            stack.extend(node.children)
    return best


def locate_function(source: bytes, line: int, language: str) -> LocatedFunction | None:
    """`line` is 1-indexed (matches traceback conventions). None if no enclosing function
    of a known type contains it (e.g. module-level code, or an unrecognized language)."""
    grammars = load_grammars()
    grammar = grammars.get(language)
    function_types = FUNCTION_NODE_TYPES.get(language)
    if grammar is None or function_types is None:
        return None

    tree = Parser(grammar).parse(source)
    node = _enclosing_function(tree.root_node, line - 1, function_types)
    if node is None:
        return None

    name_node = node.child_by_field_name("name")
    name = name_node.text.decode("utf-8", errors="replace") if name_node else "<anonymous>"
    return LocatedFunction(
        function_name=name,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        source_excerpt=node.text.decode("utf-8", errors="replace"),
    )


# Source file extensions to scan when searching for callers, per language.
_LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "javascript": (".js", ".jsx", ".mjs", ".cjs"),
    "go": (".go",),
    "csharp": (".cs",),
    "rust": (".rs",),
}


def find_callers(repo_root: Path, function_name: str, language: str, *, exclude: Path | None = None) -> list[str]:
    """Best-effort caller search: text-scan the repo's same-language files for call sites
    of `function_name` (a name match, not a resolved call graph — good enough to point an
    engineer at the right places, per the CodeContext.callers contract).
    """
    extensions = _LANGUAGE_EXTENSIONS.get(language, ())
    callers: list[str] = []
    for ext in extensions:
        for path in sorted(repo_root.rglob(f"*{ext}")):
            if path == exclude or ".git" in path.parts:
                continue
            text = path.read_text(errors="replace")
            if f"{function_name}(" in text:
                callers.append(str(path.relative_to(repo_root)))
    return callers
