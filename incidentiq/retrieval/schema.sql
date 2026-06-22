-- pgvector extension (preinstalled in the image, but must be enabled per-DB)
CREATE EXTENSION IF NOT EXISTS vector;

-- One row per chunk. Mirrors the durable half of RetrievedChunk (state.py §2.3).
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        TEXT PRIMARY KEY,                 -- stable & resolvable (FR-09); PK = idempotent upsert
    corpus          TEXT NOT NULL
                      CHECK (corpus IN ('postmortem', 'runbook')),   -- mirrors the Literal in the model
    source_doc      TEXT NOT NULL,                    -- origin doc (relative path)
    parent_section  TEXT,                             -- FR-07 parent-child; NULL for flat postmortem chunks
    text            TEXT NOT NULL,                    -- chunk body — treated as DATA only
    token_count     INTEGER NOT NULL,                 -- the ≤500 cap, recorded as proof
    embedding       vector(768) NOT NULL,             -- bge-base-en-v1.5 = 768 dims
    -- lexical half of hybrid retrieval, derived from text so it can never drift:
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);

-- ANN index for semantic search (cosine; bge vectors are normalized)
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops);

-- GIN index for BM25/full-text (the lexical retriever)
CREATE INDEX IF NOT EXISTS chunks_tsv_gin
    ON chunks USING gin (tsv);

-- corpus filter (postmortem vs runbook indexes queried in parallel)
CREATE INDEX IF NOT EXISTS chunks_corpus_idx ON chunks (corpus);