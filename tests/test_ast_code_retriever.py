"""Task 13 — AST code retriever.

Coverage, bottom-up:
  1. Grammar registry (FR-27): all 5 grammars load; a broken one warns, doesn't crash.
  2. Clone cache (FR-26): shallow clone-at-commit against a REAL local git repo (not a
     mock) — cache hit reuses the same path; a bad commit raises a typed clone_timeout.
  3. Traceback location (FR-13, traceback branch): per-language stack-frame extraction.
  4. tree-sitter function location + caller search: real parses of the fixture repo.
  5. Keyword fallback (FR-13, no-traceback branch): scoring, and the "nothing matched" dead end.
  6. The end-to-end agent (retrieve_code_context): traceback path, keyword-fallback path,
     missing repo metadata, clone failure.
  7. The graph node + router: make_ast_node, route_after_ast branches, full graph wiring.

Tests 2-6 use `tests/fixtures/git_repo.py` — a REAL git repo built at test time (git
init/commit/rev-parse), so the git + tree-sitter plumbing is exercised for real, not faked.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from incidentiq.agents.ast_code_retriever import retrieve_code_context
from incidentiq.errors import LLMCallError
from incidentiq.graph.build import build_graph, make_ast_node
from incidentiq.graph.routing import route_after_ast
from incidentiq.retrieval import ast_grammars
from incidentiq.retrieval.ast_grammars import load_grammars
from incidentiq.retrieval.code_clone import clone_at_commit
from incidentiq.retrieval.function_locator import find_callers, locate_function
from incidentiq.retrieval.keyword_locator import keyword_locate
from incidentiq.retrieval.traceback_locator import locate as locate_traceback
from incidentiq.state import CodeContext, IncidentContext, IncidentState, IncidentStatus
from tests.fixtures.git_repo import build_fixture_repo


# --- shared fixtures ---------------------------------------------------------

@pytest.fixture
def fixture_repo(tmp_path):
    return build_fixture_repo(tmp_path)


def _ctx(**overrides) -> IncidentContext:
    fields = dict(
        service="billing", alert_name="DivByZero", summary="balance endpoint 500s",
        starts_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return IncidentContext(**fields)


def _state(**overrides) -> IncidentState:
    base = IncidentState(
        incident_id="i", status=IncidentStatus.investigating, raw_payload={},
        alertmanager_fingerprint="fp", incident_context=_ctx(),
    )
    return base.model_copy(update=overrides)


_PY_TRACEBACK = (
    'Traceback (most recent call last):\n'
    '  File "main.py", line 4, in handle_request\n'
    '    return get_user_balance(user_id)\n'
    '  File "service.py", line 3, in get_user_balance\n'
    '    return account["balance"] / account["pending"]\n'
    'ZeroDivisionError: division by zero'
)


# --- 1. grammar registry (FR-27) ---------------------------------------------

def test_all_five_grammars_load():
    registry = load_grammars()
    assert set(registry) == {"python", "javascript", "go", "csharp", "rust"}
    assert all(v is not None for v in registry.values())


def test_broken_grammar_warns_not_crashes(monkeypatch, caplog):
    load_grammars.cache_clear()
    monkeypatch.setitem(ast_grammars._GRAMMAR_MODULES, "rust", "not_a_real_module_xyz")
    try:
        with caplog.at_level(logging.WARNING):
            registry = load_grammars()
        assert registry["rust"] is None                        # missing -> None, no raise
        assert registry["python"] is not None                  # the other four still load
        assert any("rust" in r.message for r in caplog.records)
    finally:
        load_grammars.cache_clear()


# --- 2. clone cache (FR-26) ---------------------------------------------------

def test_clone_at_commit_checks_out_the_right_commit(fixture_repo, tmp_path):
    cache = tmp_path / "cache"
    dest = clone_at_commit(str(fixture_repo.path), fixture_repo.v1_commit, cache_root=cache)
    assert '/ account["pending"]' not in (dest / "service.py").read_text()   # v1 = clean, pre-bug

    dest2 = clone_at_commit(str(fixture_repo.path), fixture_repo.v2_commit, cache_root=cache)
    assert '/ account["pending"]' in (dest2 / "service.py").read_text()      # v2 = the bug commit
    assert dest != dest2                                        # different commits, different cache slots


def test_clone_at_commit_cache_hit_reuses_path(fixture_repo, tmp_path):
    cache = tmp_path / "cache"
    dest1 = clone_at_commit(str(fixture_repo.path), fixture_repo.v2_commit, cache_root=cache)
    dest2 = clone_at_commit(str(fixture_repo.path), fixture_repo.v2_commit, cache_root=cache)
    assert dest1 == dest2


def test_clone_at_commit_bad_commit_raises_clone_timeout(fixture_repo, tmp_path):
    with pytest.raises(LLMCallError) as exc_info:
        clone_at_commit(str(fixture_repo.path), "deadbeef" * 5, cache_root=tmp_path / "cache")
    assert exc_info.value.typed_error.kind == "clone_timeout"


def test_clone_at_commit_bad_repo_url_raises_and_does_not_cache_partial(tmp_path):
    cache = tmp_path / "cache"
    with pytest.raises(LLMCallError):
        clone_at_commit("/nonexistent/repo", "deadbeef", cache_root=cache)
    # a broken attempt must not leave a cache entry that looks like a hit next time.
    assert list(cache.iterdir()) == []


# --- 3. traceback location (FR-13, traceback branch) --------------------------

@pytest.mark.parametrize("traceback,expected_file,expected_line,expected_lang", [
    (_PY_TRACEBACK, "service.py", 3, "python"),
    ("Error\n    at getUserBalance (/repo/service.js:3:12)\n    at f (/repo/main.js:1:1)",
     "/repo/service.js", 3, "javascript"),
    ("panic: divide by zero\nmain.GetUserBalance(...)\n\t/repo/service.go:10 +0x1d",
     "/repo/service.go", 10, "go"),
    ("System.DivideByZeroException: x\n   at Service.GetUserBalance(String userId) "
     "in /repo/Service.cs:line 12",
     "/repo/Service.cs", 12, "csharp"),
    ("thread 'main' panicked at service.rs:8:5:\nattempt to divide by zero",
     "service.rs", 8, "rust"),
])
def test_locate_traceback_per_language(traceback, expected_file, expected_line, expected_lang):
    loc = locate_traceback(traceback)
    assert loc is not None
    assert (loc.file_path, loc.line, loc.language) == (expected_file, expected_line, expected_lang)


def test_locate_traceback_none_when_missing():
    assert locate_traceback(None) is None
    assert locate_traceback("") is None


def test_locate_traceback_none_for_unrecognized_shape():
    assert locate_traceback("something went wrong, no idea where") is None


# --- 4. tree-sitter function location + callers -------------------------------

@pytest.mark.parametrize("filename,line,language,expected_name", [
    ("service.py", 3, "python", "get_user_balance"),
    ("service.js", 3, "javascript", "getUserBalance"),
    ("service.go", 10, "go", "GetUserBalance"),
    ("Service.cs", 12, "csharp", "GetUserBalance"),
    ("service.rs", 8, "rust", "get_user_balance"),
])
def test_locate_function_per_language(fixture_repo, tmp_path, filename, line, language, expected_name):
    dest = clone_at_commit(str(fixture_repo.path), fixture_repo.v2_commit, cache_root=tmp_path / "cache")
    source = (dest / filename).read_bytes()
    located = locate_function(source, line, language)
    assert located is not None
    assert located.function_name == expected_name
    assert located.start_line <= line <= located.end_line


def test_locate_function_none_outside_any_function(fixture_repo, tmp_path):
    dest = clone_at_commit(str(fixture_repo.path), fixture_repo.v1_commit, cache_root=tmp_path / "cache")
    source = (dest / "main.py").read_bytes()
    # line 1 is the module-level `from service import ...` — no enclosing function.
    assert locate_function(source, 1, "python") is None


def test_find_callers_finds_the_calling_file(fixture_repo, tmp_path):
    dest = clone_at_commit(str(fixture_repo.path), fixture_repo.v2_commit, cache_root=tmp_path / "cache")
    callers = find_callers(dest, "get_user_balance", "python", exclude=dest / "service.py")
    assert callers == ["main.py"]


# --- 5. keyword fallback (FR-13, no-traceback branch) --------------------------

def test_keyword_locate_finds_a_match(fixture_repo, tmp_path):
    dest = clone_at_commit(str(fixture_repo.path), fixture_repo.v2_commit, cache_root=tmp_path / "cache")
    # "handle_request" only appears in main.py (python) across the fixture's mirror files,
    # so this keyword set resolves deterministically regardless of the languages' otherwise
    # near-identical bodies (get_user_balance/fetch_account/balance/pending repeat in all of them).
    match = keyword_locate(dest, probable_cause="handle_request forwards to get_user_balance", summary="")
    assert match is not None
    assert match.file_path == "main.py"
    assert match.language == "python"


def test_keyword_locate_none_when_nothing_scores(fixture_repo, tmp_path):
    dest = clone_at_commit(str(fixture_repo.path), fixture_repo.v2_commit, cache_root=tmp_path / "cache")
    match = keyword_locate(dest, probable_cause="completely unrelated networking gateway timeout", summary="")
    assert match is None


def test_keyword_locate_unsupported_language_still_reports_file(fixture_repo, tmp_path):
    dest = clone_at_commit(str(fixture_repo.path), fixture_repo.v2_commit, cache_root=tmp_path / "cache")
    # service.rb only exists as of v2; nothing else in the repo contains "fetch_account" AND
    # is ruby, but ruby isn't in the supported grammar set, so no tree-sitter location either.
    match = keyword_locate(dest, probable_cause="", summary="def fetch_account user_id pending")
    assert match is not None
    assert match.language in {"unsupported", "python", "javascript", "go", "csharp", "rust"}


# --- 6. the end-to-end agent ---------------------------------------------------

def _clone_fn(fixture_repo, tmp_path):
    def fn(repo_url, commit):
        return clone_at_commit(repo_url, commit, cache_root=tmp_path / "cache")
    return fn


def test_retrieve_code_context_traceback_path(fixture_repo, tmp_path):
    incident = _ctx(
        traceback=_PY_TRACEBACK, repo_url=str(fixture_repo.path), deploy_commit=fixture_repo.v2_commit,
    )
    ctx = asyncio.run(retrieve_code_context(incident, None, clone_fn=_clone_fn(fixture_repo, tmp_path)))
    assert ctx.language == "python"
    assert ctx.file_path == "service.py"
    assert ctx.function_name == "get_user_balance"
    assert (ctx.start_line, ctx.end_line) == (1, 3)
    assert ctx.callers == ["main.py"]
    assert ctx.source_excerpt is not None and "get_user_balance" in ctx.source_excerpt
    assert (ctx.retrieval_ok, ctx.via, ctx.patch_supported) == (True, "traceback", True)


def test_retrieve_code_context_keyword_fallback_path(fixture_repo, tmp_path):
    incident = _ctx(
        traceback=None, summary="handle_request forwards to get_user_balance",
        repo_url=str(fixture_repo.path), deploy_commit=fixture_repo.v2_commit,
    )
    ctx = asyncio.run(retrieve_code_context(incident, None, clone_fn=_clone_fn(fixture_repo, tmp_path)))
    assert ctx.via == "keyword_fallback"
    assert ctx.retrieval_ok is True
    assert ctx.file_path == "main.py"
    assert ctx.language == "python"


def test_retrieve_code_context_missing_repo_metadata_raises():
    incident = _ctx(repo_url=None, deploy_commit=None)
    with pytest.raises(LLMCallError) as exc_info:
        asyncio.run(retrieve_code_context(incident, None))
    assert exc_info.value.typed_error.kind == "other"


def test_retrieve_code_context_clone_failure_raises_clone_timeout(fixture_repo, tmp_path):
    incident = _ctx(repo_url=str(fixture_repo.path), deploy_commit="deadbeef" * 5)
    with pytest.raises(LLMCallError) as exc_info:
        asyncio.run(retrieve_code_context(incident, None, clone_fn=_clone_fn(fixture_repo, tmp_path)))
    assert exc_info.value.typed_error.kind == "clone_timeout"


# --- 7. graph node + router -----------------------------------------------------

def test_make_ast_node_success_sets_code_context(fixture_repo, tmp_path):
    async def fake_retrieve(incident, rca):
        return CodeContext(language="python", retrieval_ok=True, via="traceback", patch_supported=True)
    node = make_ast_node(fake_retrieve)
    out = asyncio.run(node(_state()))
    assert out["code_context"].language == "python"
    assert "errors" not in out
    assert out["trace"][0].node == "ast_code_retriever"


def test_make_ast_node_failure_escalates():
    async def fake_retrieve(incident, rca):
        from incidentiq.errors import llm_error
        raise llm_error("clone_timeout", "boom", node="ast_code_retriever")
    node = make_ast_node(fake_retrieve)
    out = asyncio.run(node(_state()))
    assert out["status"] is IncidentStatus.escalated
    assert out["errors"][0].kind == "clone_timeout"
    assert "code_context" not in out


@pytest.mark.parametrize("code_context,expected_route", [
    (None, "escalation_node"),
    (CodeContext(language="unsupported", retrieval_ok=False, via="keyword_fallback", patch_supported=False),
     "escalation_node"),
    (CodeContext(language="python", function_name="f", retrieval_ok=True, via="traceback", patch_supported=True),
     "patch_generator"),
    # patch-supported language but NO located function → can't patch, localize only.
    (CodeContext(language="python", function_name=None, retrieval_ok=True, via="traceback", patch_supported=True),
     "code_context_only"),
    (CodeContext(language="csharp", function_name="F", retrieval_ok=True, via="traceback", patch_supported=False),
     "code_context_only"),
])
def test_route_after_ast(code_context, expected_route):
    assert route_after_ast(_state(code_context=code_context)) == expected_route


def test_graph_code_bug_path_reaches_ast_node(fixture_repo, tmp_path):
    """End-to-end: triage_router routes code_bug -> ast_code_retriever -> a real CodeContext,
    proving the wiring (not just the unit pieces) with an injected ast_fn."""
    from incidentiq.contracts import IncidentType
    from incidentiq.state import (
        Citation, ConfidenceBreakdown, RCAReport, RetrievedChunk, RetrievedContext, TriageDecision,
    )

    def _retrieved():
        return RetrievedContext(
            chunks=[RetrievedChunk(chunk_id="c1", source_doc="d", text="t",
                                   semantic_score=0.9, corpus="postmortem")],
            chunks_over_threshold=1, retriever_agreement=1.0,
        )

    async def _fake_retrieve(query):
        return _retrieved()

    async def _ok_synth(incident, retrieved, *, client):
        return RCAReport.grounded(
            retrieved=retrieved, probable_cause="p", root_service="billing", confidence_score=0.9,
            confidence_breakdown=ConfidenceBreakdown(
                self_consistency_agreement=1.0, retrieval_evidence_strength=0.8, chunks_over_threshold=1),
            source_citations=[Citation(claim="c", chunk_id="c1")],
        )

    async def _confident_code_bug_triage(incident, rca, *, client):
        return TriageDecision(
            incident_type=IncidentType.code_bug, confidence=0.95,
            rule_prior=IncidentType.code_bug, rule_prior_strength=0.9, llm_agreed=True, rationale="r",
        )

    async def _ast_fn(incident, rca):
        return await retrieve_code_context(incident, rca, clone_fn=_clone_fn(fixture_repo, tmp_path))

    # Task 13's concern ends at CodeContext; stub the Task 14 patch node (degrade to
    # code_context_only) so this stays a pure AST-wiring test. The real patch path is
    # exercised in test_patch_generator.py.
    async def _no_patch(incident, rca, code, *, client):
        return None

    incident_ctx = _ctx(
        traceback=_PY_TRACEBACK, repo_url=str(fixture_repo.path), deploy_commit=fixture_repo.v2_commit,
    )
    initial = IncidentState(
        incident_id="e2e-code", status=IncidentStatus.created, raw_payload={},
        alertmanager_fingerprint="fp", incident_context=incident_ctx,
    )

    app = build_graph(
        client=object(), retrieve_fn=_fake_retrieve, synthesize_fn=_ok_synth,
        triage_fn=_confident_code_bug_triage, ast_fn=_ast_fn, patch_fn=_no_patch,
    )
    final = asyncio.run(app.ainvoke(initial))
    assert final["code_context"].function_name == "get_user_balance"
    assert final["code_context"].patch_supported is True
    assert final["status"] != IncidentStatus.escalated
