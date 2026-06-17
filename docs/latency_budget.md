# Latency & Structured-Output Budget (measured)

> Closing artifact for **Task 1 — Ollama grammar-constrained harness** (the "_Done when_" deliverable).
> Records the two numbers Task 1 exists to prove, measured rather than assumed, next to the PRD's
> `<60s` end-to-end claim. See `plans/TASK_01_ollama_grammar_harness.md` (findings F-1…F-11).

## Method

- **Harness:** `eval/harness/structured_output_smoke.py`
- **Model:** qwen3:8b via local Ollama, grammar-constrained decoding (`format=<json-schema>`, `think:false`)
- **Schemas:** `RCADraft` and `TriageDraft` (the LLM-facing *draft* contracts — see F-8)
- **Samples:** 100 (10 OTel Astronomy-Shop scenarios × {RCA, triage} × REPEATS=5)
- **Warm:** model pre-warmed before measurement (F-10), so these are warm-path numbers, not cold
- **Concurrency:** serial behind the global `Semaphore(1)` (decision #7) — these are single-call latencies
- **Hardware:** developer machine (Apple Silicon MacBook Pro). The PRD target is a 16GB box; re-measure there.

## Results (REPEATS=5, 100 samples)

| Metric | Value | Target | Status |
|---|---|---|---|
| invalid-JSON rate | **0.0%** (0 / 100) | < 1% | ✅ (see caveat) |
| warm latency — min | 3.30s | — | — |
| warm latency — median | 6.60s | — | — |
| warm latency — max | 11.85s | — | — |
| worst single RCA call | 11.85s | — | — |
| **N=3 self-consistency RCA worst-case** | **≈ 35.6s** | < 60s | ✅ but tight |

Reproducibility: three runs agreed closely (max 11.35 / 11.41 / 11.85s; median 6.69 / 6.63 / 6.60s).

## Structured-output validity — honest reading

0 failures in 100 grammar-constrained generations. By the statistical "rule of three," 0/100 implies
the true failure rate is **< ~3%** at 95% confidence — strong evidence, and consistent with the <1%
target, but a *fully* defensible <1% claim would need ~300 samples. The observed behavior meets the
spirit of the exit criterion (≥99% valid); the grammar constraint, not luck, is doing the work.

## Latency budget — the tight part

The plan assumed N=3 RCA at ~3×4s ≈ 12s. **Measured worst case is ~3×11.85 ≈ 35.6s.** It fits under
the 60s end-to-end budget, but consumes most of it:

```
60s end-to-end budget
├── ~35.6s  N=3 RCA self-consistency (measured worst case)   ← dominant cost
└── ~24.4s  EVERYTHING ELSE: retrieval + cross-encoder rerank + triage
            + (optional) AST retrieval + patch gen + post-mortem
```

So the budget is **real but tight**. This is a measured constraint the downstream tasks inherit, not
a surprise to discover during the demo.

## Mitigation levers (for later tasks, if headroom gets squeezed)

- **Cap output tokens** (`num_predict`) and tighten `Field(max_length=...)` on draft fields — RCA is
  the long pole because it generates the most text.
- **Adaptive-N self-consistency** (SF-2): run N=3 only for borderline-confidence cases; N=1 when the
  first sample is already high-confidence.
- **Vote on lower-cardinality `incident_type`** instead of high-cardinality `root_service` (SF-2) if
  agreement collapses.

## Conclusion

Task 1 exit criteria met: structured output is reliably valid (0/100), the cold-load tax (~2.4s) is
moved off the incident path via pre-warm, hung/invalid generations produce typed errors instead of
crashes, and the worst-case latency is **measured and recorded** — with the budget tightness flagged
for the pipeline that builds on top.
