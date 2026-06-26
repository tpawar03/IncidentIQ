"""Task 8 — RCA synthesizer. Model-free: fake clients stand in for Ollama."""

import asyncio
from datetime import datetime

import pytest

from incidentiq import config
from incidentiq.agents.rca_synthesizer import (
    _build_confidence,
    _vote,
    build_rca_prompt,
    synthesize_rca,
)
from incidentiq.contracts import Citation, Hypothesis, RCADraft
from incidentiq.errors import LLMCallError, llm_error
from incidentiq.state import IncidentContext, RetrievedChunk, RetrievedContext


# --- fixtures-as-helpers ----------------------------------------------------

def _incident(**over) -> IncidentContext:
    base = dict(service="payment", alert_name="HighErrorRate", summary="5xx spike",
                starts_at=datetime(2026, 1, 1))
    base.update(over)
    return IncidentContext(**base)


def _chunks(sim=0.8, n=5):
    return [RetrievedChunk(chunk_id=f"c{i}", source_doc="pm.md", text="OOMKilled restart loop",
                           semantic_score=sim, corpus="postmortem") for i in range(n)]


def _ctx(sim=0.8, n=5, over=4, agree=0.5):
    return RetrievedContext(chunks=_chunks(sim, n), chunks_over_threshold=over,
                            retriever_agreement=agree)


def _draft(svc, ncit=1, cause="x", raw=None, nhyp=0):
    return RCADraft(
        probable_cause=cause, root_service=svc, llm_confidence_raw=raw,
        source_citations=[Citation(claim=f"c{i}", chunk_id=f"c{i}") for i in range(ncit)],
        top_hypotheses=[Hypothesis(service=svc, root_cause="rc", rank=i + 1) for i in range(nhyp)],
    )


class _Client:
    """Returns a fixed list of drafts, one per call (cycles if exhausted)."""
    def __init__(self, drafts): self._drafts, self._i = drafts, 0
    async def generate_structured(self, prompt, model):
        d = self._drafts[self._i % len(self._drafts)]; self._i += 1
        if isinstance(d, LLMCallError):
            raise d
        return d


# --- token budget (Step 2) --------------------------------------------------

def test_budget_drops_low_ranked_chunks_but_keeps_at_least_one():
    rc = RetrievedContext(chunks=_chunks(0.8, n=5), chunks_over_threshold=5, retriever_agreement=0.5)
    rc.chunks[0] = rc.chunks[0].model_copy(update={"text": "word " * 300})
    _, presented, truncated = build_rca_prompt(_incident(), rc, budget=350)
    assert len(presented) == 1 and truncated is True          # F-42 floor


def test_full_corpus_fits_under_generous_budget():
    _, presented, truncated = build_rca_prompt(_incident(), _ctx(), budget=6000)
    assert len(presented) == 5 and truncated is False


def test_prompt_wraps_evidence_in_data_envelope():
    prompt, _, _ = build_rca_prompt(_incident(), _ctx())
    assert "UNTRUSTED DATA" in prompt and "<<<BEGIN EVIDENCE>>>" in prompt
    assert "chunk_id: c0" in prompt                            # cite-by-id label present


# --- vote (Step 4) ----------------------------------------------------------

def test_vote_majority_picks_most_cited_winning_draft():
    canonical, agreement = _vote([_draft("payment"), _draft("payment", ncit=3, cause="best"),
                                  _draft("cart")])
    assert canonical.root_service == "payment"
    assert canonical.probable_cause == "best"                 # richest winning draft
    assert round(agreement, 2) == 0.67


def test_vote_full_disagreement_floors_agreement():
    _, agreement = _vote([_draft("a"), _draft("b"), _draft("c")])
    assert round(agreement, 2) == 0.33                        # 1/3


# --- composite confidence (Step 5) ------------------------------------------

def test_strong_evidence_high_confidence_no_penalties():
    score, b = _build_confidence(presented=_chunks(0.85), retrieved=_ctx(0.85, over=5, agree=0.6),
                                 agreement=1.0, alerts_truncated=False)
    assert score > 0.8 and b.penalties_applied == []


def test_weak_and_truncated_apply_both_penalties_and_clamp():
    score, b = _build_confidence(presented=_chunks(0.3, n=1), retrieved=_ctx(0.3, n=1, over=1, agree=0.1),
                                 agreement=0.33, alerts_truncated=True)
    assert 0.0 <= score < 0.3
    assert "weak_retrieval -0.15" in b.penalties_applied
    assert "truncated -0.10" in b.penalties_applied


def test_confidence_ignores_llm_raw_number():
    """Headline: a model screaming 0.99 with weak signals still scores low."""
    rc = _ctx(0.3, n=1, over=1, agree=0.0)
    client = _Client([_draft("a", raw=0.99), _draft("b", raw=0.99), _draft("c", raw=0.99)])
    report = asyncio.run(synthesize_rca(_incident(), rc, client=client))
    assert report.llm_confidence_raw == 0.99                  # carried
    assert report.confidence_score < 0.5                      # but NOT trusted


# --- end-to-end (Step 6) ----------------------------------------------------

def test_synthesize_happy_path_returns_grounded_report():
    client = _Client([_draft("payment", ncit=2, cause="OOMKilled", raw=0.7)] * 3)
    report = asyncio.run(synthesize_rca(_incident(), _ctx(), client=client))
    assert report.root_service == "payment"
    assert report.self_consistency_agreement == 1.0
    assert {c.chunk_id for c in report.source_citations} <= {f"c{i}" for i in range(5)}


def test_one_sample_failure_is_tolerated():
    client = _Client([llm_error("llm_timeout", "stalled"), _draft("payment"), _draft("payment")])
    report = asyncio.run(synthesize_rca(_incident(), _ctx(), client=client))
    assert report.root_service == "payment"                   # survived on 2/3


def test_all_samples_fail_escalates():
    client = _Client([llm_error("invalid_json", "bad")] * 3)
    with pytest.raises(LLMCallError) as ei:
        asyncio.run(synthesize_rca(_incident(), _ctx(), client=client))
    assert ei.value.typed_error.node == "rca_synthesizer"


def test_ungrounded_citation_escalates():
    bad = RCADraft(probable_cause="x", root_service="payment",
                   source_citations=[Citation(claim="made up", chunk_id="NOPE")])
    with pytest.raises(LLMCallError) as ei:
        asyncio.run(synthesize_rca(_incident(), _ctx(), client=_Client([bad] * 3)))
    assert "not grounded" in ei.value.typed_error.reason