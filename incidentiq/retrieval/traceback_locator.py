"""Traceback -> (file_path, line, language) extraction (FR-13, traceback branch).

Best-effort, not a general stack-trace parser: matches each supported language's common
frame shape and returns the innermost (fault-site) frame. Python lists frames outermost ->
innermost, so its LAST match is the fault site; the other languages list innermost first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_PY = re.compile(r'File "(?P<file>[^"]+\.py)", line (?P<line>\d+)')
_CSHARP = re.compile(r'in (?P<file>[\w./\\-]+\.cs):line (?P<line>\d+)')
_RUST = re.compile(r'(?P<file>[\w./\\-]+\.rs):(?P<line>\d+)(?::\d+)?')
_GO = re.compile(r'(?P<file>[\w./\\-]+\.go):(?P<line>\d+)')
_JS = re.compile(r'(?P<file>[\w./\\-]+\.jsx?):(?P<line>\d+)(?::\d+)?')


@dataclass
class TracebackLocation:
    file_path: str
    line: int
    language: str


def locate(traceback: str | None) -> TracebackLocation | None:
    """Best-effort fault-site extraction. Returns None if no known shape matches (FR-13
    falls back to keyword search in that case)."""
    if not traceback:
        return None

    py_matches = list(_PY.finditer(traceback))
    if py_matches:
        m = py_matches[-1]                                # innermost = LAST for Python
        return TracebackLocation(file_path=m.group("file"), line=int(m.group("line")), language="python")

    for pattern, lang in ((_CSHARP, "csharp"), (_RUST, "rust"), (_GO, "go"), (_JS, "javascript")):
        m = pattern.search(traceback)                      # innermost = FIRST for these
        if m:
            return TracebackLocation(file_path=m.group("file"), line=int(m.group("line")), language=lang)

    return None
