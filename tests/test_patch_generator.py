"""Task 14 — patch generator (regen → diff → validate, decision #3).

Coverage, bottom-up:
  1. `patching.py` (deterministic core): valid py/js splice+diff+syntax; invalid syntax; the
     SF-5 scope guard (rename / extra function / non-function fragment).
  2. `patch_generator.generate_patch`: success on first try, success after one bad attempt,
     double-fail → None (degrade), scope-violation → None, an LLM that keeps erroring → None,
     unsupported/no-function preconditions → None.
  3. Graph nodes + routers: make_patch_node (success sets patch+plan; degrade sets neither),
     make_code_context_only_node (no-command plan), route_after_patch branches, and a full
     code-bug graph run that produces a syntax-valid patch end-to-end.

The deterministic pieces run against the REAL fixture git repo; the model is faked so we test
the pipeline/plumbing, never the LLM.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from incidentiq.agents.patch_generator import generate_patch
from incidentiq.contracts import PatchDraft
from incidentiq.errors import llm_error
from incidentiq.graph.build import build_graph, make_code_context_only_node, make_patch_node
from incidentiq.graph.routing import route_after_patch
from incidentiq.retrieval.code_clone import clone_at_commit
from incidentiq.retrieval.patching import build_patch_attempt
from incidentiq.state import (
    CodeContext, IncidentContext, IncidentState, IncidentStatus, Patch, RemediationClass,
)
from tests.fixtures.git_repo import build_fixture_repo


# --- shared fixtures ---------------------------------------------------------

@pytest.fixture
def fixture_repo(tmp_path):
    return build_fixture_repo(tmp_path)


def _clone_fn(fixture_repo, tmp_path):
    def fn(repo_url, commit):
        return clone_at_commit(repo_url, commit, cache_root=tmp_path / "cache")
    return fn


def _repo(fixture_repo, tmp_path):
    return clone_at_commit(str(fixture_repo.path), fixture_repo.v2_commit, cache_root=tmp_path / "cache")


# The v2 fixture bug: `return account["balance"] / account["pending"]` (pending can be 0).
_GOOD_PY = (
    'def get_user_balance(user_id):\n'
    '    account = fetch_account(user_id)\n'
    '    pending = account["pending"] or 1\n'
    '    return account["balance"] / pending'
)
_BAD_SYNTAX_PY = 'def get_user_balance(user_id)\n    return 1'                 # missing colon
_SCOPE_RENAMED_PY = 'def totally_different(user_id):\n    return 0'
_SCOPE_TWO_FUNCS_PY = (
    'def get_user_balance(user_id):\n    return helper(user_id)\n\ndef helper(user_id):\n    return 1'
)
_GOOD_JS = (
    'function getUserBalance(userId) {\n'
    '    const account = fetchAccount(userId);\n'
    '    const pending = account.pending || 1;\n'
    '    return account.balance / pending;\n'
    '}'
)
_BAD_SYNTAX_JS = 'function getUserBalance(userId) {\n    return account.balance /// ;\n'


def _code(**overrides) -> CodeContext:
    fields = dict(
        language="python", file_path="service.py", function_name="get_user_balance",
        start_line=1, end_line=3, callers=["main.py"], source_excerpt="<orig>",
        retrieval_ok=True, via="traceback", patch_supported=True,
    )
    fields.update(overrides)
    return CodeContext(**fields)


def _ctx(fixture_repo) -> IncidentContext:
    return IncidentContext(
        service="billing", alert_name="DivByZero", summary="balance endpoint 500s",
        repo_url=str(fixture_repo.path), deploy_commit=fixture_repo.v2_commit,
        starts_at=datetime.now(timezone.utc),
    )


def _state(fixture_repo, **overrides) -> IncidentState:
    base = IncidentState(
        incident_id="i", status=IncidentStatus.investigating, raw_payload={},
        alertmanager_fingerprint="fp", incident_context=_ctx(fixture_repo),
    )
    return base.model_copy(update={"code_context": _code(), **overrides})


class _FakeClient:
    """Yields a fixed sequence of PatchDrafts (or raises) — one per generate_structured call."""
    def __init__(self, *drafts):
        self._drafts = list(drafts)
        self.calls = 0

    async def generate_structured(self, prompt, schema_model):
        item = self._drafts[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def _draft(body, summary="fix"):
    return PatchDraft(new_function_body=body, summary=summary)


# --- 1. deterministic core (patching.py) -------------------------------------

def test_build_patch_attempt_valid_python(fixture_repo, tmp_path):
    repo = _repo(fixture_repo, tmp_path)
    a = build_patch_attempt(
        repo_root=repo, file_path="service.py", function_name="get_user_balance",
        start_line=1, end_line=3, language="python", new_body=_GOOD_PY,
    )
    assert a.syntax_valid and a.scope_ok
    assert a.unified_diff.startswith("--- a/service.py")
    assert "+    pending =" in a.unified_diff


def test_build_patch_attempt_invalid_python(fixture_repo, tmp_path):
    repo = _repo(fixture_repo, tmp_path)
    a = build_patch_attempt(
        repo_root=repo, file_path="service.py", function_name="get_user_balance",
        start_line=1, end_line=3, language="python", new_body=_BAD_SYNTAX_PY,
    )
    assert a.syntax_valid is False


def test_build_patch_attempt_valid_javascript(fixture_repo, tmp_path):
    repo = _repo(fixture_repo, tmp_path)
    a = build_patch_attempt(
        repo_root=repo, file_path="service.js", function_name="getUserBalance",
        start_line=1, end_line=4, language="javascript", new_body=_GOOD_JS,
    )
    assert a.syntax_valid and a.scope_ok


def test_build_patch_attempt_invalid_javascript(fixture_repo, tmp_path):
    repo = _repo(fixture_repo, tmp_path)
    a = build_patch_attempt(
        repo_root=repo, file_path="service.js", function_name="getUserBalance",
        start_line=1, end_line=4, language="javascript", new_body=_BAD_SYNTAX_JS,
    )
    assert a.syntax_valid is False


def test_scope_guard_rejects_rename(fixture_repo, tmp_path):
    repo = _repo(fixture_repo, tmp_path)
    a = build_patch_attempt(
        repo_root=repo, file_path="service.py", function_name="get_user_balance",
        start_line=1, end_line=3, language="python", new_body=_SCOPE_RENAMED_PY,
    )
    assert a.scope_ok is False


def test_scope_guard_rejects_extra_function(fixture_repo, tmp_path):
    repo = _repo(fixture_repo, tmp_path)
    a = build_patch_attempt(
        repo_root=repo, file_path="service.py", function_name="get_user_balance",
        start_line=1, end_line=3, language="python", new_body=_SCOPE_TWO_FUNCS_PY,
    )
    assert a.scope_ok is False                            # syntactically fine, but 2 top-level funcs


# --- 2. generate_patch -------------------------------------------------------

def test_generate_patch_success_first_try(fixture_repo, tmp_path):
    client = _FakeClient(_draft(_GOOD_PY, "guard divide-by-zero"))
    result = asyncio.run(generate_patch(
        _ctx(fixture_repo), None, _code(), client=client, clone_fn=_clone_fn(fixture_repo, tmp_path),
    ))
    assert result is not None
    patch, plan = result
    assert patch.syntax_valid and patch.scope_ok and patch.attempts == 1
    assert patch.unified_diff.startswith("--- a/service.py")
    assert plan.remediation_class is RemediationClass.patch
    assert plan.steps == []                               # a patch is not a shell command
    assert plan.summary == "guard divide-by-zero"


def test_generate_patch_recovers_on_second_attempt(fixture_repo, tmp_path):
    client = _FakeClient(_draft(_BAD_SYNTAX_PY), _draft(_GOOD_PY))    # 1st fails syntax, 2nd ok
    result = asyncio.run(generate_patch(
        _ctx(fixture_repo), None, _code(), client=client, clone_fn=_clone_fn(fixture_repo, tmp_path),
    ))
    assert result is not None
    patch, _ = result
    assert patch.attempts == 2 and patch.syntax_valid


def test_generate_patch_double_syntax_fail_degrades(fixture_repo, tmp_path):
    client = _FakeClient(_draft(_BAD_SYNTAX_PY), _draft(_BAD_SYNTAX_PY))
    result = asyncio.run(generate_patch(
        _ctx(fixture_repo), None, _code(), client=client, clone_fn=_clone_fn(fixture_repo, tmp_path),
    ))
    assert result is None
    assert client.calls == 2                              # exactly MAX_ATTEMPTS, no more


def test_generate_patch_scope_violation_degrades(fixture_repo, tmp_path):
    client = _FakeClient(_draft(_SCOPE_TWO_FUNCS_PY), _draft(_SCOPE_RENAMED_PY))
    result = asyncio.run(generate_patch(
        _ctx(fixture_repo), None, _code(), client=client, clone_fn=_clone_fn(fixture_repo, tmp_path),
    ))
    assert result is None


def test_generate_patch_llm_errors_degrade(fixture_repo, tmp_path):
    client = _FakeClient(llm_error("llm_timeout", "boom"), llm_error("llm_timeout", "boom"))
    result = asyncio.run(generate_patch(
        _ctx(fixture_repo), None, _code(), client=client, clone_fn=_clone_fn(fixture_repo, tmp_path),
    ))
    assert result is None


def test_generate_patch_precondition_no_function(fixture_repo, tmp_path):
    client = _FakeClient(_draft(_GOOD_PY))
    result = asyncio.run(generate_patch(
        _ctx(fixture_repo), None, _code(function_name=None),
        client=client, clone_fn=_clone_fn(fixture_repo, tmp_path),
    ))
    assert result is None
    assert client.calls == 0                              # never even prompted the model


def test_generate_patch_precondition_not_patch_supported(fixture_repo, tmp_path):
    client = _FakeClient(_draft(_GOOD_PY))
    result = asyncio.run(generate_patch(
        _ctx(fixture_repo), None, _code(language="csharp", patch_supported=False),
        client=client, clone_fn=_clone_fn(fixture_repo, tmp_path),
    ))
    assert result is None
    assert client.calls == 0


# --- 3. graph nodes + routers ------------------------------------------------

def test_make_patch_node_success_sets_patch_and_plan(fixture_repo, tmp_path):
    async def fake_patch(incident, rca, code, *, client):
        patch = Patch(new_function_body=_GOOD_PY, unified_diff="--- a/x\n+++ b/x\n",
                      syntax_valid=True, scope_ok=True, attempts=1)
        from incidentiq.state import RemediationPlan
        return patch, RemediationPlan(remediation_class=RemediationClass.patch, summary="s", steps=[])
    node = make_patch_node(object(), fake_patch)
    out = asyncio.run(node(_state(fixture_repo)))
    assert out["patch"].syntax_valid is True
    assert out["remediation_plan"].remediation_class is RemediationClass.patch
    assert out["trace"][0].node == "patch_generator"


def test_make_patch_node_degrade_sets_neither(fixture_repo, tmp_path):
    async def fake_patch(incident, rca, code, *, client):
        return None
    node = make_patch_node(object(), fake_patch)
    out = asyncio.run(node(_state(fixture_repo)))
    assert "patch" not in out and "remediation_plan" not in out
    assert out["trace"][0].node == "patch_generator"


def test_make_patch_node_llm_error_degrades_not_escalates(fixture_repo, tmp_path):
    async def fake_patch(incident, rca, code, *, client):
        raise llm_error("llm_timeout", "boom", node="patch_generator")
    node = make_patch_node(object(), fake_patch)
    out = asyncio.run(node(_state(fixture_repo)))
    assert "patch" not in out
    assert out.get("status") is not IncidentStatus.escalated   # degrade to code_context_only, not escalate


def test_code_context_only_node_emits_no_command_plan(fixture_repo, tmp_path):
    node = make_code_context_only_node()
    out = asyncio.run(node(_state(fixture_repo)))
    plan = out["remediation_plan"]
    assert plan.remediation_class is RemediationClass.none
    assert plan.steps == []
    assert "get_user_balance" in plan.summary
    assert out["trace"][0].node == "code_context_only"


@pytest.mark.parametrize("patch,expected", [
    (None, "code_context_only"),
    (Patch(new_function_body="x", unified_diff="d", syntax_valid=False, scope_ok=True, attempts=2),
     "code_context_only"),
    (Patch(new_function_body="x", unified_diff="d", syntax_valid=True, scope_ok=False, attempts=2),
     "code_context_only"),
    (Patch(new_function_body="x", unified_diff="d", syntax_valid=True, scope_ok=True, attempts=1),
     "human_checkpoint"),
])
def test_route_after_patch(fixture_repo, patch, expected):
    assert route_after_patch(_state(fixture_repo, patch=patch)) == expected


def test_graph_code_bug_produces_patch_end_to_end(fixture_repo, tmp_path):
    """Full path: triage(code_bug) → ast_code_retriever → patch_generator → a syntax-valid
    Patch + a class=patch RemediationPlan, with only the model faked."""
    from incidentiq.agents.ast_code_retriever import retrieve_code_context
    from incidentiq.contracts import IncidentType
    from incidentiq.state import (
        Citation, ConfidenceBreakdown, RCAReport, RetrievedChunk, RetrievedContext, TriageDecision,
    )

    _PY_TRACEBACK = (
        'Traceback (most recent call last):\n'
        '  File "service.py", line 3, in get_user_balance\n'
        '    return account["balance"] / account["pending"]\n'
        'ZeroDivisionError: division by zero'
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
            retrieved=retrieved, probable_cause="divide by account['pending'] which can be 0",
            root_service="billing", confidence_score=0.9,
            confidence_breakdown=ConfidenceBreakdown(
                self_consistency_agreement=1.0, retrieval_evidence_strength=0.8, chunks_over_threshold=1),
            source_citations=[Citation(claim="c", chunk_id="c1")],
        )

    async def _code_bug_triage(incident, rca, *, client):
        return TriageDecision(
            incident_type=IncidentType.code_bug, confidence=0.95,
            rule_prior=IncidentType.code_bug, rule_prior_strength=0.9, llm_agreed=True, rationale="r",
        )

    async def _ast_fn(incident, rca):
        return await retrieve_code_context(incident, rca, clone_fn=_clone_fn(fixture_repo, tmp_path))

    patch_client = _FakeClient(_draft(_GOOD_PY, "guard divide-by-zero"))

    async def _patch_fn(incident, rca, code, *, client):
        return await generate_patch(
            incident, rca, code, client=patch_client, clone_fn=_clone_fn(fixture_repo, tmp_path),
        )

    incident_ctx = IncidentContext(
        service="billing", alert_name="DivByZero", summary="balance endpoint 500s",
        traceback=_PY_TRACEBACK, repo_url=str(fixture_repo.path), deploy_commit=fixture_repo.v2_commit,
        starts_at=datetime.now(timezone.utc),
    )
    initial = IncidentState(
        incident_id="e2e-patch", status=IncidentStatus.created, raw_payload={},
        alertmanager_fingerprint="fp", incident_context=incident_ctx,
    )

    app = build_graph(
        client=object(), retrieve_fn=_fake_retrieve, synthesize_fn=_ok_synth,
        triage_fn=_code_bug_triage, ast_fn=_ast_fn, patch_fn=_patch_fn,
    )
    final = asyncio.run(app.ainvoke(initial))
    assert final["patch"].syntax_valid and final["patch"].scope_ok
    assert final["remediation_plan"].remediation_class is RemediationClass.patch
    assert final["code_context"].function_name == "get_user_balance"
