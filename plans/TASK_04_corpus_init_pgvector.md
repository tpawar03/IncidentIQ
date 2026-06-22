# Task 4 — Corpus Init + pgvector

> **Status:** ✅ COMPLETE (2026-06-21) — 994 chunks seeded in 42s (<2 min); 5 corpus tests, 49 total pass
> **Source:** Fourth (final) Foundation task in [`../docs/TASKS.md`](../docs/TASKS.md)
> **Implements:** [`../docs/CONTRACTS.md`](../docs/CONTRACTS.md) §2.3 (`RetrievedChunk`/`RetrievedContext`),
>   §5 (eval oracle isolation); FR-07 (parent-child), FR-09 (resolvable `chunk_id`), FR-23 (depends_on gate).
> **Builds on:** Task 2's `RetrievedChunk` model (the row shape the `chunks` table mirrors).

---

## Goal

`init_corpus.py` seeds the document corpus into a local **pgvector** store via the local
`BAAI/bge-base-en-v1.5` embedding model, with a **500-token chunk cap** and **parent-child
runbook chunking**, behind a Docker `depends_on` health gate (FR-23).

**Done when:** corpus init completes **< 2 min**; an **empty corpus is never reachable** by the
retriever (init is a precondition, enforced).

## Exit Criteria (Done when)

- [x] Corpus init completes **< 2 min** — measured **42s** for 994 chunks (424 postmortem + 570 runbook) (F-27)
- [x] **Empty corpus never reachable** — pre-flight guard (refuse on 0 source docs) + `assert_corpus_ready()`
      startup check, paired with the compose `pg_isready` healthcheck (FR-23) (F-22, F-27)
- [x] **500-token cap holds** for every stored chunk — verified `max(token_count)=500` in-DB; enforced
      structurally with a terminating sentence/word-split fallback (F-26, F-28)
- [x] **Parent-child runbook chunking** — runbook chunks carry `parent_section` (heading); postmortems flat (F-26)
- [x] `chunk_id` is **stable + resolvable** (FR-09) — deterministic `{pm,rb}_<sha1(source|section|ordinal)>` (F-26)
- [x] Idempotent + atomic load — `TRUNCATE` + bulk insert in one transaction; re-run → identical state (F-27)
- [x] 5 DB-free corpus tests added; **full suite 49 passing**

## Corpus source (decided 2026-06-21)

Two open-source repos (confirmed reachable), cloned into a **git-ignored** `corpus_sources/`:
- **Postmortems** — [`icco/postmortems`](https://github.com/icco/postmortems) → `data/*.md`,
  YAML frontmatter + prose. `corpus: "postmortem"`.
- **Runbooks** — [`Scoutflo/Scoutflo-SRE-Playbooks`](https://github.com/Scoutflo/Scoutflo-SRE-Playbooks)
  → `{AWS,K8s,Sentry} Playbooks/<NN-Category>/*.md`, each with `Meaning`/`Impact`/`Playbook`/
  `Diagnosis` sections. `corpus: "runbook"`. **Scoped subset** (OTel-relevant) to hit the <2 min target.

We do **not** vendor these into the repo (license + bloat); they are fetched locally and gitignored.

## Components Built

### 1. pgvector infrastructure (`docker-compose.yml`)
- One `pgvector/pgvector:pg16` Postgres = vector store + app DB + (later) LangGraph checkpointer
  (AGENT_ORCHESTRATION line 262). `pg_isready` healthcheck = the FR-23 `depends_on` gate (F-22).
- Host port **5433** → container 5432 (D-6: 5432 was already bound on the host). pgvector 0.8.3 (F-23).

### 2. Schema / DDL (`incidentiq/retrieval/schema.sql`)
- `chunks` table mirrors the **durable half** of `RetrievedChunk` (F-24) — query-time scores excluded.
  `vector(768)` (bge dims), `token_count`, a `GENERATED ... STORED` tsvector.
- Indexes: **HNSW**/`vector_cosine_ops` (semantic; builds on an empty table, unlike ivfflat),
  **GIN** on the tsvector (lexical/BM25), btree on `corpus`. `CHECK (corpus IN ('postmortem','runbook'))`.
- Applied idempotently by `apply_schema()` (lives in `retrieval/init_corpus.py`; `db.py` stays
  schema-agnostic — D-7).

### 3. Embedding model (`incidentiq/retrieval/embedding.py`)
- `BAAI/bge-base-en-v1.5` via sentence-transformers, 768-dim, `normalize_embeddings=True` → cosine.
- **Asymmetric** (F-25): `embed_passages` (no prefix) vs `embed_query` (instruction prefix), one
  `@lru_cache get_embedder` singleton (= the MF-2 pre-warm hook `warmup.py` points at). `count_tokens`
  exposes bge's own tokenizer for the cap.

### 4. Chunking (`incidentiq/retrieval/chunking.py`)
- `MAX_TOKENS=500` (under bge's 512 ceiling so nothing is silently truncated, F-26).
- `chunk_runbook` = parent-child (heading → `parent_section`, FR-07); `chunk_postmortem` = flat.
- `_pack` greedy-packs paragraphs → sentence-split → `_hard_split` word windows; **provably
  terminates** (F-28). Deterministic `chunk_id` (FR-09).

### 5. Corpus loader (`incidentiq/retrieval/init_corpus.py`)
- read → chunk → embed (batched) → `TRUNCATE` + bulk insert in **one transaction** (idempotent +
  atomic, F-27). Pre-flight + `assert_corpus_ready()` guards. Runbook subset scoped to hold <2 min.
- Run: `python -m incidentiq.retrieval.init_corpus`.

---

## Scope Boundaries

- **Do NOT** build the retriever here — BM25 + semantic search, RRF fusion, cross-encoder rerank,
  min-score threshold and `retriever_agreement` all belong to the **Hybrid retriever** task. This task
  only *populates* the store and its two indexes; querying them is next.
- **Do NOT** pre-warm the embedder/reranker on app startup here — the hook (`get_embedder`) exists,
  but wiring it into `warmup.py` lands with the retriever (it's where the cold-load would otherwise hit).
- **Do NOT** ingest the full corpus — the runbook set is **scoped** to OTel-relevant categories to hold
  the <2 min budget; the exact 6-scenario roster is finalized in the `services.yml + flagd` task.
- **Eval oracle isolation (CONTRACTS §5)** is *not* exercised here — this corpus is general public
  docs (`agent_visible_docs`), no oracle split. The `eval_oracle/` invariant is a later eval task.
- Third-party corpus repos are **fetched, never vendored** (license + bloat) — `corpus_sources/` is
  git-ignored.

---

## Suggested File Layout

```
docker-compose.yml                          # pgvector Postgres + FR-23 healthcheck (host :5433)
incidentiq/db.py                            # shared infra: connect() + DATABASE_URL only (schema-agnostic)
incidentiq/retrieval/schema.sql             # chunks table + HNSW/GIN indexes
incidentiq/retrieval/embedding.py           # bge-base-en-v1.5; embed_passages / embed_query; count_tokens
incidentiq/retrieval/chunking.py            # 500-token cap; parent-child runbooks; terminating splitter
incidentiq/retrieval/init_corpus.py         # apply_schema + read→chunk→embed→upsert + guards
tests/test_corpus.py                        # 5 DB-free tests (chunk invariants, cap, ids, empty guard)
corpus_sources/                             # git-ignored: cloned icco/postmortems + scoutflo-playbooks
```

---

## Related Decisions (from architecture-decisions / PRD)

- **#5** Retrieval = hybrid BM25 + semantic → RRF → cross-encoder rerank → top-5 (this task lays the
  two indexes the hybrid retriever will query).
- **FR-07** Parent-child chunking; never an orphan sub-chunk → `parent_section` on runbook chunks.
- **FR-09** Every citation `chunk_id` resolves to a real stored chunk → deterministic, stable ids +
  one Postgres so the id is an FK target, not a cross-store promise.
- **FR-23** Docker `depends_on` health gate → the compose `pg_isready` healthcheck.
- **MF-2** Pre-warm models → `get_embedder` singleton is the embedder's pre-warm hook (wired later).
- **CONTRACTS §2.3 / §5** `RetrievedChunk`/`RetrievedContext` row shape; eval-oracle isolation (deferred).

---

## Findings & Decisions Log

_(entries added in parallel as we build — observed → means → choice → interview framing)_

**F-22 — One Postgres does three jobs; the healthcheck is the FR-23 gate.**
- *Observed:* the `pgvector/pgvector:pg16` container backs the vector store, the app DB, and (later)
  the LangGraph checkpointer — AGENT_ORCHESTRATION line 262 says "one Postgres."
- *Means:* fewer moving parts, one transaction boundary, one backup target; the vector index and the
  incident rows it cites live in the same DB so a `chunk_id` is a real FK target, not a cross-store
  promise (FR-09).
- *Choice:* a compose `healthcheck` (`pg_isready`) so dependents declare
  `depends_on: { condition: service_healthy }`. That is the literal FR-23 gate — the retriever can
  never address a store that isn't up, which is half of "empty corpus never reachable."
- *Interview framing:* "Durability, retrieval, and app state share one Postgres on purpose — the
  citation key is a foreign key, not a hope. The compose healthcheck is the dependency gate, so
  nothing in the graph runs against an unready or unseeded store."

**D-6 — Host port 5433 (not 5432).**
- *Observed:* 5432 was already bound on the host (a non-Docker Postgres not visible to our user).
- *Choice:* map host `5433 → container 5432` rather than fight for 5432; the connection string uses
  5433. Container-internal port is unchanged, so nothing inside the compose network cares.

**F-23 — pgvector 0.8.3 confirmed; `CREATE EXTENSION vector` succeeds.**
- The extension is preinstalled in the image but still must be `CREATE EXTENSION`-ed per database;
  that statement belongs in the schema DDL (sub-step 2), run idempotently (`IF NOT EXISTS`).

**F-24 — The `chunks` table stores only what is intrinsic to a chunk; query-time scores are excluded.**
- *Observed:* `RetrievedChunk` carries `semantic_score`/`bm25_score`/`rerank_score`, but the table
  does not.
- *Means:* those three are functions of `(query, chunk)`, not properties of the chunk — they don't
  exist until a query does, and would be stale for the next query. Persisting them would be a
  category error.
- *Choice:* the table stores identity + content + the machinery (`embedding`, `token_count`, a
  generated `tsv`); the retriever computes the scores at query time and fills the DTO. Two indexes
  back the hybrid retriever: **HNSW**/`vector_cosine_ops` (semantic; HNSW builds on an empty table,
  unlike ivfflat) and **GIN** on a `GENERATED ... STORED` tsvector (lexical/BM25, can't drift from
  `text`). bge vectors are L2-normalized → cosine is the right metric.
- *Interview framing:* "The schema is the chunk's identity; the scores are the query's verdict on it.
  Keeping them apart is why the same stored chunk can rank differently for two different queries —
  and why the lexical index is a generated column, so it can never disagree with the text it indexes."

**F-25 — bge is asymmetric; the query/passage prefix split lives in one module.**
- *Observed:* `bge-base-en-v1.5` was trained with an instruction prefix on the *query* side only
  (`"Represent this sentence for searching relevant passages: "`); passages are embedded raw.
- *Means:* embedding corpus chunks with the query prefix (or queries without it) pushes the two into
  slightly different sub-spaces and measurably drops recall. The asymmetry is a property of the model,
  not a caller's choice.
- *Choice:* `incidentiq/embedding.py` exposes `embed_passages` (no prefix, used by the corpus loader)
  and `embed_query` (prefixed, used by the retriever) — two functions, one singleton `get_embedder`
  (`@lru_cache`, also the MF-2 pre-warm hook the `warmup.py` TODO points at). `normalize_embeddings=True`
  → unit vectors → cosine (matches the HNSW `vector_cosine_ops` index).
- *Interview framing:* "The corpus loader and the retriever embed text on two different days in two
  different files — making them call the same module guarantees they used the same recipe, which is
  the only reason a stored passage vector and a live query vector are comparable at all."

**F-26 — The 500-token cap is enforced structurally, with a sentence-split fallback for oversize units.**
- *Observed:* `MAX_TOKENS = 500` sits under bge's 512 ceiling; `_pack` recurses into sentence-splitting
  only when a *single text unit already exceeds the cap*.
- *Means:* without the fallback, one giant paragraph (a log dump, a long unbroken `Playbook` section)
  would emit a single >500-token chunk that bge **silently truncates** at embed time — producing a
  vector for only part of the text, i.e. a quietly wrong vector that still looks valid. The fallback
  guarantees the cap holds regardless of how the source doc is formatted.
- *Choice:* paragraph-pack first (cheap, preserves coherence); only break to sentences when forced.
  Runbooks chunk parent-child (heading = `parent_section`, FR-07); postmortems chunk flat
  (`parent_section=None`). `chunk_id` is deterministic (`{pm,rb}_<sha1(source|section|ordinal)>`) so it
  is stable for FR-09 deep-linking across unchanged docs.
- *Interview framing:* "The cap isn't a hope, it's a post-condition — measured in the model's own
  tokens, with a fallback so no input shape can sneak a truncated vector into the store."

**F-27 — "Empty corpus never reachable" is two guards + idempotent atomic load; seed = 994 chunks/42s.**
- *Observed:* `init_corpus()` runs read → chunk → embed → `TRUNCATE` + bulk insert in **one
  transaction**; result: 424 postmortem + 570 runbook = 994 chunks in 42s (budget <2 min), `max_tok=500`.
- *Means:* TRUNCATE+insert in a single tx is idempotent (re-run → identical end state) *and* atomic
  (a crash mid-load can't leave a half-seeded store — it rolls back to the prior good corpus). The
  runbook set is scoped to OTel-relevant categories (K8s Pods/Workloads/Resource-Mgmt + AWS Compute)
  to hold the budget; the full 431 would blow it.
- *Choice:* the FR-23 requirement is enforced at **two** points — a **pre-flight guard** (refuse to
  init when zero source docs are found, before any DB/model work, so a broken clone fails fast and
  cheaply) and **`assert_corpus_ready()`** (app-startup check that the table is non-empty, pairing
  with the compose healthcheck). Init reordered to collect+guard *before* `apply_schema`/embed so the
  guard is testable without Docker and fails instantly.
- *Interview framing:* "An empty corpus is unreachable by construction: the loader won't produce one
  (fail-fast pre-flight), the load is atomic so you never observe a partial one, and startup refuses
  to serve against one. The healthcheck gates the connection; these gate the contents."

**F-28 — A real infinite-recursion bug, caught by the cap test on degenerate input.**
- *Observed:* `test_token_cap_holds_even_for_oversize_paragraph` (`"word "*4000`) hit `RecursionError`:
  an oversize unit with **no sentence boundaries** made `_split_sentences` return it unchanged, so
  `_pack` recursed on the identical unit forever.
- *Means:* a real single-line log dump / minified blob / huge table row in a source doc would have
  crashed `init_corpus` in production — not hypothetical, exactly the kind of content that lands in a
  postmortem. The recursion only terminates if each step *subdivides*; sentence-splitting doesn't
  guarantee that.
- *Choice:* `_hard_split` — a word-level last-resort that packs words into ≤500-token windows and
  **always makes progress**; `_pack` only recurses on sentences when `len(sentences) > 1`, else hard-
  splits. Per-word token sums slightly over-estimate (wordpiece merges across words) → windows land a
  touch under cap, the safe direction.
- *Interview framing:* "The test wasn't decoration — it found an infinite loop on degenerate input
  that real corpora contain. The fix is a termination guarantee: every branch of the splitter
  strictly reduces, so the chunker provably halts on any input."

**D-7 — Post-task refactor: retrieval modules grouped into `incidentiq/retrieval/`; `db.py` made
schema-agnostic.**
- *Observed:* the flat package was at 11 modules and the roadmap adds ~20 more (retriever, RCA, graph
  nodes, routers, executors, API). Two smells: a growing flat namespace, and `incidentiq/catalog.py`
  (module) vs root `catalog/commands.yml` (data) sharing a name.
- *Choice:* mirror the architecture layers as subpackages, starting where the next file lands.
  `embedding.py`/`chunking.py`/`init_corpus.py`/`schema.sql` → **`incidentiq/retrieval/`** (the
  `retriever.py` of the next task joins them). `db.py` kept at the package top but slimmed to
  `connect()` + `DATABASE_URL` only — **shared infra knows how to connect, not what tables exist**;
  `apply_schema()` (the chunks DDL) moved into `retrieval/init_corpus.py` next to its `schema.sql`.
  Deferred: `remediation/` (fixes the catalog name overlap), `graph/`, `api/` — built when those
  phases arrive, not speculatively. No `src/` layout (matters for libraries, not this app).
- *Run command changed:* `python -m incidentiq.retrieval.init_corpus`. Re-verified: 994 chunks, 43s,
  49 tests still green — the test suite was the refactor's safety net.
- *Interview framing:* "I let the architecture's layers drive the package layout, and I refactored at
  the cheap moment — 11 modules, full test coverage — rather than after it hurt. Infra stays
  domain-agnostic: `db` connects, each domain owns its own DDL."

