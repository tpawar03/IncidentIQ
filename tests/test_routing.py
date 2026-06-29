"""Every branch of the two confidence gates (FR-11). Pure functions, no LLM, no DB.

The point of a deterministic router: each edge is a unit test, and the safety branches
(escalation on low RCA, unknown on low triage) are provable, not hoped for.
"""
from __future__ import annotations

from datetime import datetime, timezone
from incidentiq.agents.triage_router import rule_prior
from incidentiq import config
from incidentiq.contracts import IncidentType
from incidentiq.graph.routing import route_after_rca, route_after_triage
from incidentiq.state import (
    ConfidenceBreakdown, IncidentContext, IncidentState, IncidentStatus,
    Citation, RCAReport, RetrievedChunk, RetrievedContext, TriageDecision,
)
import asyncio
import pytest

from incidentiq.contracts import TriageDraft
from incidentiq.agents.triage_router import (
    triage_incident, build_triage_prompt, _combine_confidence,
)


def _state(*, rca_report=None, triage_decision=None) -> IncidentState:
    ctx = IncidentContext(
        service="cart", alert_name="A", summary="s",
        starts_at=datetime.now(timezone.utc),
    )
    base = IncidentState(
        incident_id="i", status=IncidentStatus.investigating, raw_payload={},
        alertmanager_fingerprint="fp", incident_context=ctx,
    )
    # model_copy(update=...) merges WITHOUT re-validating nested models — the same path
    # LangGraph uses to thread node outputs. Grounding was already proven at construction.
    return base.model_copy(update={"rca_report": rca_report, "triage_decision": triage_decision})


def _rca(confidence: float) -> RCAReport:
    retrieved = RetrievedContext(
        chunks=[RetrievedChunk(chunk_id="c1", source_doc="d", text="t",
                               semantic_score=0.9, corpus="postmortem")],
        chunks_over_threshold=1, retriever_agreement=1.0,
    )
    return RCAReport.grounded(
        retrieved=retrieved,
        probable_cause="p", root_service="cart", confidence_score=confidence,
        confidence_breakdown=ConfidenceBreakdown(
            self_consistency_agreement=1.0, retrieval_evidence_strength=0.5,
            chunks_over_threshold=1),
        source_citations=[Citation(claim="c", chunk_id="c1")],
    )


def _triage(incident_type: IncidentType, confidence: float) -> TriageDecision:
    return TriageDecision(
        incident_type=incident_type, confidence=confidence,
        rule_prior=incident_type, rule_prior_strength=0.8,
        llm_agreed=True, rationale="r",
    )


# --- Gate A: route_after_rca ------------------------------------------------

def test_rca_below_threshold_escalates():
    s = _state(rca_report=_rca(config.RCA_ESCALATE_BELOW - 0.01))
    assert route_after_rca(s) == "escalation_node"


def test_rca_at_threshold_proceeds_to_triage():
    # `< threshold` escalates, so exactly-at-threshold must pass (boundary lives with triage)
    s = _state(rca_report=_rca(config.RCA_ESCALATE_BELOW))
    assert route_after_rca(s) == "triage_router"


def test_rca_missing_report_escalates():
    assert route_after_rca(_state(rca_report=None)) == "escalation_node"


# --- Gate B: route_after_triage ---------------------------------------------

def test_triage_infra_routes_to_runbook():
    s = _state(triage_decision=_triage(IncidentType.infra, 0.90))
    assert route_after_triage(s) == "runbook_executor"


def test_triage_config_routes_to_config_analyzer():
    s = _state(triage_decision=_triage(IncidentType.config, 0.90))
    assert route_after_triage(s) == "config_diff_analyzer"


def test_triage_code_bug_routes_to_ast():
    s = _state(triage_decision=_triage(IncidentType.code_bug, 0.90))
    assert route_after_triage(s) == "ast_code_retriever"


def test_triage_low_confidence_routes_to_unknown():
    # confidence < gate → unknown_path, even though the type model also self-coerces
    s = _state(triage_decision=_triage(IncidentType.infra, config.TRIAGE_UNKNOWN_BELOW - 0.01))
    assert route_after_triage(s) == "unknown_path"


def test_triage_unknown_type_defaults_to_unknown_path():
    # high confidence but type=unknown must NOT fall through to infra (FR-10)
    s = _state(triage_decision=_triage(IncidentType.unknown, 0.95))
    assert route_after_triage(s) == "unknown_path"


def test_triage_missing_decision_routes_to_unknown():
    assert route_after_triage(_state(triage_decision=None)) == "unknown_path"

# --- Rule prior: the deterministic half of hybrid triage --------------------

def _ctx(alert_name="A", summary="s", *, traceback=None, deploy_gap_minutes=None) -> IncidentContext:
    return IncidentContext(
        service="cart", alert_name=alert_name, summary=summary,
        traceback=traceback, deploy_gap_minutes=deploy_gap_minutes,
        starts_at=datetime.now(timezone.utc),
    )


def test_prior_traceback_is_code_bug():
    t, strength = rule_prior(_ctx(traceback="NullPointerException at Cart.java:42"))
    assert t is IncidentType.code_bug
    assert strength >= 0.75


def test_prior_recent_deploy_strengthens_code_bug():
    weak, ws = rule_prior(_ctx(traceback="boom"))                      # no deploy info
    strong, ss = rule_prior(_ctx(traceback="boom", deploy_gap_minutes=5))
    assert weak is strong is IncidentType.code_bug
    assert ss > ws                                                     # fresh deploy → higher prior


def test_prior_flag_keyword_is_config():
    t, strength = rule_prior(_ctx("FeatureFlagError", "rollout of new feature toggle failed"))
    assert t is IncidentType.config
    assert strength >= 0.70


def test_prior_readiness_probe_is_config_not_infra():
    # contains both "probe" (config) and timeout-ish wording — specificity wins
    t, _ = rule_prior(_ctx("failedReadinessProbe", "readiness probe timeout"))
    assert t is IncidentType.config


def test_prior_cpu_is_infra():
    t, strength = rule_prior(_ctx("adServiceHighCpu", "CPU saturation on ad-service"))
    assert t is IncidentType.infra
    assert strength >= 0.70


def test_prior_redis_cache_is_infra():
    t, _ = rule_prior(_ctx("CartCacheFailure", "redis cache connection pool exhausted"))
    assert t is IncidentType.infra


def test_prior_generic_error_without_traceback_is_weak_code_bug():
    t, strength = rule_prior(_ctx("HighErrorRate", "error rate spiking, 500s climbing"))
    assert t is IncidentType.code_bug
    assert strength < 0.75                                            # weaker than a real traceback


def test_prior_no_signal_is_unknown():
    t, strength = rule_prior(_ctx("MysteryAlert", "something is off"))
    assert t is IncidentType.unknown
    assert strength <= 0.30

# --- The hybrid triage node: rule prior + LLM confirm -----------------------

class _FakeClient:
    """Pins the LLM's TriageDraft so we test the COMBINATION, not the model."""
    def __init__(self, draft): self._draft = draft
    async def generate_structured(self, prompt, model): return self._draft


def _draft(incident_type, confidence=0.85, rationale="r") -> TriageDraft:
    return TriageDraft(incident_type=incident_type, confidence=confidence, rationale=rationale)


def test_triage_agreement_yields_confident_decision():
    ctx = _ctx("adServiceHighCpu", "CPU saturation")        # rule prior → infra
    client = _FakeClient(_draft(IncidentType.infra, 0.80))  # LLM concurs
    d = asyncio.run(triage_incident(ctx, _rca(0.8), client=client))
    assert d.llm_agreed is True
    assert d.incident_type is IncidentType.infra
    assert d.confidence >= config.TRIAGE_UNKNOWN_BELOW
    assert d.rule_prior is IncidentType.infra


def test_triage_disagreement_collapses_to_unknown():
    ctx = _ctx("adServiceHighCpu", "CPU saturation")        # rule prior → infra
    client = _FakeClient(_draft(IncidentType.code_bug, 0.90))  # LLM disagrees, confidently
    d = asyncio.run(triage_incident(ctx, _rca(0.8), client=client))
    assert d.llm_agreed is False
    assert d.incident_type is IncidentType.unknown          # coerced below the gate
    assert d.llm_incident_type_raw is IncidentType.code_bug  # model's guess kept as advisory
    assert d.confidence < config.TRIAGE_UNKNOWN_BELOW


def test_combine_agreement_adds_bonus():
    conf, agreed = _combine_confidence(IncidentType.infra, 0.8, IncidentType.infra, 0.8)
    assert agreed is True
    assert conf == pytest.approx(0.8 + config.TRIAGE_AGREE_BONUS)   # 0.5*(0.8+0.8)+bonus


def test_triage_disagreement_is_unknown_even_at_max_confidence():
    # the boundary case: both estimators at 1.0 but conflicting. Must STILL be unknown —
    # the categorical guarantee can't depend on the penalty magnitude (decision #4).
    ctx = _ctx("adServiceHighCpu", "CPU saturation")          # rule prior → infra
    client = _FakeClient(_draft(IncidentType.code_bug, 1.0))  # LLM maximally confident, conflicting
    d = asyncio.run(triage_incident(ctx, _rca(0.8), client=client))
    assert d.incident_type is IncidentType.unknown
    assert d.llm_agreed is False
    assert d.llm_incident_type_raw is IncidentType.code_bug


def test_triage_prompt_anchors_on_prior_and_marks_data():
    prompt = build_triage_prompt(_ctx("adServiceHighCpu", "CPU saturation"),
                                 _rca(0.8), IncidentType.infra, 0.8)
    assert "RULE PRIOR" in prompt and "infra" in prompt      # the LLM sees the anchor
    assert "UNTRUSTED DATA" in prompt                        # injection discipline (decision #11)