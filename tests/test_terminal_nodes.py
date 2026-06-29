"""Task 11 — central escalation + unknown terminal sinks.

Two properties under test:
  1. Branch/behaviour: each sink sets the right status and assembles an evidence summary
     (escalation reads typed errors; unknown lists ranked hypotheses).
  2. The STRUCTURAL safety invariant (F11-1): every terminal RemediationPlan carries
     remediation_class=none and ZERO steps — no command can ride out of escalation/unknown.

The end-to-end graph tests use FAKE nodes and NO checkpointer (in-memory), so they prove the
two confidence gates actually land on these sinks without needing Postgres.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from incidentiq import config
from incidentiq.agents.terminal import synthesize_escalation, synthesize_unknown
from incidentiq.contracts import IncidentType
from incidentiq.errors import TypedError, llm_error
from incidentiq.graph.build import build_graph, make_escalation_node, make_unknown_node
from incidentiq.state import (
    Citation, ConfidenceBreakdown, Hypothesis, IncidentContext, IncidentState,
    IncidentStatus, RCAReport, RemediationClass, RetrievedChunk, RetrievedContext,
    TriageDecision,
)


# --- shared fixtures --------------------------------------------------------

def _retrieved() -> RetrievedContext:
    return RetrievedContext(
        chunks=[RetrievedChunk(chunk_id="c1", source_doc="pm1", text="evidence",
                               semantic_score=0.9, corpus="postmortem")],
        chunks_over_threshold=1, retriever_agreement=1.0,
    )


def _rca(confidence: float, *, hypotheses=None) -> RCAReport:
    return RCAReport.grounded(
        retrieved=_retrieved(),
        probable_cause="bad deploy to checkout", root_service="checkout",
        confidence_score=confidence,
        confidence_breakdown=ConfidenceBreakdown(
            self_consistency_agreement=1.0, retrieval_evidence_strength=0.5,
            chunks_over_threshold=1),
        source_citations=[Citation(claim="latency from deploy", chunk_id="c1")],
        top_hypotheses=hypotheses or [],
    )


def _err(node="rca_synthesizer", kind="llm_timeout", reason="model hung") -> TypedError:
    return TypedError(node=node, kind=kind, reason=reason, ts=datetime.now(timezone.utc))


def _state(*, rca_report=None, errors=None) -> IncidentState:
    ctx = IncidentContext(service="checkout", alert_name="HighLatency",
                          summary="p99 spiking", starts_at=datetime.now(timezone.utc))
    base = IncidentState(
        incident_id="i1", status=IncidentStatus.investigating, raw_payload={},
        alertmanager_fingerprint="fp", incident_context=ctx, errors=errors or [],
    )
    # model_copy mirrors LangGraph's threading: merge a node output without re-validating nested models.
    return base.model_copy(update={"rca_report": rca_report})


# --- escalation builder -----------------------------------------------------

def test_escalation_with_upstream_error_does_not_backfill():
    plan, new_errors = synthesize_escalation(_state(errors=[_err()]))
    assert new_errors == []                       # the failure was already typed upstream
    assert "model hung" in plan.summary
    assert plan.remediation_class is RemediationClass.none
    assert plan.steps == []


def test_escalation_low_confidence_backfills_typed_error():
    plan, new_errors = synthesize_escalation(_state(rca_report=_rca(config.RCA_ESCALATE_BELOW - 0.05)))
    assert len(new_errors) == 1
    assert new_errors[0].kind == "low_confidence"
    assert new_errors[0].node == "escalation_node"
    assert "below escalation gate" in new_errors[0].reason
    assert plan.steps == []


def test_escalation_no_rca_no_error_backfills_no_report():
    plan, new_errors = synthesize_escalation(_state())
    assert new_errors[0].kind == "low_confidence"
    assert "no RCA report" in new_errors[0].reason
    assert plan.references == []
    assert plan.steps == []


# --- unknown builder --------------------------------------------------------

def test_unknown_lists_ranked_hypotheses():
    hyps = [Hypothesis(service="cart", root_cause="cache miss", rank=2),
            Hypothesis(service="checkout", root_cause="deploy regression", rank=1)]
    plan = synthesize_unknown(_state(rca_report=_rca(0.8, hypotheses=hyps)))
    # ordered by rank: checkout (1) appears before cart (2)
    assert plan.summary.index("checkout: deploy regression") < plan.summary.index("cart: cache miss")
    assert plan.remediation_class is RemediationClass.none
    assert plan.steps == []


def test_unknown_without_rca_still_emits_no_command_plan():
    plan = synthesize_unknown(_state())
    assert plan.steps == []
    assert plan.references == []


# --- the structural safety invariant (F11-1) --------------------------------

def test_terminal_plans_never_carry_commands():
    esc_plan, _ = synthesize_escalation(_state(rca_report=_rca(0.2)))
    unk_plan = synthesize_unknown(_state(rca_report=_rca(0.8)))
    for plan in (esc_plan, unk_plan):
        assert plan.remediation_class is RemediationClass.none
        assert plan.steps == []                   # zero executable intents, by construction


# --- node wrappers ----------------------------------------------------------

def test_escalation_node_sets_status_and_appends_error():
    out = asyncio.run(make_escalation_node()(_state(rca_report=_rca(0.1))))
    assert out["status"] is IncidentStatus.escalated
    assert out["remediation_plan"].steps == []
    assert out["errors"][0].kind == "low_confidence"
    assert out["trace"][0].node == "escalation_node"


def test_unknown_node_sets_status_unknown():
    out = asyncio.run(make_unknown_node()(_state(rca_report=_rca(0.8))))
    assert out["status"] is IncidentStatus.unknown
    assert out["remediation_plan"].steps == []
    assert out["trace"][0].node == "unknown_path"


# --- end-to-end graph routing (in-memory, no Postgres) ----------------------

async def _fake_retrieve(query: str) -> RetrievedContext:
    return _retrieved()


def _initial() -> IncidentState:
    ctx = IncidentContext(service="checkout", alert_name="HighLatency",
                          summary="p99 spiking", starts_at=datetime.now(timezone.utc))
    return IncidentState(incident_id="e2e", status=IncidentStatus.created, raw_payload={},
                         alertmanager_fingerprint="fp", incident_context=ctx)


def test_graph_low_rca_routes_to_escalation():
    async def low_synth(incident, retrieved, *, client):
        return _rca(config.RCA_ESCALATE_BELOW - 0.1)      # below Gate A

    app = build_graph(client=None, retrieve_fn=_fake_retrieve, synthesize_fn=low_synth)
    final = asyncio.run(app.ainvoke(_initial()))
    assert final["status"] is IncidentStatus.escalated
    assert final["remediation_plan"].steps == []
    assert final["errors"][0].kind == "low_confidence"


def test_graph_rca_hard_failure_routes_to_escalation():
    async def boom_synth(incident, retrieved, *, client):
        raise llm_error("llm_timeout", "model hung")

    app = build_graph(client=None, retrieve_fn=_fake_retrieve, synthesize_fn=boom_synth)
    final = asyncio.run(app.ainvoke(_initial()))
    assert final["status"] is IncidentStatus.escalated
    assert final["remediation_plan"].steps == []
    kinds = [e.kind for e in final["errors"]]
    assert "llm_timeout" in kinds                          # original error survives
    assert kinds.count("low_confidence") == 0             # not backfilled on top of a real error


def test_graph_low_triage_routes_to_unknown():
    async def ok_synth(incident, retrieved, *, client):
        return _rca(0.9)                                   # clears Gate A

    async def low_triage(incident, rca, *, client):
        return TriageDecision(
            incident_type=IncidentType.infra, confidence=config.TRIAGE_UNKNOWN_BELOW - 0.1,
            rule_prior=IncidentType.infra, rule_prior_strength=0.5,
            llm_agreed=True, rationale="weak",
        )

    app = build_graph(client=None, retrieve_fn=_fake_retrieve,
                      synthesize_fn=ok_synth, triage_fn=low_triage)
    final = asyncio.run(app.ainvoke(_initial()))
    assert final["status"] is IncidentStatus.unknown
    assert final["remediation_plan"].steps == []