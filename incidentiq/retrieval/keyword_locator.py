"""Keyword-search localization when a traceback isn't available (FR-13 fallback branch).

Scores every source file in the cloned repo by how many keywords drawn from the RCA's
probable_cause + the incident summary it contains, picks the best file, then (for a
supported language) picks the best-scoring function inside it — same signal, coarse to fine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Parser

from incidentiq.retrieval.ast_grammars import load_grammars
from incidentiq.retrieval.function_locator import FUNCTION_NODE_TYPES, LocatedFunction

_STOPWORDS = {
    "the", "a", "an", "and", "or", "is", "was", "were", "with", "for", "that", "this",
    "from", "into", "when", "then", "than", "due", "to", "of", "in", "on", "at", "has",
    "have", "had", "not", "but", "are", "been",
    "error", "exception", "failed", "failure", "caused", "cause",   # RCA-boilerplate noise
}

_SUPPORTED_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go",
    ".cs": "csharp",
    ".rs": "rust",
}
# Scanned but never tree-sitter-parsed: a text hit beats reporting nothing (FR-13), but the
# contract's language enum has no slot for them — reported as "unsupported".
_UNSUPPORTED_EXTENSIONS = (".rb", ".php", ".java", ".c", ".cpp", ".h")


def _keywords(*texts: str) -> list[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", " ".join(texts).lower())
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _score(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    return sum(lowered.count(k) for k in keywords)


def _best_function(source: bytes, language: str, keywords: list[str]) -> LocatedFunction | None:
    grammar = load_grammars().get(language)
    function_types = FUNCTION_NODE_TYPES.get(language)
    if grammar is None or function_types is None:
        return None

    candidates = []
    stack = [Parser(grammar).parse(source).root_node]
    while stack:
        node = stack.pop()
        if node.type in function_types:
            candidates.append(node)
        stack.extend(node.children)
    if not candidates:
        return None

    best = max(candidates, key=lambda n: _score(n.text.decode("utf-8", errors="replace"), keywords))
    name_node = best.child_by_field_name("name")
    name = name_node.text.decode("utf-8", errors="replace") if name_node else "<anonymous>"
    return LocatedFunction(
        function_name=name,
        start_line=best.start_point[0] + 1,
        end_line=best.end_point[0] + 1,
        source_excerpt=best.text.decode("utf-8", errors="replace"),
    )


@dataclass
class KeywordMatch:
    file_path: str
    language: str                 # one of the 5 supported languages, or "unsupported"
    located: LocatedFunction | None


def keyword_locate(repo_root: Path, probable_cause: str, summary: str) -> KeywordMatch | None:
    """None if no keyword scored a hit anywhere in the repo (a genuine dead end)."""
    keywords = _keywords(probable_cause, summary)
    if not keywords:
        return None

    extensions = {**_SUPPORTED_EXTENSIONS, **{ext: "unsupported" for ext in _UNSUPPORTED_EXTENSIONS}}

    best_path, best_lang, best_score = None, None, 0
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        lang = extensions.get(path.suffix)
        if lang is None:
            continue
        score = _score(path.read_text(errors="replace"), keywords)
        if score > best_score:
            best_path, best_lang, best_score = path, lang, score

    if best_path is None:
        return None

    located = _best_function(best_path.read_bytes(), best_lang, keywords) if best_lang != "unsupported" else None
    return KeywordMatch(file_path=str(best_path.relative_to(repo_root)), language=best_lang, located=located)
