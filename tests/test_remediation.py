"""Task 12 — infra + config remediation paths.

The agents turn a confident diagnosis into a RemediationPlan of CATALOG command intents: the
LLM picks a command_id from a per-path menu, we prove it against the real catalog. Tests cover
the two 'done-when' scenarios, the safety rejections (out-of-menu / non-catalog / bad args →
escalation), the 'no shell strings' structural property, and end-to-end graph routing.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from incidentiq.agents.remediation import (
    _CONFIG_CLASSES, _INFRA_CLASSES, _menu, plan_config_remediation, plan_infra_remediation,
)
from incidentiq.catalog import load_catalog
from incidentiq.contracts import IncidentType, RemediationDraft
from incidentiq.errors import LLMCallError
from incidentiq.graph.build import build_graph, make_remediation_node
from incidentiq.graph.routing import route_after_remediation
from incidentiq.state import (
    Citation, CommandIntent, ConfidenceBreakdown, IncidentContext, IncidentState,
    IncidentStatus, RCAReport, RemediationClass, RemediationPlan, RetrievedChunk,
    RetrievedContext, TriageDecision,
)


class _FakeClient:
    """Pins the LLM's RemediationDraft so we test the COMBINATION, not the model."""
    def __init__(self, draft): self._draft = draft
    async def generate_structured(self, prompt, schema_model): return self._draft


def _retrieved() -> RetrievedContext:
    return RetrievedContext(
        chunks=[RetrievedChunk(chunk_id="c1", source_doc="pm1", text="t",
                               semantic_score=0.9, corpus="postmortem")],
        chunks_over_threshold=1, retriever_agreement=1.0,
    )


def _rca(confidence=0.9) -> RCAReport:
    return RCAReport.grounded(
        retrieved=_retrieved(), probable_cause="bad deploy", root_service="adservice",
        confidence_score=confidence,
        confidence_breakdown=ConfidenceBreakdown(
            self_consistency_agreement=1.0, retrieval_evidence_strength=0.8, chunks_over_threshold=1),
        source_citations=[Citation(claim="x", chunk_id="c1")],
    )


def _ctx(alert="adServiceHighCpu", summary="CPU saturation on ad-service") -> IncidentContext:
    return IncidentContext(service="adservice", alert_name=alert, summary=summary,
                           namespace="otel-demo", starts_at=datetime.now(timezone.utc))


def _draft(command_id, args=None, summary="do it") -> RemediationDraft:
    return RemediationDraft(command_id=command_id, args=args or {}, summary=summary)


# --- per-path menus ---------------------------------------------------------

def test_menus_partition_the_catalog_by_class():
    cat = load_catalog()
    infra, config = _menu(cat, _INFRA_CLASSES), _menu(cat, _CONFIG_CLASSES)
    assert "kubectl_rollout_restart" in infra
    assert set(config) == {"flag_rollback", "config_revert"}
    assert "flag_rollback" not in infra            # flag actions are NOT an infra option


# --- the two 'done-when' scenarios -----------------------------------------

def test_infra_cpu_scenario_yields_runbook_plan():
    client = _FakeClient(_draft("kubectl_rollout_restart",
                                {"deployment": "adservice", "namespace": "otel-demo"}))
    plan = asyncio.run(plan_infra_remediation(_ctx(), _rca(), client=client))
    assert plan.remediation_class is RemediationClass.kubectl
    assert [s.command_id for s in plan.steps] == ["kubectl_rollout_restart"]
    assert plan.references[0].chunk_id == "c1"     # carries the RCA evidence


def test_flag_scenario_yields_flag_rollback_intent():
    client = _FakeClient(_draft("flag_rollback", {"flag_key": "adServiceFailure"}))
    plan = asyncio.run(plan_config_remediation(
        _ctx("FeatureFlagError", "feature toggle rollout failed"), _rca(), client=client))
    assert plan.remediation_class is RemediationClass.flag_rollback
    assert plan.steps[0].command_id == "flag_rollback"
    assert plan.steps[0].args["flag_key"] == "adServiceFailure"


# --- safety: no command escapes the menu / catalog / arg schema -------------

def test_out_of_menu_pick_escalates():
    client = _FakeClient(_draft("kubectl_rollout_restart",
                                {"deployment": "adservice", "namespace": "otel-demo"}))
    with pytest.raises(LLMCallError, match="outside the config_diff_analyzer menu"):
        asyncio.run(plan_config_remediation(_ctx(), _rca(), client=client))


def test_non_catalog_command_escalates():
    client = _FakeClient(_draft("delete_everything", {}))
    with pytest.raises(LLMCallError):
        asyncio.run(plan_infra_remediation(_ctx(), _rca(), client=client))


def test_injected_args_escalate():
    client = _FakeClient(_draft("flag_rollback", {"flag_key": "x; rm -rf / #"}))
    with pytest.raises(LLMCallError, match="invalid config_diff_analyzer command"):
        asyncio.run(plan_config_remediation(_ctx(), _rca(), client=client))


def test_plan_steps_are_typed_intents_never_shell():
    client = _FakeClient(_draft("flag_rollback", {"flag_key": "ok"}))
    plan = asyncio.run(plan_config_remediation(_ctx(), _rca(), client=client))
    cat = load_catalog()
    for step in plan.steps:
        assert isinstance(step, CommandIntent)     # structured intent, not a string
        assert isinstance(step.args, dict)         # typed args; no rendered shell anywhere
        assert step.command_id in cat


# --- node wrapper + post-remediation routing --------------------------------

def _state(*, rca_report=None, remediation_plan=None) -> IncidentState:
    base = IncidentState(incident_id="i", status=IncidentStatus.investigating, raw_payload={},
                         alertmanager_fingerprint="fp", incident_context=_ctx())
    return base.model_copy(update={"rca_report": rca_report, "remediation_plan": remediation_plan})


def test_remediation_node_success_sets_plan():
    client = _FakeClient(_draft("kubectl_rollout_restart",
                                {"deployment": "adservice", "namespace": "otel-demo"}))
    node = make_remediation_node("runbook_executor", client, plan_infra_remediation)
    out = asyncio.run(node(_state(rca_report=_rca())))
    assert out["remediation_plan"].steps[0].command_id == "kubectl_rollout_restart"
    assert "errors" not in out
    assert out["trace"][0].node == "runbook_executor"


def test_remediation_node_failure_escalates():
    client = _FakeClient(_draft("delete_everything", {}))
    node = make_remediation_node("runbook_executor", client, plan_infra_remediation)
    out = asyncio.run(node(_state(rca_report=_rca())))
    assert out["status"] is IncidentStatus.escalated
    assert out["errors"][0].node == "runbook_executor"
    assert "remediation_plan" not in out


def test_route_after_remediation_plan_ready_to_checkpoint():
    cat = load_catalog()
    plan = RemediationPlan(
        remediation_class=RemediationClass.flag_rollback, summary="s",
        steps=[CommandIntent.from_catalog(catalog=cat, command_id="flag_rollback",
                                          args={"flag_key": "ok"})])
    assert route_after_remediation(_state(remediation_plan=plan)) == "human_checkpoint"


def test_route_after_remediation_no_plan_escalates():
    assert route_after_remediation(_state(remediation_plan=None)) == "escalation_node"


# --- end-to-end graph routing (in-memory, no Postgres) ----------------------

async def _fake_retrieve(query): return _retrieved()
async def _ok_synth(incident, retrieved, *, client): return _rca(0.9)


def _confident_triage(incident_type):
    async def _triage(incident, rca, *, client):
        return TriageDecision(incident_type=incident_type, confidence=0.95,
                              rule_prior=incident_type, rule_prior_strength=0.9,
                              llm_agreed=True, rationale="r")
    return _triage


def _initial() -> IncidentState:
    return IncidentState(incident_id="e2e", status=IncidentStatus.created, raw_payload={},
                         alertmanager_fingerprint="fp", incident_context=_ctx())


def test_graph_infra_path_produces_kubectl_plan():
    client = _FakeClient(_draft("kubectl_rollout_restart",
                                {"deployment": "adservice", "namespace": "otel-demo"}))
    app = build_graph(client=client, retrieve_fn=_fake_retrieve, synthesize_fn=_ok_synth,
                      triage_fn=_confident_triage(IncidentType.infra))
    final = asyncio.run(app.ainvoke(_initial()))
    assert final["remediation_plan"].steps[0].command_id == "kubectl_rollout_restart"


def test_graph_config_path_produces_flag_rollback_plan():
    client = _FakeClient(_draft("flag_rollback", {"flag_key": "adServiceFailure"}))
    app = build_graph(client=client, retrieve_fn=_fake_retrieve, synthesize_fn=_ok_synth,
                      triage_fn=_confident_triage(IncidentType.config))
    final = asyncio.run(app.ainvoke(_initial()))
    assert final["remediation_plan"].steps[0].command_id == "flag_rollback"


def test_graph_remediation_failure_routes_to_escalation_sink():
    client = _FakeClient(_draft("delete_everything", {}))          # invalid pick → typed error
    app = build_graph(client=client, retrieve_fn=_fake_retrieve, synthesize_fn=_ok_synth,
                      triage_fn=_confident_triage(IncidentType.infra))
    final = asyncio.run(app.ainvoke(_initial()))
    assert final["status"] is IncidentStatus.escalated
    assert final["remediation_plan"].steps == []                  # central sink emits no commands
    assert any(e.node == "runbook_executor" for e in final["errors"])