"""Deterministic side of the patch pipeline (decision #3): the model rewrites a function,
THIS module computes the diff and checks syntax — the model never counts lines.

Only python + javascript are patch-supported (Task 14 scope): both have a zero-install native
syntax gate (stdlib `compile()`, `node --check`). go/csharp/rust localize only.
"""
from __future__ import annotations

import difflib
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Parser

from incidentiq.retrieval.ast_grammars import load_grammars
from incidentiq.retrieval.function_locator import FUNCTION_NODE_TYPES

_NODE_CHECK_TIMEOUT = 15.0


@dataclass
class PatchAttempt:
    unified_diff: str
    syntax_valid: bool
    scope_ok: bool


def _splice(file_text: str, start_line: int, end_line: int, new_body: str) -> str:
    """Replace the 1-indexed inclusive line span [start_line, end_line] with new_body."""
    lines = file_text.splitlines(keepends=True)
    newline = "\n"
    replacement = new_body if new_body.endswith("\n") else new_body + newline
    spliced = lines[: start_line - 1] + [replacement] + lines[end_line:]
    return "".join(spliced)


def _unified_diff(original: str, modified: str, rel_path: str) -> str:
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        modified.splitlines(keepends=True),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
    )
    return "".join(diff)


def _python_syntax_ok(modified_text: str, rel_path: str) -> bool:
    try:
        compile(modified_text, rel_path, "exec")
        return True
    except SyntaxError:
        return False


def _javascript_syntax_ok(modified_text: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=True) as f:
        f.write(modified_text)
        f.flush()
        try:
            result = subprocess.run(
                ["node", "--check", f.name],
                capture_output=True, timeout=_NODE_CHECK_TIMEOUT, text=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
    return result.returncode == 0


def _syntax_ok(modified_text: str, language: str, rel_path: str) -> bool:
    if language == "python":
        return _python_syntax_ok(modified_text, rel_path)
    if language == "javascript":
        return _javascript_syntax_ok(modified_text)
    return False                                          # unreachable for patch-supported langs


def _scope_ok(new_body: str, language: str, function_name: str) -> bool:
    """SF-5: the regenerated code must be exactly ONE top-level function with the SAME name.

    Guards against the model renaming the function, splitting the fix across new helper
    functions, or dumping extra top-level statements — any of which means the diff no longer
    maps cleanly onto the single function we localized (a misleading patch is worse than none).
    """
    grammar = load_grammars().get(language)
    function_types = FUNCTION_NODE_TYPES.get(language)
    if grammar is None or function_types is None:
        return False

    root = Parser(grammar).parse(new_body.encode()).root_node
    if root.has_error:                                    # unparseable fragment
        return False

    top_level_functions = [c for c in root.children if c.type in function_types]
    if len(top_level_functions) != 1:
        return False
    name_node = top_level_functions[0].child_by_field_name("name")
    name = name_node.text.decode("utf-8", errors="replace") if name_node else None
    return name == function_name


def build_patch_attempt(
    *, repo_root: Path, file_path: str, function_name: str,
    start_line: int, end_line: int, language: str, new_body: str,
) -> PatchAttempt:
    """Splice the regenerated function into the file, compute the diff, and gate it on
    scope (SF-5) + syntax. Pure/deterministic given its inputs — no LLM, no state."""
    scope_ok = _scope_ok(new_body, language, function_name)

    original = (repo_root / file_path).read_text()
    modified = _splice(original, start_line, end_line, new_body)
    unified_diff = _unified_diff(original, modified, file_path)
    syntax_valid = _syntax_ok(modified, language, file_path)

    return PatchAttempt(unified_diff=unified_diff, syntax_valid=syntax_valid, scope_ok=scope_ok)
