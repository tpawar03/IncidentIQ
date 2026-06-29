"""FR-33 durability: kill a graph run mid-incident, prove a fresh checkpointer resumes
from the last completed node — a resume, not a restart.

Uses FAKE nodes (no Ollama, no retriever DB) but the REAL Postgres checkpointer, so the
test exercises the actual durability mechanism in milliseconds. Skips cleanly when Postgres
isn't up (same convention as the corpus/ingestion tests).

The proof is the retriever call count: it runs ONCE (run 1), gets checkpointed, then on
resume (run 2, fresh app + fresh saver + new event loop = a simulated process restart) it
is NOT called again, yet the RCA node completes — so state was rehydrated from Postgres.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from incidentiq.graph.build import build_graph
from incidentiq.graph.checkpointer import postgres_checkpointer
from incidentiq.state import (
    Citation,
    ConfidenceBreakdown,
    IncidentContext,
    IncidentState,
    IncidentStatus,
    RCAReport,
    RetrievedChunk,
    RetrievedContext,
)


@pytest.fixture
def require_postgres():
    from incidentiq.db import connect

    try:
        with connect() as c, c.cursor() as cur:
            cur.execute("select 1")
    except Exception:
        pytest.skip("Postgres not available (docker compose up)")


def _initial_state() -> IncidentState:
    ctx = IncidentContext(
        service="checkout",
        alert_name="HighLatency",
        summary="p99 latency spiking after deploy",
        starts_at=datetime.now(timezone.utc),
    )
    return IncidentState(
        incident_id="durability",
        status=IncidentStatus.created,
        raw_payload={},
        alertmanager_fingerprint="fp",
        incident_context=ctx,
    )


class _CountingRetriever:
    """Fake retriever recording its call count — the evidence that resume != restart."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, query: str) -> RetrievedContext:
        self.calls += 1
        chunk = RetrievedChunk(
            chunk_id="c1", source_doc="pm1", text="evidence", semantic_score=0.9,
            corpus="postmortem",
        )
        return RetrievedContext(chunks=[chunk], chunks_over_threshold=1, retriever_agreement=1.0)


async def _crashing_synth(incident, retrieved, *, client):
    raise RuntimeError("simulated process kill mid-RCA")


async def _ok_synth(incident, retrieved, *, client):
    return RCAReport.grounded(
        retrieved=retrieved,
        probable_cause="bad deploy",
        root_service="checkout",
        confidence_score=0.8,
        confidence_breakdown=ConfidenceBreakdown(
            self_consistency_agreement=1.0,
            retrieval_evidence_strength=0.8,
            chunks_over_threshold=1,
        ),
        source_citations=[Citation(claim="latency from deploy", chunk_id="c1")],
    )

async def _ok_triage(incident, rca, *, client):
    from incidentiq.contracts import IncidentType
    from incidentiq.state import TriageDecision
    return TriageDecision(
        incident_type=IncidentType.infra, confidence=0.9,
        rule_prior=IncidentType.infra, rule_prior_strength=0.8,
        llm_agreed=True, rationale="fake",
    )


def test_killed_graph_resumes_from_last_node(require_postgres):
    thread_id = f"durability-test-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}
    retriever = _CountingRetriever()

    # Run 1: retriever succeeds + checkpoints; RCA "crashes" → simulated kill.
    async def run1():
        async with postgres_checkpointer() as cp:
            app = build_graph(
                client=None, retrieve_fn=retriever, synthesize_fn=_crashing_synth, checkpointer=cp,
            )
            with pytest.raises(RuntimeError, match="simulated process kill"):
                await app.ainvoke(_initial_state(), config)

    asyncio.run(run1())
    assert retriever.calls == 1  # retriever ran and was checkpointed before the crash

    # Run 2: fresh app + fresh saver + new event loop (simulated restart), same thread_id.
    async def run2():
        async with postgres_checkpointer() as cp:
            app = build_graph(
                client=None, retrieve_fn=retriever, synthesize_fn=_ok_synth,
                triage_fn=_ok_triage, checkpointer=cp,
            )
            return await app.ainvoke(None, config)  # None input → resume from checkpoint

    final = asyncio.run(run2())

    # Resumed, not restarted: retriever did NOT run again, yet RCA completed from the checkpoint.
    assert retriever.calls == 1
    assert final["retrieved_context"] is not None
    assert final["rca_report"] is not None
    assert final["rca_report"].root_service == "checkout"
