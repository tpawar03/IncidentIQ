"""Local cross-encoder reranker: BAAI/bge-reranker-base.

Fusion maximizes recall from two cheap first-stage retrievers. The cross-encoder is the
PRECISION stage: it jointly attends over (query, chunk) in one forward pass, so it judges
relevance far better than either bi-encoder/lexical signal — at a cost we only pay on the
small fused shortlist, never the whole corpus.
"""
from __future__ import annotations

from functools import lru_cache

from sentence_transformers import CrossEncoder

RERANKER_NAME = "BAAI/bge-reranker-base"


@lru_cache(maxsize=1)
def get_reranker() -> CrossEncoder:
    """Singleton load — also a pre-warm entry point (mirrors get_embedder, MF-2)."""
    return CrossEncoder(RERANKER_NAME)


def rerank_scores(query: str, passages: list[str]) -> list[float]:
    """Relevance logit per (query, passage) pair. Higher = more relevant. Order-preserving."""
    if not passages:
        return []
    model = get_reranker()
    scores = model.predict([(query, p) for p in passages])
    return [float(s) for s in scores]