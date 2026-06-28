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
    RetrievedContext,
)

# Injectable dependency signatures — real impls in retrieval/ + agents/, fakes in tests.
RetrieveFn = Callable[[str], Awaitable[RetrievedContext]]
SynthesizeFn = Callable[..., Awaitable[RCAReport]]


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


def build_graph(
    *,
    client: OllamaClient,
    retrieve_fn: RetrieveFn = retrieve,
    synthesize_fn: SynthesizeFn = synthesize_rca,
    checkpointer=None,
):
    """Walking-skeleton StateGraph: parallel_retriever -> rca_synthesizer -> END.

    Linear for now — the confidence-gate conditional edges (escalation/triage) land in
    Task 10. Deps are injected so the durability test can substitute fakes (no DB/Ollama).
    Node ids match AGENT_ORCHESTRATION.md §6 exactly.
    """
    graph = StateGraph(IncidentState)
    graph.add_node("parallel_retriever", make_retriever_node(retrieve_fn))
    graph.add_node("rca_synthesizer", make_rca_node(client, synthesize_fn))

    graph.add_edge(START, "parallel_retriever")
    graph.add_edge("parallel_retriever", "rca_synthesizer")
    graph.add_edge("rca_synthesizer", END)

    return graph.compile(checkpointer=checkpointer)