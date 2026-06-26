"""Hybrid retriever: concurrent semantic + lexical search over the chunks table,
fused by RRF and reranked by a local cross-encoder (decision #5).

Both first-stage retrievers hit the SAME Postgres table — the semantic side via the
HNSW vector index, the lexical side via the GIN tsvector. One store, no sync drift.
"""

from __future__ import annotations

import numpy as np
import asyncio

from incidentiq.state import RetrievedChunk, RetrievedContext

from incidentiq.retrieval.reranking import rerank_scores
from dataclasses import dataclass

from pgvector.psycopg import register_vector

from incidentiq.db import connect
from incidentiq.retrieval.embedding import embed_query


@dataclass
class _Candidate:
    """A chunk returned by ONE retriever, with that retriever's raw score and rank."""
    chunk_id: str
    source_doc: str
    parent_section: str | None
    text: str
    corpus: str
    score: float

@dataclass
class _Fused:
    """A chunk after fusion: carries BOTH raw scores (None where a retriever missed it)
    plus the combined RRF score we sort on."""
    chunk_id: str
    source_doc: str
    parent_section: str | None
    text: str
    corpus: str
    semantic_score: float | None
    bm25_score: float | None
    rrf_score: float
    rerank_score: float | None = None


def _rrf_fuse(
    semantic: list[_Candidate], lexical: list[_Candidate], k_rrf: int = 60
) -> list[_Fused]:
    """Reciprocal Rank Fusion: combine two ranked lists by rank position, not score.

    Each list contributes 1/(k_rrf + rank) per chunk; scores from the two arms are
    summed. Rank-based, so the incompatible score scales never need normalizing.
    """
    fused: dict[str, _Fused] = {}

    def _slot(c: _Candidate) -> _Fused:
        return fused.setdefault(
            c.chunk_id,
            _Fused(c.chunk_id, c.source_doc, c.parent_section, c.text, c.corpus,
                   None, None, 0.0),
        )

    for rank, c in enumerate(semantic, start=1):
        f = _slot(c)
        f.semantic_score = c.score
        f.rrf_score += 1.0 / (k_rrf + rank)

    for rank, c in enumerate(lexical, start=1):
        f = _slot(c)
        f.bm25_score = c.score
        f.rrf_score += 1.0 / (k_rrf + rank)

    return sorted(fused.values(), key=lambda f: f.rrf_score, reverse=True)

def _rerank(query: str, fused: list[_Fused], top_n: int = 5) -> list[_Fused]:
    """Cross-encoder precision pass over the fused shortlist → top_n by true relevance."""
    scores = rerank_scores(query, [f.text for f in fused])
    for f, s in zip(fused, scores):
        f.rerank_score = s
    return sorted(fused, key=lambda f: f.rerank_score, reverse=True)[:top_n]

def _semantic_search(qvec: np.ndarray, k: int, corpus: str | None = None) -> list[_Candidate]:
    """Dense retrieval: precomputed bge query vector vs. the HNSW index, cosine similarity."""
    with connect() as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chunk_id, source_doc, parent_section, text, corpus,
                       1 - (embedding <=> %s) AS score
                FROM chunks
                WHERE (%s::text IS NULL OR corpus = %s)
                ORDER BY embedding <=> %s
                LIMIT %s
                """,
                (qvec, corpus, corpus, qvec, k),
            )
            return [_Candidate(*r) for r in cur.fetchall()]


def _lexical_search(query: str, k: int, corpus: str | None = None) -> list[_Candidate]:
    """Sparse/lexical retrieval: Postgres full-text rank over the GIN tsvector.

    plainto_tsquery safely parses raw text but AND-combines lexemes — too strict for
    natural-language incident text. We rewrite its '&' connectors to '|' so the arm
    matches ANY term and ranks by how well (recall-oriented first stage).
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH q AS (
                SELECT replace(plainto_tsquery('english', %s)::text, '&', '|')::tsquery AS tsq
            )
            SELECT chunk_id, source_doc, parent_section, text, corpus,
                   ts_rank(tsv, q.tsq) AS score
            FROM chunks, q
            WHERE tsv @@ q.tsq
              AND (%s::text IS NULL OR corpus = %s)
            ORDER BY score DESC
            LIMIT %s
            """,
            (query, corpus, corpus, k),
        )
        rows = cur.fetchall()
    return [_Candidate(*r) for r in rows]

# rerank_score gate. PLACEHOLDER — the calibration task (MF-1) owns the final value;
# relevant chunks saturate ~0.97 and junk drops off sharply, so ~0.5 separates (F-39).
RERANK_THRESHOLD = 0.5


def _backfill_semantic(qvec: np.ndarray, chunks: list[_Fused]) -> None:
    """Fill the TRUE cosine for lexical-only survivors so semantic_score is real, never faked (D-13)."""
    missing = [f for f in chunks if f.semantic_score is None]
    if not missing:
        return
    with connect() as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, 1 - (embedding <=> %s) FROM chunks WHERE chunk_id = ANY(%s)",
                (qvec, [f.chunk_id for f in missing]),
            )
            scores = dict(cur.fetchall())
    for f in missing:
        f.semantic_score = scores.get(f.chunk_id, 0.0)


def _agreement(semantic: list[_Candidate], lexical: list[_Candidate]) -> float:
    """Jaccard overlap of the two retrievers' candidate sets — how much they corroborate (FR-06)."""
    s = {c.chunk_id for c in semantic}
    l = {c.chunk_id for c in lexical}
    union = s | l
    return len(s & l) / len(union) if union else 0.0


def _to_chunk(f: _Fused) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f.chunk_id, source_doc=f.source_doc, parent_section=f.parent_section,
        text=f.text, corpus=f.corpus, semantic_score=f.semantic_score,
        bm25_score=f.bm25_score, rerank_score=f.rerank_score,
    )


async def retrieve(
    query: str, *, corpus: str | None = None, k: int = 20, top_n: int = 5,
    threshold: float = RERANK_THRESHOLD,
) -> RetrievedContext:
    """Hybrid retrieve: concurrent semantic + lexical → RRF → rerank → top_n + signals."""
    qvec = np.asarray(embed_query(query), dtype=np.float32)
    # Two blocking DB searches run concurrently off the event loop (FastAPI-friendly).
    semantic, lexical = await asyncio.gather(
        asyncio.to_thread(_semantic_search, qvec, k, corpus),
        asyncio.to_thread(_lexical_search, query, k, corpus),
    )
    top = _rerank(query, _rrf_fuse(semantic, lexical), top_n)
    _backfill_semantic(qvec, top)
    chunks = [_to_chunk(f) for f in top]
    return RetrievedContext(
        chunks=chunks,
        chunks_over_threshold=sum(1 for c in chunks if (c.rerank_score or 0.0) >= threshold),
        retriever_agreement=_agreement(semantic, lexical),
        degraded=(not semantic or not lexical),   # one arm empty → graceful, flagged
    )
