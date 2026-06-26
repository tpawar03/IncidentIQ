# Task 8 — RCA Synthesizer + Composite Confidence (Core pipeline #4)

> **Goal:** turn N=3 grammar-constrained `RCADraft`s into one grounded `RCAReport` whose
> `confidence_score` is **computed from signals, not emitted by the model** (decision #1).
> Consumes the Task-1 Ollama harness + the Task-7 hybrid retriever.
>
> **Done when:** citations resolve to real `chunk_id`s; confidence is composite not raw;
> no context overflow (FR-25).

## Sub-step map
1. Config placeholders (`w_self`, `w_ret`, gate thresholds) — MF-1 discipline.
2. Prompt builder + token budget (tiktoken) + DATA envelope for chunks.
3. N=3 fan-out through the semaphore; graceful degrade (≥1 survivor).
4. Self-consistency vote + canonical draft selection.
5. Composite confidence + `ConfidenceBreakdown`.
6. Assemble via `RCAReport.grounded(...)` (citation grounding gate).
7. Model-free tests (monkeypatch `generate_structured`).

---

## Findings & Decisions Log

<!-- F-41+, D-14+ entries appended as we build. Format: observed → meaning → choice → interview framing. -->

**D-14 — Calibration knobs live in one config module, never as agent-code literals (MF-1).**
`W_SELF`/`W_RET` + gate thresholds (0.65/0.70) sit in `incidentiq/config.py` as explicit
PLACEHOLDERS. *Why:* decision #1 says confidence is calibrated, not guessed; one import site
makes the week-6 recalibration a one-line change instead of a grep. The two penalty constants
(−0.15/−0.10) stay as synthesizer literals — they're semantic event-corrections, not the
continuous weighting calibration fits. *Interview framing:* "the weights are an empirical
question the reliability curve answers; the penalties encode a fixed judgment ('a truncated
alert is worth 10% less trust'). Calibrating them too is defensible but low marginal value."

**F-41 — The citable set is the PRESENTED chunks, not everything retrieved.**
Token budgeting can drop low-ranked chunks before the model sees them. *Meaning:* grounding
against the full retrieved set would let a hallucinated citation to a real-but-dropped chunk_id
pass. *Choice:* `synthesize_rca` narrows the grounding context to `presented` chunks via
`retrieved.model_copy(update={"chunks": presented})`, so the model can only cite what it
actually saw. *Interview framing:* "grounding is only as honest as the evidence window — the
validator's valid-set must equal the model's visible-set, not the retriever's full output."

**F-42 — The budget guard must guarantee ≥1 chunk (text-truncate, don't drop to zero).**
At a pathologically small budget, every chunk overflowed → 0 presented. *Meaning:* `RCADraft`
requires `min_length=1` citation and we ground against presented — zero presented makes a valid
RCA structurally impossible. *Choice:* if nothing fits, force-include the top chunk with its
*text* token-truncated (`_truncate_to_tokens`) to the remaining budget; preserves the overflow
guarantee AND keeps a citable anchor. *Interview framing:* "a resource guard that can starve
its own output to invalidity is a latent bug — bound it on both ends."

**F-43 — Ungrounded canonical citation → escalation, fail-closed (deferred: grounded-draft fallback).**
The grounding validator runs at `RCAReport.grounded()`, AFTER the vote — outside the harness's
per-call retry. *Choice:* catch `ValidationError` → `LLMCallError(kind="other", node="rca_synthesizer")`
→ escalation (decision #10). *Deferred:* could instead fall back to another winning-service draft
whose citations DO ground before escalating; skipped to keep selection decoupled from the
presented-id set. *Interview framing:* "an ungrounded RCA is structurally untrustworthy — escalating
is the safe default; a smarter retry is a robustness optimization, not a correctness fix."

**F-44 — Measured N=3 latency ≈ 52s (live qwen3:8b, think=False, 3-chunk prompt).**
Self-consistency is sequential behind `Semaphore(1)` (~17s/generation). *Meaning:* the vote eats
most of the <60s RCA budget on its own — before triage/remediation. *Implication:* validates
N=3 (not 5) and why MF-2 pre-warming is on the critical path; adaptive-N (SF-2) would only fire
for borderline cases. *Interview framing:* "self-consistency buys calibrated honesty at 3× latency
— a deliberate, budgeted trade, not a free lunch."

**DEFERRED:** (a) prompt-level context truncation is NOT a confidence penalty yet — only the two
documented penalties (weak-retrieval, alert-truncation) apply; revisit if eval shows dropped
chunks correlate with wrong answers. (b) SF-2 fallback (vote on low-cardinality `incident_type`
if `root_service` agreement collapses) — wire only if the golden-set agreement distribution is
pathologically low.
