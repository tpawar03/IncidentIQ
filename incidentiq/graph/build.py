"""Assemble the flat LangGraph StateGraph: nodes + edges + reducers.

TASK (Orchestration #1) — to build here, per docs/AGENT_ORCHESTRATION.md §1:
  - StateGraph over IncidentState (state.py), additive reducers for errors/trace.
  - Nodes: alert_enricher → hybrid_retriever → rca_synthesizer → triage_router
    → remediation paths → HITL interrupt → execution → post_mortem.
  - Conditional edges via routing.py (confidence gates).
  - Compiled with the Postgres checkpointer (checkpointer.py) for mid-incident resume <10s (FR-33).

Walking-skeleton status: parallel_retriever -> rca_synthesizer -> END wired (linear).
Confidence-gate conditional edges + downstream nodes land in later tasks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Awaitable, Callable

from langgraph.graph import StateGraph, START, END

from incidentiq.agents.rca_synthesizer import synthesize_rca
from incidentiq.errors import LLMCallError
from incidentiq.llm.ollama_client import OllamaClient
from incidentiq.retrieval.retriever import retrieve
from incidentiq.state import (
    AgentSpan,
    IncidentContext,
    IncidentState,
    IncidentStatus,
    RCAReport,
    RemediationPlan,
    RetrievedContext,
)
from incidentiq.agents.terminal import synthesize_escalation, synthesize_unknown
from incidentiq.agents.triage_router import triage_incident
from incidentiq.graph.routing import route_after_rca, route_after_remediation, route_after_triage
from incidentiq.state import TriageDecision
from incidentiq.agents.remediation import plan_config_remediation, plan_infra_remediation

# Injectable dependency signatures — real impls in retrieval/ + agents/, fakes in tests.
RetrieveFn = Callable[[str], Awaitable[RetrievedContext]]
SynthesizeFn = Callable[..., Awaitable[RCAReport]]
TriageFn = Callable[..., Awaitable[TriageDecision]]
RemediationFn = Callable[..., Awaitable[RemediationPlan]]


def _retrieval_query(ctx: IncidentContext) -> str:
    """alert_name + summary always present; traceback sharpens code-bug retrieval."""
    parts = [ctx.alert_name, ctx.summary, ctx.traceback]
    return "  ".join(p for p in parts if p)


def _span(node: str, start: datetime) -> AgentSpan:
    # redaction_applied=False: no prompt redaction implemented yet (FR-37 is a later task).
    return AgentSpan(
        node=node, start_time=start, end_time=datetime.now(timezone.utc),
        redaction_applied=False,
    )


def make_retriever_node(retrieve_fn: RetrieveFn = retrieve):
    """First graph node: trusted IncidentContext -> RetrievedContext (Task 7 logic)."""
    async def retriever_node(state: IncidentState) -> dict:
        start = datetime.now(timezone.utc)
        retrieved = await retrieve_fn(_retrieval_query(state.incident_context))
        return {
            "status": IncidentStatus.investigating,
            "retrieved_context": retrieved,
            "trace": [_span("parallel_retriever", start)],
        }
    return retriever_node


def make_rca_node(client: OllamaClient, synthesize_fn: SynthesizeFn = synthesize_rca):
    """RCA node: N=3 self-consistency RCA (Task 8). Injected OllamaClient."""
    async def rca_node(state: IncidentState) -> dict:
        start = datetime.now(timezone.utc)
        try:
            report = await synthesize_fn(
                state.incident_context, state.retrieved_context, client=client,
            )
        except LLMCallError as e:
            # decision #10: record the typed failure; ROUTING to escalation is Task 10's job.
            return {
                "status": IncidentStatus.escalated,
                "errors": [e.typed_error],
                "trace": [_span("rca_synthesizer", start)],
            }
        return {"rca_report": report, "trace": [_span("rca_synthesizer", start)]}
    return rca_node

def make_triage_node(client: OllamaClient, triage_fn: TriageFn = triage_incident):
    """Hybrid triage node (Task 10): rule prior + LLM confirm → TriageDecision."""
    async def triage_node(state: IncidentState) -> dict:
        start = datetime.now(timezone.utc)
        try:
            decision = await triage_fn(
                state.incident_context, state.rca_report, client=client,
            )
        except LLMCallError as e:
            # SF-4: a hung/failed triage generation escalates rather than crashing the graph.
            return {
                "status": IncidentStatus.escalated,
                "errors": [e.typed_error],
                "trace": [_span("triage_router", start)],
            }
        return {"triage_decision": decision, "trace": [_span("triage_router", start)]}
    return triage_node

def make_remediation_node(node_name: str, client: OllamaClient, plan_fn: RemediationFn):
    """Infra/config path node (Task 12): diagnosis → RemediationPlan of catalog intents.

    A path agent that can't produce a catalog action raises LLMCallError → recorded as a typed
    error + status=escalated; route_after_remediation then sends it to the central escalation sink.
    """
    async def remediation_node(state: IncidentState) -> dict:
        start = datetime.now(timezone.utc)
        try:
            plan = await plan_fn(state.incident_context, state.rca_report, client=client)
        except LLMCallError as e:
            return {
                "status": IncidentStatus.escalated,
                "errors": [e.typed_error],
                "trace": [_span(node_name, start)],
            }
        return {"remediation_plan": plan, "trace": [_span(node_name, start)]}
    return remediation_node

def make_escalation_node():
    """Central escalation sink (Task 11): typed errors → evidence summary, no commands."""
    async def escalation_node(state: IncidentState) -> dict:
        start = datetime.now(timezone.utc)
        plan, new_errors = synthesize_escalation(state)
        return {
            "status": IncidentStatus.escalated,
            "remediation_plan": plan,
            "errors": new_errors,                 # add-reducer appends the backfilled reason (F11-3)
            "trace": [_span("escalation_node", start)],
        }
    return escalation_node


def make_unknown_node():
    """Unknown terminal path (Task 11): evidence + ranked hypotheses, no commands (FR-10)."""
    async def unknown_node(state: IncidentState) -> dict:
        start = datetime.now(timezone.utc)
        return {
            "status": IncidentStatus.unknown,
            "remediation_plan": synthesize_unknown(state),
            "trace": [_span("unknown_path", start)],
        }
    return unknown_node


def build_graph(
    *,
    client: OllamaClient,
    retrieve_fn: RetrieveFn = retrieve,
    synthesize_fn: SynthesizeFn = synthesize_rca,
    triage_fn: TriageFn = triage_incident,
    infra_fn: RemediationFn = plan_infra_remediation,
    config_fn: RemediationFn = plan_config_remediation,
    checkpointer=None,
):
    """Flat StateGraph: parallel_retriever → rca_synthesizer →(Gate A)→ triage_router →(Gate B).

    The two confidence gates (routing.py) are wired as conditional edges. Downstream
    targets (escalation_node, unknown_path, remediation entries) are mapped to END here
    and replaced with real nodes in Tasks 11/12. Deps are injected for test fakes.
    Node ids match AGENT_ORCHESTRATION.md §6 exactly.
    """
    graph = StateGraph(IncidentState)
    graph.add_node("parallel_retriever", make_retriever_node(retrieve_fn))
    graph.add_node("rca_synthesizer", make_rca_node(client, synthesize_fn))
    graph.add_node("triage_router", make_triage_node(client, triage_fn))
    graph.add_node("runbook_executor", make_remediation_node("runbook_executor", client, infra_fn))
    graph.add_node("config_diff_analyzer",make_remediation_node("config_diff_analyzer", client, config_fn))
    graph.add_node("escalation_node", make_escalation_node())
    graph.add_node("unknown_path", make_unknown_node())

    graph.add_edge(START, "parallel_retriever")
    graph.add_edge("parallel_retriever", "rca_synthesizer")

    # Gate A: composite RCA confidence < threshold → escalation, else triage.
    graph.add_conditional_edges("rca_synthesizer", route_after_rca, {
        "escalation_node": "escalation_node",   # Task 11: real sink (was END)
        "triage_router": "triage_router",
    })
    # Gate B: incident_type → remediation entry; low confidence → unknown (no commands).
    graph.add_conditional_edges("triage_router", route_after_triage, {
        "runbook_executor": "runbook_executor",            # Task 12: infra path (was END)
        "config_diff_analyzer": "config_diff_analyzer",    # Task 12: config path (was END)
        "ast_code_retriever": END,     # PLACEHOLDER → AST/code task
        "unknown_path": "unknown_path",
    })
    # After a path agent: a usable plan → HITL checkpoint (later task → END); else the central
    # escalation sink (Task 11) — proving one sink absorbs remediation failures too.
    for path_node in ("runbook_executor", "config_diff_analyzer"):
        graph.add_conditional_edges(path_node, route_after_remediation, {
            "human_checkpoint": END,      # PLACEHOLDER → HITL task
            "escalation_node": "escalation_node",
        })
    # Both terminal sinks flow to post-mortem; that node lands in a later task → END for now.
    graph.add_edge("escalation_node", END)
    graph.add_edge("unknown_path", END)

    return graph.compile(checkpointer=checkpointer)