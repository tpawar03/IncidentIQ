"""Task 7 hybrid-retriever tests: fusion, agreement, and rerank selection.

DB-free and model-free on purpose (mirrors test_corpus) — the fusion/signal logic is
pure, and the cross-encoder is monkeypatched. The live retrieve() path is verified by
running it against the seeded corpus.
"""
import incidentiq.retrieval.retriever as r
from incidentiq.retrieval.retriever import _Candidate, _agreement, _rerank, _rrf_fuse


def _cand(chunk_id, score=0.5, corpus="runbook"):
    return _Candidate(chunk_id, f"{chunk_id}.md", None, f"text of {chunk_id}", corpus, score)


def test_rrf_rewards_cross_modal_agreement():
    # B sits at rank 2 in BOTH lists; A is rank 1 in one list only.
    semantic = [_cand("A"), _cand("B"), _cand("C")]
    lexical = [_cand("D"), _cand("B"), _cand("E")]
    fused = _rrf_fuse(semantic, lexical)
    assert fused[0].chunk_id == "B"                       # agreement beats single-list dominance
    assert {f.chunk_id for f in fused} == {"A", "B", "C", "D", "E"}   # union, deduped


def test_rrf_carries_per_arm_scores_and_none():
    fused = {f.chunk_id: f for f in _rrf_fuse([_cand("A", 0.8)], [_cand("D", 0.07)])}
    assert fused["A"].semantic_score == 0.8 and fused["A"].bm25_score is None   # semantic-only
    assert fused["D"].bm25_score == 0.07 and fused["D"].semantic_score is None  # lexical-only


def test_agreement_is_jaccard():
    semantic = [_cand("A"), _cand("B"), _cand("C")]
    lexical = [_cand("D"), _cand("B"), _cand("E")]
    assert _agreement(semantic, lexical) == 1 / 5         # {B} / {A,B,C,D,E}
    assert _agreement([], []) == 0.0                       # no divide-by-zero


def test_rerank_orders_by_score_and_trims(monkeypatch):
    fused = _rrf_fuse([_cand("A"), _cand("B"), _cand("C")], [])
    by_id = {f.chunk_id: f for f in fused}
    monkeypatch.setattr(
        r, "rerank_scores",
        lambda query, passages: [{"text of A": 0.1, "text of B": 0.9, "text of C": 0.5}[p] for p in passages],
    )
    top = _rerank("q", fused, top_n=2)
    assert [f.chunk_id for f in top] == ["B", "C"]         # by rerank score desc, trimmed to 2
    assert top[0].rerank_score == 0.9                      # score attached to the chunk