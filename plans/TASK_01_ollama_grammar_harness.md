# Task 1 — Ollama + Grammar-Constrained Output Harness

> **Status:** Planned (not started)
> **Source:** First Foundation task in [`../docs/TASKS.md`](../docs/TASKS.md)
> **De-risks:** Decision #2 (grammar-constrained decoding) and Decision #7 (single-semaphore concurrency)
> **Why first:** Highest-risk bet. If a self-hosted 8B model can't reliably emit schema-valid
> structured output, the whole design is invalid. Validate before anything depends on it.

---

## Goal

Prove that a self-hosted 8B model (Qwen3-8B via Ollama) can reliably emit schema-valid
structured output, and lock the concurrency + timeout primitives that every downstream agent
node inherits.

## Exit Criteria (Done when)

- [x] Invalid-JSON rate **< 1%** — measured **0.0% (0/100)** with grammar-constrained decoding (F-11; see `docs/latency_budget.md`)
- [x] Warm-model latency measured at the **worst case** — single RCA ~11.85s → **N=3 ≈ 35.6s** vs <60s;
      recorded in `docs/latency_budget.md` (real but tight; ~24s headroom for the rest of the pipeline)
- [x] Pre-warm path proven — ~2.4s cold-load moved off the incident path (F-10); `prewarm_llm()` best-effort
- [x] Hung generation → `TypedError(kind="llm_timeout")` / bad output → `invalid_json` after 1 retry → `LLMCallError`, not a crash (3c)

---

## Components to Build

### 1. `OllamaClient` wrapper
- Async client around Ollama's `/api/chat` (or `/api/generate`) with `format=<json-schema>` for
  grammar-constrained decoding (decision #2).
- Use the Pydantic model's `.model_json_schema()` directly as the constraint, so the contract
  *is* the grammar.
- A single module-level `asyncio.Semaphore(1)` guarding every model call (decision #7) — serial
  incidents; this is also where N=3 self-consistency will later contend.
- Generic `generate_structured(prompt, schema_model) -> BaseModel` that decodes →
  `model_validate` → returns the typed object.

### 2. Timeout + retry envelope (MF-2)
- Per-call `asyncio.timeout` ceiling; on timeout → `TypedError(kind="timeout")`.
- **1 retry** on invalid/unparseable JSON; second failure → `TypedError(kind="invalid_json")`.
- Retry is a **fallback only** — the grammar constraint is the primary mechanism.
- Both typed errors route to escalation (decision #10) — nothing raises past the node boundary.

### 3. Model pre-warm on startup (MF-2)
- One throwaway call each to: the LLM, the `bge-base-en-v1.5` embedder, and the
  `bge-reranker-base` cross-encoder, so cold-load never hits a live incident's latency path.
- Embedder/reranker are stubbed-warm here; their real use lands in the retriever task — this
  task only establishes the warm-up hook.

### 4. Validation harness (the actual de-risking)
- 20 sample prompts → constrain to `RCAReport` and `TriageDecision` schemas
  (from [`../docs/CONTRACTS.md`](../docs/CONTRACTS.md) §2.3 / §2.4).
- Measure: invalid-JSON rate, and **worst-case warm latency** (single call + the 3×4s N=3 ceiling).
- Record results in a short artifact next to the `<60s` claim so the latency budget is grounded
  in measurement, not the spec.

---

## Scope Boundaries

- **Do NOT** implement the composite confidence formula or the N=3 vote logic here — those belong
  to the RCA synthesizer task. This task only proves the *primitive* (constrained decode +
  semaphore + timeout) and *budgets* the 3×4s ceiling.
- **Do NOT** hardcode the 0.65 / 0.70 thresholds or `w_self` / `w_ret` weights — they are
  placeholders until the calibration task (MF-1).
- Schemas come from the contract layer (Task 2). The harness needs real schemas to validate
  against, so **build the Pydantic contract layer (Task 2) and this harness in tandem** (or stub
  minimal `RCAReport` / `TriageDecision` and replace with the real import).

---

## Suggested File Layout

```
incidentiq/llm/ollama_client.py            # OllamaClient, semaphore, generate_structured
incidentiq/llm/warmup.py                   # pre-warm hook (LLM + embedder + reranker)
incidentiq/errors.py                       # TypedError construction helpers
eval/harness/structured_output_smoke.py    # 20-prompt invalid-JSON + latency measure
docs/latency_budget.md                     # measured worst-case vs <60s claim (new or appended)
```

---

## Related Decisions (from architecture-decisions)

- **#2** Structured output = grammar-constrained decoding (Ollama JSON-schema / Outlines);
  retry is fallback only.
- **#7** Concurrency = single global async semaphore over Ollama; serial incidents;
  N=3 self-consistency on RCA vote only.
- **#10** Error flow = typed failures written into `IncidentState`; one central
  escalation/unknown terminal node.
- **MF-2** Pre-warm models + per-call timeout + 1 retry; measure worst-case latency.

---

## Findings & Decisions Log (built incrementally)

> Running notes captured *while building*, for later reference and interviews. Each entry:
> what we observed → what it means → the design choice it drove → how to frame it.

### F-1 — Grammar-constrained decoding works on a self-hosted 8B (core bet validated)
- **Observed:** A 20-line spike fed `WeatherReport.model_json_schema()` to Ollama's `/api/chat`
  `format` field. qwen3:8b returned perfectly-shaped JSON — correct keys, correct types — that
  `model_validate_json()` accepted on the first try. No markdown fences, no prose wrapper.
- **What it means:** Decision #2 (grammar-constrained decoding, not "ask nicely + regex the
  output") is viable on local hardware. This was the single highest-risk assumption in the whole
  project; de-risking it first means everything downstream rests on proven ground.
- **Design choice:** The Pydantic contract *is* the grammar. `Model.model_json_schema()` → the
  `format` field → guaranteed-shape output → `model_validate`. One mechanism, reused for every
  structured node (`RCAReport`, `TriageDecision`, `RemediationPlan`, `PostMortem`).
- **Interview framing:** *"Instead of prompting for JSON and parsing defensively, I constrain the
  decoder with the JSON Schema my Pydantic model already produces. Structure becomes a hard
  guarantee at the sampling level, not a hope — retry logic is a fallback, not the primary path."*

### F-2 — Schema guarantees *shape*, not *content discipline*
- **Observed:** In the same spike, the `conditions: str` field came back as a 400-character travel
  brochure. The schema was satisfied (it's a string) but the content was undisciplined.
- **What it means:** Grammar constraints enforce the *structure* (which keys, which types,
  enums, required fields) — they do **not** enforce brevity, relevance, or semantic correctness.
  Those remain a prompt-engineering + validation concern.
- **Design choice:** Lean on two schema-level levers the model also sees: `Field(description=...)`
  to steer content, and `Field(max_length=...)` which Ollama attempts to honor. Plus prompt
  discipline. This is *why* the real `CONTRACTS.md` models carry rich field descriptions rather
  than bare types.
- **Interview framing:** *"Constrained decoding solved my parsing problem but not my quality
  problem. A string field will always be valid JSON and can still be garbage. So the schema does
  double duty — it's both the parser contract and, via field descriptions and length bounds, a
  lightweight content guide."*

### F-3 — One global semaphore over the model (decision #7)
- **Observed/Reasoned:** qwen3:8b is ~5GB of weights on a 16GB box. Concurrent generations
  (multiple incidents, or the N=3 self-consistency votes) would thrash memory and tank latency.
- **Design choice:** A single module-level `asyncio.Semaphore(1)` that **every** model call must
  acquire — serial access, process-wide, no exceptions. Incidents are processed serially; even the
  N=3 RCA votes run one-at-a-time behind this gate.
- **Why module-level:** it's shared across the whole process automatically, so there's no way for a
  new code path to accidentally bypass it.
- **Interview framing:** *"Self-hosting a single model on commodity hardware means concurrency is a
  liability, not a feature. I made serialization a structural invariant — one global semaphore the
  whole system funnels through — so no future code path can accidentally double-load the model."*

### F-4 — Suppress reasoning-model `<think>` output at the request (`think: False`)
- **Observed:** qwen3 is a *reasoning* model that can emit `<think>...</think>` blocks. With
  `format=schema` Ollama generally suppresses it, but relying on that is fragile.
- **Design choice:** Set `"think": False` explicitly in the request body rather than leaving it to
  default behavior — make the suppression a deliberate, visible contract at the call site.
- **Interview framing:** *"Reasoning models can leak chain-of-thought into the output channel. I
  disabled thinking explicitly at the API boundary so the structured-output guarantee doesn't
  depend on undocumented default behavior."*

### F-5 — Don't root a project venv on Anaconda (a real debugging story)
- **Symptom:** `import incidentiq` failed with `ModuleNotFoundError` even though `uv pip list`
  showed the package "installed" and the editable `incidentiq.pth` correctly pointed at `src/`.
- **Isolation steps (the useful part for interviews):**
  1. Confirmed the package was registered but not importable — so it's a *path* problem, not a
     *build* problem.
  2. Checked the editable `.pth` — it correctly contained `.../IncidentIQ/src`.
  3. Dumped `sys.path` — `src` was **absent**, so the `.pth` wasn't being applied.
  4. Verified `site` was enabled (`sys.flags.no_site == 0`) and that `site` could *see* the
     `.pth` file — yet even a **manual** `site.addsitedir(...)` refused to add the (existing,
     absolute) path.
  5. Read `pyvenv.cfg`: `home = /opt/anaconda3/bin` — the venv was built on **Anaconda's**
     Python, whose customized startup/`site` doesn't process editable `.pth` files in a uv venv.
- **Root cause:** Anaconda's Python interferes with standard `.pth`-based editable installs in a
  uv-created virtual environment.
- **Fix:** Recreate the venv on a **uv-managed standalone CPython** instead of Anaconda
  (`uv python install 3.13` → `uv venv --python 3.13 --managed-python` → `uv sync`). Confirmed
  `pyvenv.cfg` `home` now points at uv's CPython; import works; tests pass.
- **Takeaway / interview framing:** *"My package looked installed but wouldn't import. I bisected
  it — build vs. path, then `.pth` content vs. `.pth` application — down to `sys.path` not getting
  the editable entry, and traced it to the venv being rooted on Anaconda's Python, which patches
  startup behavior. I rebuilt the environment on a standalone interpreter. The general lesson:
  keep project virtual environments off your Anaconda base; use an isolated, project-pinned
  interpreter so tooling behaves predictably."*
- **Operational note:** Always run project commands through `uv run ...` (which uses `.venv`), not
  the active `(base)` conda Python.

### F-6 — Editable `src/`-layout install was unreliable → switched to flat layout
- **The fuller story (F-5's Anaconda fix was necessary but NOT sufficient):** After moving off
  Anaconda to a uv-managed CPython, the editable install *still* intermittently broke. Symptom:
  `import incidentiq` worked right after a forced `uv sync --reinstall`, then broke again after the
  next `uv run` — because **`uv run` does an implicit re-sync**, and the incremental editable
  reinstall left the package unimportable.
- **Deep diagnosis:** The editable `.pth` file contained a correct absolute path to the package
  dir, and `os.path.exists()` on that path returned `True` — yet `site.addpackage()` refused to add
  it to `sys.path`. Reproduced across **two build backends** (`uv_build` and `hatchling`) and **two
  interpreters** (Anaconda + uv-managed). Conclusion: this toolchain's `.pth`/`site` editable-install
  handling is broken in a way not fixable from application code.
- **Decision:** Abandon the `src/` layout's editable-install dependency. Move the package to a
  **flat layout** (`incidentiq/` at the repo root) so `import incidentiq` resolves from the working
  directory — no editable `.pth`, no `site` processing in the path. Build backend stays `hatchling`
  with `packages = ["incidentiq"]`.
- **Trade-off (state it honestly in interviews):** `src/` layout's value is that tests run against
  the *installed* package, catching packaging mistakes early. Flat layout gives that up. For a
  single-app project where reliability of the dev loop matters more than packaging-edge-case
  detection, that's an acceptable trade.
- **Verification:** 4× consecutive `uv run python` imports, `uv run pytest`, and a post-pytest
  import all pass — the failure mode is gone, not just hidden.
- **Interview framing:** *"I hit an editable-install bug where the package's `.pth` pointed at a
  real directory but `site` wouldn't add it to the path, and every implicit re-sync re-broke it. I
  reproduced it across build backends and interpreters to prove it was the toolchain, not my code,
  then made a pragmatic call: drop the src layout for a flat one to eliminate the editable-install
  dependency entirely. I documented the trade-off rather than pretending src layout is always
  strictly better."*

### D-1 — Extended the `TypedError.kind` enum with `llm_timeout`
- **Gap:** The `CONTRACTS.md` `kind` enum had `clone_timeout` (git clones) but no kind for an LLM
  generation timeout — the exact failure the harness must handle (MF-2).
- **Decision:** Add a dedicated `"llm_timeout"` rather than folding it into `"other"`. Updated both
  `incidentiq/errors.py` and `CONTRACTS.md` so doc and code stay in sync.
- **Rationale / interview framing:** *"Typed errors only pay off if the kind is specific enough to
  route and debug on. A hung model and a generic failure need different escalation messaging, so I
  gave the timeout its own kind instead of collapsing signal into a catch-all."*

### F-7 — Test the harness logic by mocking the model boundary, not the model
- **Observed:** The retry-once and fail-fast-on-timeout paths are tested by monkeypatching
  `httpx`'s `post` to return malformed content / raise `ReadTimeout` — asserting `kind` and the
  exact attempt count — instead of trying to make qwen3 emit bad output.
- **Why:** Under grammar constraints the model essentially *can't* be coaxed into invalid JSON on
  demand; such a test would be flaky and slow. The retry/timeout logic is *our* code, so we mock
  the collaborator, control the input precisely, and assert control flow deterministically.
- **Split:** Mock the collaborator to test *our logic* (fast unit tests); use the real model to
  test the *integration* (the 20-prompt validation harness, 3f). Both matter; they test different
  things.
- **Interview framing:** *"I separated unit from integration: the retry/timeout behavior is
  deterministic code, so I mocked the HTTP boundary and asserted the exact attempt count and error
  kind. I save the real-model calls for the integration harness that measures invalid-JSON rate and
  latency. You don't test your own control flow against a stochastic dependency."*

### F-8 — The grammar-constrained schema is a "draft", not the full state object
- **Insight:** The LLM-facing schema must contain **only the fields the model legitimately
  decides**. The final `RCAReport`/`TriageDecision` state objects carry computed fields that the
  model must NOT emit:
  - `RCAReport.confidence_score` + `confidence_breakdown` → composite, computed post-hoc from
    retrieval + self-consistency signals (decision #1). The LLM may emit only `llm_confidence_raw`
    (advisory).
  - `TriageDecision.rule_prior` / `rule_prior_strength` / `llm_agreed` → set by the deterministic
    rule layer (decision #4), not the model.
- **Design choice:** Define separate **draft** models (`RCADraft`, `TriageDraft`) that hold just the
  model's output. These are what we feed to `model_json_schema()` for grammar constraining. The
  graph node later builds the full state object by layering computed fields on top of the draft.
- **Why it matters:** Feeding the full schema would (1) order an 8B model to *fabricate* its own
  confidence — the exact anti-pattern decision #1 forbids — and (2) bloat the grammar with fields
  the model can't meaningfully fill.
- **Also applied F-2 here:** the draft fields carry `Field(description=...)` to steer content and
  `Field(max_length=...)` to prevent the "travel-brochure" rambling we saw in the spike.
- **Interview framing:** *"I split the model's output contract from the system's state contract. The
  LLM only emits what it's qualified to decide — root cause, service, citations. Confidence is
  computed by the system from measurable signals and layered on afterward, so the model can never
  inflate its own trust score. The grammar constraint enforces exactly that boundary."*

### F-9 — Schema guarantees citation *structure*, never citation *grounding*
- **Observed:** A real `RCADraft` generation cited `chunk_42` and `chunk_77` — both real IDs from
  the prompt (not hallucinated), and `llm_confidence_raw` came back `null` (model didn't fabricate
  confidence — decision #1 boundary holding), and `probable_cause` stayed concise (F-2 working).
- **The trap:** The citations were grounded **only because the chunk_ids were literally in the
  prompt**. In production, retrieval returns many chunks and the model can invent a plausible-looking
  `chunk_id`. The grammar constraint guarantees a citation is *shaped* right (has a `chunk_id`
  string) — it does **nothing** to guarantee that id *exists in the retrieved set*.
- **Implication:** The `RCAReport` validator ("every `Citation.chunk_id` ∈ retrieved chunk_ids,
  else `ValidationError` → retry") is **non-negotiable** and belongs to the RCA node, which has the
  retrieval context. Grounding is a *runtime* check against retrieved data, not a static schema
  property — so it can't live in the draft model alone; it needs validation context.
- **Interview framing:** *"My structured output looked perfect — grounded citations, no fabricated
  confidence. But the grounding was an artifact of an easy prompt. The schema can only guarantee a
  citation has the right shape, not that its chunk_id is real. So citation grounding is enforced as a
  runtime validator against the actual retrieved set, with a retry on failure — structure and
  grounding are two different guarantees and I enforce them in two different places."*

### F-10 — Pre-warm payoff measured (~2.4s of cold-load moved off the incident path)
- **Observed:** After `ollama stop` (forcing a true cold start), the first constrained generation
  took **2.84s**; the immediately-following warm call took **0.42s**. The ~2.4s delta is the model
  loading its weights into memory.
- **What it means:** MF-2's pre-warm isn't theoretical — that 2.4s is a fixed tax that, without
  warmup, would land on the *first real incident* during an outage. Warming at startup pays it once,
  off the latency path.
- **Caveat:** This used the trivial `_Warmup` schema; a real `RCADraft` generation is longer. The
  *delta* (cold-load cost) is roughly schema-independent — it's the weight load, not the generation.
- **Interview framing:** *"I measured the cold-vs-warm gap by force-unloading the model: ~2.8s cold
  vs ~0.4s warm. That ~2.4s is pure weight-loading. Pre-warming at startup moves that fixed cost off
  the first incident, which is precisely when you can't afford it."*

### F-11 — Validation harness results: 0% invalid, but RCA latency ~3× the plan's assumption
- **Run (REPEATS=1, 20 samples = 10 scenarios × {RCA, triage}, warm):**
  - **invalid-JSON rate: 0.0%** (0/20) — core bet validated end-to-end (decision #2).
  - latency warm: min 3.73s, median 6.69s, **max 11.35s**.
  - worst single RCA call: **11.35s** → **N=3 self-consistency worst-case ≈ 34s** (vs the <60s budget).
- **The finding:** The plan budgeted N=3 RCA at ~3×4s ≈ 12s. Measured worst case is ~3×11.35 ≈
  **34s** — within 60s but consuming most of it, leaving only ~26s for retrieval + rerank + triage +
  patch-gen + everything else. The end-to-end budget is **real-but-tight**, not comfortable.
- **Statistical caveat (honesty):** 20 samples can only resolve an invalid rate down to 5% (1
  failure = 5%). 0% here is *consistent with* <1% but doesn't *prove* it; a defensible <1% claim
  needs ~100+ samples (REPEATS=5). The single-run max (11.35s) is also a noisy worst-case — more
  samples give a stabler p95/max.
- **Mitigation levers (for later tasks, not Task 1):** cap output tokens (`num_predict`) / tighten
  `max_length`; adaptive-N self-consistency (escalate N only for borderline cases, SF-2); these
  trade a little accuracy for headroom if the end-to-end budget gets squeezed.
- **Interview framing:** *"My structured-output validity was 0% invalid, but the harness also caught
  that my latency assumption was optimistic — worst-case RCA was ~11s, not 4s, so N=3 self-consistency
  is ~34s of my 60s budget. I'd rather find that in a harness than a demo. It tells me the latency
  budget is real but tight, and points at concrete levers — token caps, adaptive-N — if later stages
  eat the remaining headroom."*

