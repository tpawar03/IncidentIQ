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

- [ ] Invalid-JSON rate **< 1%** across 20 sample prompts producing `RCAReport` / `TriageDecision`
- [ ] Warm-model latency measured at the **worst case** (N=3 RCA budgeted at 3×4s, *not* 3×2s)
      and recorded next to the `<60s` PRD claim
- [ ] Pre-warm path proven: no cold-load ever lands on the latency path
- [ ] Hung generation → `TypedError(kind="invalid_json" | "timeout")` → escalation, not a crash

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
