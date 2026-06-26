# Task 7 — Hybrid Retriever (Core pipeline #3)

**Goal:** `retrieve(query, ...) -> RetrievedContext` over the 994-chunk corpus.
Two retrievers (semantic pgvector + lexical Postgres FTS) run **concurrently**,
fuse by **RRF**, then a local **cross-encoder rerank** (bge-reranker-base) trims to
top-5. Emit the confidence signals the contract requires: `chunks_over_threshold`,
`retriever_agreement`, `degraded`. Architecture decision #5.

**Contract it must produce** (`state.py` §2.3): `RetrievedContext{chunks: list[RetrievedChunk],
chunks_over_threshold, retriever_agreement, degraded}`; each `RetrievedChunk` carries
`semantic_score`, `bm25_score`, `rerank_score` (query-time, NOT stored — F-24).

**Done when:** context recall > 0.80 on the 8 OTel golden scenarios (Layer B target).

## Decomposition (vertical slice = one query → ranked, reranked context)

1. **Two single-modality searches** — `_semantic_search` (embed_query → pgvector `<=>`)
   and `_lexical_search` (`plainto_tsquery` + `ts_rank` over the GIN tsvector). Each
   returns ranked candidates with its own raw score.
2. **RRF fusion** — pure function over the two ranked lists; rank-based, so the
   incompatible score scales (unbounded ts_rank vs 0–1 cosine) never need normalizing.
3. **Cross-encoder rerank** — score `(query, chunk_text)` pairs with bge-reranker-base
   over the fused shortlist only; sort → top-5.
4. **Signals + assembly** — `chunks_over_threshold` (rerank threshold), `retriever_agreement`
   (overlap of the two top-k sets), `degraded` (one retriever empty) → `RetrievedContext`.
5. **Concurrency + tests** — `asyncio.gather` over the two blocking searches; DB-free unit
   tests for fusion/signals + a small live retrieval smoke test.

---

## Result (COMPLETE 2026-06-25)

**Shipped:**
- `incidentiq/retrieval/retriever.py` — `_semantic_search` (pgvector cosine, precomputed qvec),
  `_lexical_search` (FTS, `&`→`|` OR-rewrite), `_rrf_fuse`, `_rerank`, `_backfill_semantic`,
  `_agreement`, and the public `async def retrieve() -> RetrievedContext`.
- `incidentiq/retrieval/reranking.py` — `get_reranker()` (cached) + `rerank_scores()` over
  `BAAI/bge-reranker-base` (CrossEncoder; ~1.1 GB, downloaded once).
- `tests/test_retriever.py` — 4 DB-free/model-free tests (fusion, agreement, rerank select).

**Live smoke (query "pods are crashlooping with OOMKilled out of memory"):** 5 chunks,
`over_threshold=5`, `agreement=0.48`, `degraded=False`; reranker reordered hard vs RRF (F-39).
**Suite 67 → 71 passing.** No new deps (sentence-transformers already present).

**Deferred:** `RERANK_THRESHOLD=0.5` is a placeholder pending the calibration task (MF-1).
Context-recall >0.80 on the 8 OTel golden scenarios is verified later with the eval harness.

## Findings & Decisions

### F-37 — pgvector type adaptation depends on SQL context (column vs operator)

- **Observed:** passing a Python `list` as the embedding worked in `init_corpus`'s
  `INSERT ... VALUES (%s)` but failed in the retriever's `embedding <=> %s` with
  `operator does not exist: vector <=> double precision[]`.
- **What it means:** with `register_vector(conn)`, pgvector adapts a list to `vector`
  only when Postgres can infer the target type from a `vector` *column* (the INSERT).
  In an operator expression there is no column context, so psycopg uses its default
  list→`double precision[]` adapter, and no `vector <=> float[]` operator exists.
- **Design choice:** pass the query vector as a `numpy.ndarray` (`np.asarray(..., float32)`).
  `register_vector` registers an ndarray dumper that always encodes as `vector`,
  context-independent — so the same param works in any SQL position.
- **Interview framing:** "Driver type adaptation can be context-sensitive; an ndarray
  pins the wire type so it doesn't matter whether the param lands in a column or an operator."

### F-38 — `plainto_tsquery` AND-matching kills lexical recall; rewrite `&`→`|`

- **Observed:** `_lexical_search` returned ZERO rows for a 5-word incident query while
  semantic returned the right runbooks. `plainto_tsquery('english', q)` produces
  `pods & crashloop & oomkilled & memory` — every lexeme AND-ed — so it requires one
  chunk containing all terms; none did.
- **What it means:** AND semantics suit short exact phrases, not natural-language
  incident summaries. A recall-oriented first-stage retriever wants ANY-term matching,
  ranked by `ts_rank` (which already rewards more/closer matches).
- **Design choice:** `replace(plainto_tsquery('english', %s)::text, '&', '|')::tsquery`.
  `plainto_tsquery` still does the safe lexeme parsing (injection property preserved —
  it only ever emits `&` between lexemes, never operators), and we flip the connectors
  to OR. Computed once in a CTE, reused by both `@@` and `ts_rank`.
- **Interview framing:** "Lexical retrieval's job is recall, not precision — that's the
  reranker's job. AND-matching is a precision tool in a recall slot, so it starves the
  fusion of lexical candidates. OR-of-lexemes + `ts_rank` is the right first stage."

### F-39 — Cross-encoder scores saturate on a homogeneous shortlist

- **Observed:** reranking a fused shortlist of all-pod-crash runbooks gave scores in a
  0.003 band (0.971–0.974), yet the *order* changed a lot vs RRF (a chunk at RRF rank ~12
  jumped to rerank #2; the RRF #1 fell out of top-5).
- **What it means:** bge-reranker (sigmoid-activated) confidently calls every candidate
  "relevant" when the shortlist is homogeneous, so absolute scores compress and the order
  among near-ties is noisy. Reranking discriminates most on *heterogeneous* shortlists.
- **Design choice:** treat `rerank_score` as a relevance gate, not a fine ranking signal —
  `chunks_over_threshold` thresholds it (relevant chunks saturate ~0.97, junk drops sharply,
  so ~0.5 separates). Don't over-read tiny score gaps in confidence math.
- **Interview framing:** "Reranker score is a good keep/drop gate but a weak tiebreaker on a
  uniform candidate set — which is why our confidence signal counts chunks-over-threshold
  rather than averaging the raw rerank scores."

### D-13 — Backfill the true cosine for lexical-only survivors

- **Observed:** `RetrievedChunk.semantic_score` is a required `float`, but a chunk that
  reaches the reranked top-5 via the lexical arm alone has no semantic score after fusion.
- **Options:** (A) backfill the real cosine for those ids; (B) coerce `None→0.0`; (C) relax
  the locked contract to `float | None`.
- **Design choice:** **(A)**. `embed_query` is hoisted to `retrieve()` so the same `qvec` is
  reused for a tiny `WHERE chunk_id = ANY(...)` cosine lookup on the ≤5 survivors. Keeps the
  Task-2 contract intact and never fabricates a score — same principle as keeping `bm25_score`
  `None` when absent (B would lie: "semantically irrelevant" vs "not in semantic top-k").
- **Interview framing:** "A required field shouldn't force a fabricated value. We had the query
  vector in hand, so the honest fix was to compute the real similarity for the few chunks that
  needed it, not to paper over the gap with a zero."

### F-40 — Honoring "concurrent BM25 + semantic" in a sync-psycopg codebase

- **Observed:** decision #5 specifies the two retrievers run concurrently (`asyncio.gather`),
  but `db.connect()` is sync psycopg and the searches are blocking DB calls.
- **Design choice:** `retrieve()` is `async`; the two blocking searches run via
  `asyncio.gather(asyncio.to_thread(_semantic_search, ...), asyncio.to_thread(_lexical_search, ...))`.
  Blocking work moves to worker threads so the event loop stays free (FastAPI-friendly) and the
  two arms genuinely overlap. `embed_query` runs once before the gather (semantic needs the vector;
  backfill reuses it). `degraded` flips if either arm returns empty → downstream knows retrieval
  was single-modality.
- **Interview framing:** "You don't need an async driver to get real concurrency for blocking I/O —
  `to_thread` offloads it so the event loop isn't blocked and independent queries run in parallel."

