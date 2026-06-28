# Task 9 — Flat LangGraph graph + Postgres checkpointer

> Builds `src/incidentiq/graph/{build,routing,checkpointer}.py`.
> Spec: `docs/AGENT_ORCHESTRATION.md` §1 (node/edge map), §2.2 (gates), §2.3 (concurrency/checkpoint).
> **Done when:** process killed mid-incident resumes from the last completed node in <10s (FR-33).

## Scope decisions (2026-06-27)

- **Walking skeleton, not full edge map.** Wire the *real* nodes that exist today
  (`alert_enricher → hybrid_retriever → rca_synthesizer`) and stub the rest as pass-through
  placeholders. Proves end-to-end flow + checkpointer durability *now*; each downstream node
  (triage, AST, patch, exec, post-mortem) gets filled in its own later task.
  - *Why:* the "done when" is **durability**, not feature-completeness. A 3-real-node graph
    exercises the checkpointer just as well as a 12-node one, with far less placeholder code
    that can't be run. Risk-first: durability is the hard-to-retrofit piece.
- **Checkpointer backend: official `langgraph-checkpoint-postgres`** (`PostgresSaver`) against
  the existing pgvector DB on host 5433 — matches §2.3 "one Postgres" and FR-33 exactly.
  Rejected hand-rolling a `BaseCheckpointSaver`: reinvents a tested package for no gain.

## Findings & Decisions Log

### F9-1 — deps installed
`uv add langgraph langgraph-checkpoint-postgres` → langgraph **1.2.6**,
langgraph-checkpoint-postgres 3.1.0 (pulls langchain-core, psycopg-pool). `psycopg[binary]`
was already a dep (the saver needs it). `from langgraph.graph import StateGraph, START, END`
and `from langgraph.checkpoint.postgres import PostgresSaver` both import clean.
Note: `langgraph.__version__` doesn't exist — use `importlib.metadata.version("langgraph")`.

### F9-2 — Pydantic state + reducers confirmed (sub-step 1)
- **Observed:** a `StateGraph(S)` over a Pydantic `BaseModel` with `status: str` and
  `trace: Annotated[list[str], add]`, run through two nodes, returned
  `{'status': 'b', 'trace': ['a ran', 'b ran']}`.
- **What it means:** LangGraph (a) accepts a Pydantic model as the schema, and (b) honors the
  `operator.add` reducer declared via `Annotated` — `trace` accumulated both nodes' writes
  instead of the second clobbering the first; `status` (no reducer) was last-write-wins.
- **Design choice:** use `IncidentState` (state.py) directly as the graph schema. No TypedDict
  rewrite, no adapter. Nodes return partial-update dicts; `errors`/`trace` accumulate, scalars
  overwrite. This is why those two fields were declared `Annotated[..., add]` back in Task 2.
- **Interview framing:** "I verified the framework honors my reducer contract with a 20-line
  probe before building on it — Pydantic-as-state + additive reducers for the audit trail
  (trace/errors) so parallel and retry paths append safely instead of racing to overwrite."

### F9-3 — enricher stays PRE-graph (deviation from §1)
- **Observed tension:** AGENT_ORCHESTRATION.md §1 draws `alert_enricher` as the first graph
  node, but enricher.py's docstring + D-9 + app.py's `run_investigation` all treat enrichment
  as a step that runs *before* the graph, so the graph only ever receives a trusted
  `IncidentContext`. `IncidentState.incident_context` is a **required** field — encoding exactly
  that "graph entry is already trusted" invariant.
- **Design choice:** keep `enrich()` in the FastAPI background task (pre-graph). Graph entry
  state already carries `incident_context`; the **first graph node is the hybrid retriever**.
  No weakening of the required field. Walking skeleton = `retriever → rca_synthesizer → stubs`.
- **Why over the §1 node version:** reinforces the D-9 trust boundary (the untrusted→trusted
  handoff happens once, outside the durable graph), and avoids making the contract optional just
  to satisfy a topology drawing. The §1 map is updated with a note recording this.
- **Interview framing:** "The graph is the *trusted, durable* core; the single untrusted→trusted
  conversion (raw webhook → enriched IncidentContext) happens once at the FastAPI boundary,
  before any checkpoint exists. So `incident_context` is required at graph entry by construction —
  the type system enforces the trust boundary."

### F9-4 — F-6 redux: editable install unreliable under conda-base; standardize on PYTHONPATH=src
- **Observed:** after the `src/` migration, `.venv/bin/python -c "import incidentiq"` failed
  with `ModuleNotFoundError` in fresh shells (3/3), even though the editable `.pth`
  (`_editable_impl_incidentiq.pth`) contained the correct `…/src` path and lived in the right
  site-packages dir. `uv pip install -e .` made it work *once*, then a fresh shell broke again —
  nondeterministic. `PYTHONPATH=src .venv/bin/python …` worked 3/3. pytest was never affected
  (it has `[tool.pytest.ini_options] pythonpath = ["src"]`).
- **Cause (CORRECTED — it was never conda):** the editable `.pth` uv writes
  (`_editable_impl_incidentiq.pth`) contains the `…/src` path with **no trailing newline**, and
  CPython's `site` module skips an unterminated final line — so `src/` never reaches `sys.path`.
  Proven by experiment: a duplicate `.pth` with the *same* path but a trailing `\n` is honored;
  the import-style `.pth` works too. Reproduces on 3.13 AND 3.14, conda present or not. The
  conda-shell theory was a red herring (the apparent flakiness was `uv pip install -e .`
  occasionally writing the file differently). pytest always dodged it via `pythonpath=["src"]`.
- **Fix:** standardize the project on ONE mechanism — `PYTHONPATH=src`. Changed Makefile
  `PY := PYTHONPATH=src .venv/bin/python` so `run`/`ingest` resolve `incidentiq.*` entrypoints
  deterministically, mirroring exactly what pytest already does. Did not chase the conda/site
  internals further — the env-var path is the canonical, reproducible equivalent.
- **Interview framing:** "src-layout is cleaner for packaging but reintroduces an import-path
  gap when the editable install is flaky; I made the whole toolchain depend on one explicit,
  deterministic signal (`PYTHONPATH=src`) instead of an implicitly-processed `.pth`, so tests,
  `-m` runs, and uvicorn all resolve the package identically."

### F9-5 — clean-init to Python 3.14 + the stale-Ollama-runner gotcha
- **Context:** did a full env teardown — deleted `.venv`, removed Anaconda/pyenv/python.org-3.13/
  uv-3.13 (kept macOS system 3.9.6, which is SIP-sealed and unremovable). Then deleted
  `pyproject.toml` + `uv.lock` and re-initialised the project on **Python 3.14**.
- **pyproject reconstructed** (not `uv init` — project already had `src/` code): `requires-python
  = ">=3.14"`, `.python-version = 3.14`, src-layout build (`packages=["src/incidentiq"]`) +
  `[tool.pytest.ini_options] pythonpath=["src"]`, and the langgraph deps. Verified
  `torch==2.12.1` resolves for cp314 before committing (PyTorch was the wheel-availability risk).
  `uv sync` → 3.14.6 venv, 64/64 fast tests… except:
- **2 Ollama tests 500'd after the move — NOT a 3.14 bug.** Root cause via curl:
  `error starting runner: fork/exec /opt/homebrew/opt/ollama/bin/ollama: no such file or directory`.
  The server still listening on :11434 was the **old Homebrew ollama process** (alive in memory
  from before cleanup); `brew autoremove` had deleted its runner binary, so `/api/version`
  answered but any real inference failed. `pkill ollama` + `open -a Ollama` switched serving to
  the standalone app (0.20.7→0.24.0, version skew resolved) → 4/4 Ollama tests pass, suite 64/64.
- **Interview framing:** "A 500 from a model server isn't necessarily your client or your runtime —
  I curled the raw endpoint, saw the server was fork/exec-ing a binary I'd just uninstalled, and
  realised a long-lived daemon was pinned to a deleted path. Restarting the service, not changing
  any code, was the fix. Also: I de-risked a major-version Python bump by resolving the heaviest
  native dep (torch) for the new ABI *before* migrating, not after."

### Env state after clean init (2026-06-28)
- Pythons on machine: macOS system `/usr/bin/python3` 3.9.6 (untouchable) + python.org 3.14.6
  framework (`/usr/local/bin/python3`); project `.venv` is 3.14.6 (uv-built on the framework base).
- Conda/pyenv/Homebrew-python all gone. Ollama = standalone app only (0.24.0), `qwen3:8b` intact.
- Project deps locked fresh in `uv.lock` for 3.14 (langgraph 1.2.x + checkpoint-postgres 3.1.x).

### F9-6 — reverted src/ → flat layout (permanent import fix)
- **Why:** the editable-`.pth` missing-newline bug (F9-4 corrected) meant `import incidentiq`
  only worked with `PYTHONPATH=src`. Rather than carry that workaround forever, moved the
  package back to repo root (`incidentiq/`), the project's original layout.
- **What changed:** moved `src/incidentiq → incidentiq`; `pyproject.toml`
  `packages = ["incidentiq"]` + removed `[tool.pytest.ini_options] pythonpath`; Makefile
  `PY := .venv/bin/python` (+ lint target `compileall incidentiq`).
- **Path bugs the move exposed (fixed):** repo-root paths computed from `__file__` had one too
  many `.parent` hops once the package shifted up a level — `catalog.py` DEFAULT_CATALOG_PATH
  (now `.parent.parent`) and `init_corpus.py` CORPUS_ROOT (now `.parent×3`). 22 tests failed on
  this; back to 64/64 after the fix.
- **Result:** bare `.venv/bin/python -c "import incidentiq"` works in fresh shells + `uv run`,
  no env var, survives `uv sync` (cwd `''` on sys.path covers a root-level package).
- **Interview framing:** "src-layout is the modern default but relies on the editable install
  being correct; when that broke (malformed `.pth`), flat layout sidestepped the whole class —
  a root package is importable via cwd with no path machinery. Moving the package also meant
  auditing every `__file__`-relative path, the hidden cost of layout changes."

### F9-7 — walking skeleton + durability DONE (sub-steps 4–6)
- **build_graph** (build.py): `StateGraph(IncidentState)` → `parallel_retriever → rca_synthesizer
  → END`, linear (conditional gates deferred to Task 10). Deps injected (retrieve_fn/synthesize_fn/
  client) so tests use fakes. Compiles clean; node ids match §6.
- **checkpointer.py**: `postgres_checkpointer()` async CM → `AsyncPostgresSaver.from_conn_string(
  DATABASE_URL)` + idempotent `setup()`. One Postgres for store + app + checkpoints (§2.3).
- **Durability proof** (tests/test_graph_durability.py): run 1 retriever succeeds + checkpoints,
  RCA raises RuntimeError (simulated kill); run 2 = fresh app + fresh saver + new event loop
  (simulated restart), same thread_id, `ainvoke(None)` resumes. Evidence = retriever call count
  stays **1** across both runs while RCA completes → resume, not restart. **2.9s, well under the
  FR-33 <10s bar.** Gated like other DB tests (skips without Postgres; excluded from `test-fast`).
- Suite: 64 fast / 65 with durability.
- **Interview framing:** "Durability isn't a claim you assert, it's one you kill a process to prove.
  I made the nodes injectable specifically so the durability test runs against the REAL Postgres
  checkpointer but FAKE nodes — proving the resume machinery in 3s without standing up Ollama, and
  the retriever's call count is the falsifiable evidence that it resumed rather than restarted."

### Task 9 status
- DONE: state contract, walking-skeleton graph, Postgres checkpointer, FR-33 durability proof.
- REMAINING (later tasks, by design): routing.py confidence gates + downstream nodes (Task 10/11);
  wiring build_graph into app.py `run_investigation` (replace the TODO; needs checkpointer in the
  FastAPI lifespan) — fold into the real end-to-end / SSE task.
