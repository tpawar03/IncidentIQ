"""ast_code_retriever (Task 13): cache-aware shallow clone @ deploy commit -> tree-sitter
-> offending function + callers -> CodeContext (AGENT_ORCHESTRATION Flow A step 6).

Traceback present -> parse file/line, locate the enclosing function (`via="traceback"`).
Traceback missing, unparseable, or pointing at a file that no longer exists at this commit
-> keyword search on probable_cause/summary (FR-13, `via="keyword_fallback"`). Clone/index
failure raises a typed error (`clone_timeout`) that the graph node turns into escalation —
this agent never talks to an LLM, so there's nothing else that can fail here.
"""
from __future__ import annotations

from incidentiq.errors import llm_error
from incidentiq.retrieval.code_clone import clone_at_commit
from incidentiq.retrieval.function_locator import LocatedFunction, find_callers, locate_function
from incidentiq.retrieval.keyword_locator import keyword_locate
from incidentiq.retrieval.traceback_locator import locate as locate_traceback
from incidentiq.state import CodeContext, IncidentContext, RCAReport

# CodeContext.patch_supported: True only for languages patch_generator (Task 14) can
# regenerate + syntax-validate with a ZERO-INSTALL native validator on this box —
# py_compile (stdlib) + `node --check`. go/csharp/rust localize only (code_context_only):
# go was dropped from the patch set with C#/Rust (decision, Task 14) to avoid depending on a
# toolchain we can't validate against here; a wrong-but-unvalidated diff is worse than a TODO.
_PATCH_SUPPORTED_LANGUAGES = {"python", "javascript"}


def _from_located(
    *, language: str, file_path: str, located: LocatedFunction | None, via: str,
    repo_root, fallback_line: int | None = None,
) -> CodeContext:
    callers: list[str] = []
    if located is not None:
        callers = find_callers(repo_root, located.function_name, language, exclude=repo_root / file_path)
    return CodeContext(
        language=language,
        file_path=file_path,
        function_name=located.function_name if located else None,
        start_line=located.start_line if located else fallback_line,
        end_line=located.end_line if located else fallback_line,
        callers=callers,
        source_excerpt=located.source_excerpt if located else None,
        retrieval_ok=True,
        via=via,
        patch_supported=language in _PATCH_SUPPORTED_LANGUAGES,
    )


async def retrieve_code_context(
    incident: IncidentContext, rca: RCAReport | None, *, clone_fn=clone_at_commit,
) -> CodeContext:
    """Entry point for the `ast_code_retriever` graph node."""
    if not incident.repo_url or not incident.deploy_commit:
        raise llm_error(
            "other", "no repo_url/deploy_commit on IncidentContext", node="ast_code_retriever",
        )

    repo_root = clone_fn(incident.repo_url, incident.deploy_commit)   # raises clone_timeout on failure

    traceback_loc = locate_traceback(incident.traceback)
    if traceback_loc is not None:
        abs_path = repo_root / traceback_loc.file_path
        if abs_path.is_file():
            located = locate_function(abs_path.read_bytes(), traceback_loc.line, traceback_loc.language)
            return _from_located(
                language=traceback_loc.language, file_path=traceback_loc.file_path,
                located=located, via="traceback", repo_root=repo_root, fallback_line=traceback_loc.line,
            )
        # Traceback named a path that isn't in this commit's tree (rename/stale trace) — fall
        # through to keyword search rather than giving up on an otherwise-usable traceback.

    probable_cause = rca.probable_cause if rca is not None else ""
    match = keyword_locate(repo_root, probable_cause, incident.summary)
    if match is None:
        return CodeContext(
            language="unsupported", retrieval_ok=False, via="keyword_fallback", patch_supported=False,
        )

    return _from_located(
        language=match.language, file_path=match.file_path,
        located=match.located, via="keyword_fallback", repo_root=repo_root,
    )
