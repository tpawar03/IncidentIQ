"""Seed the corpus into pgvector: read -> chunk -> embed -> upsert.

Done when: completes < 2 min, and the chunks table is never empty after a run
(empty-corpus guard) so the retriever can never reach an unseeded store.
"""
from __future__ import annotations

import time
from pathlib import Path

from pgvector.psycopg import register_vector

from incidentiq.db import connect
from incidentiq.retrieval.chunking import Chunk, chunk_postmortem, chunk_runbook
from incidentiq.retrieval.embedding import embed_passages

# repo_root/corpus_sources (this module now lives at incidentiq/retrieval/)
CORPUS_ROOT = Path(__file__).parent.parent.parent / "corpus_sources"
_SCHEMA_SQL = Path(__file__).parent / "schema.sql"
POSTMORTEMS = CORPUS_ROOT / "postmortems" / "data"
RUNBOOKS = CORPUS_ROOT / "scoutflo-playbooks"

# OTel-relevant runbook subset — keeps init under the <2 min budget (TASKS.md scope note).
RUNBOOK_SUBSET = [
    "K8s Playbooks/03-Pods",
    "K8s Playbooks/04-Workloads",
    "K8s Playbooks/09-Resource-Management",
    "AWS Playbooks/01-Compute",
]
EMBED_BATCH = 64


def _rel(p: Path) -> str:
    return str(p.relative_to(CORPUS_ROOT))


def _collect() -> list[Chunk]:
    chunks: list[Chunk] = []
    for md in sorted(POSTMORTEMS.glob("*.md")):
        chunks += chunk_postmortem(_rel(md), md.read_text(encoding="utf-8", errors="ignore"))
    for sub in RUNBOOK_SUBSET:
        for md in sorted((RUNBOOKS / sub).glob("*.md")):
            chunks += chunk_runbook(_rel(md), md.read_text(encoding="utf-8", errors="ignore"))
    return chunks


def apply_schema() -> None:
    """Idempotent: create the vector extension, chunks table, and indexes if absent.
    Lives with the retrieval domain — db.connect() stays schema-agnostic shared infra."""
    sql = _SCHEMA_SQL.read_text()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        conn.commit()


def _embed(chunks: list[Chunk]) -> list[list[float]]:
    vecs: list[list[float]] = []
    for i in range(0, len(chunks), EMBED_BATCH):
        vecs += embed_passages([c.text for c in chunks[i : i + EMBED_BATCH]])
    return vecs


def init_corpus() -> int:
    chunks = _collect()
    if not chunks:                                   # pre-flight guard (no DB/model touched yet)
        raise RuntimeError("refusing to init: no source documents found (empty corpus)")
    apply_schema()
    vecs = _embed(chunks)

    with connect() as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE chunks")           # full, atomic, idempotent rebuild
            cur.executemany(
                "INSERT INTO chunks "
                "(chunk_id, corpus, source_doc, parent_section, text, token_count, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [(c.chunk_id, c.corpus, c.source_doc, c.parent_section,
                  c.text, c.token_count, v) for c, v in zip(chunks, vecs)],
            )
            cur.execute("SELECT count(*) FROM chunks")
            n = cur.fetchone()[0]
        conn.commit()
    if n == 0:                                        # post-init guard
        raise RuntimeError("post-init guard tripped: chunks table is empty")
    return n


def corpus_count() -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks")
        return cur.fetchone()[0]


def assert_corpus_ready() -> None:
    """App-startup guard (pairs with the FR-23 healthcheck): the graph must never
    run against an empty store. This is 'empty corpus never reachable.'"""
    if corpus_count() == 0:
        raise RuntimeError("corpus is empty — run `python -m incidentiq.retrieval.init_corpus` first")


if __name__ == "__main__":
    t = time.time()
    n = init_corpus()
    print(f"seeded {n} chunks in {time.time() - t:.1f}s")