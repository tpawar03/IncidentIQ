# Task 2 — Pydantic Contract Layer

> **Status:** ✅ COMPLETE (2026-06-17) — all sub-steps done; 18 contract tests pass (22 total w/ Task 1)
> **Source:** Second Foundation task in [`../docs/TASKS.md`](../docs/TASKS.md)
> **Implements:** [`../docs/CONTRACTS.md`](../docs/CONTRACTS.md) §2 (all state schemas)
> **Builds on:** Task 1's *draft* schemas in [`../incidentiq/contracts.py`](../incidentiq/contracts.py)
>   (`RCADraft`/`TriageDraft`/`IncidentType`/draft `Citation`/`Hypothesis`).

---

## Goal

Turn every model in CONTRACTS.md §2 into runnable Pydantic v2. The headline pieces are the two
**context-dependent validators** — `Citation.chunk_id ∈ retrieved chunks` and
`CommandIntent.command_id ∈ catalog` — which need knowledge the model doesn't itself contain.

## Exit Criteria (Done when)

- [x] Every §2 state object exists and **round-trips** (serialize → deserialize → equal).
- [x] Bad input is **rejected** at every model with a constraint.
- [x] `Citation.chunk_id` validator: passes for in-set ids, raises `ValidationError` for out-of-set.
- [x] `TriageDecision` post-validator: `confidence < 0.70` coerces `incident_type = unknown` (FR-10).
- [x] `CommandIntent` catalog validator: rejects unknown `command_id`; validates args vs catalog schema.
- [x] Unit tests (valid + invalid) for each validator. (15 in `tests/test_state_validators.py`.)
- [x] `IncidentState` threaded state with additive reducers (2f).

## Layout decision (D-2)

- `contracts.py` (unchanged) = **lean LLM-facing draft** schemas — what the grammar emits.
- `state.py` (new) = **full state objects** — draft fields + post-hoc computed fields
  (`entailment_score`, `confidence_score`, `confidence_breakdown`, `llm_incident_type_raw`, …).
- Rationale: F-8 (draft ≠ full). A grammar-constrained model must not emit fields computed
  after generation; keeping them out of the draft schema keeps the grammar honest.

## Sub-steps

- [x] **2a** — enums + pure data models (Deploy, IncidentContext, RetrievedChunk/Context,
      full Citation, Hypothesis, ConfidenceBreakdown) + round-trip test. ✅
- [x] **2b** — grammar-constrained state objects (RCAReport, TriageDecision, RemediationPlan,
      CodeContext, Patch) + review/execution/post-mortem models. ✅
- [x] **2c** — 🎯 `Citation.chunk_id` validator via validation context (F-9), fail-closed. ✅
- [x] **2d** — `TriageDecision` post-validator (confidence < 0.70 → unknown, FR-10) + advisory
      `llm_incident_type_raw` so coercion isn't lossy. ✅
- [x] **2e** — 🎯 `CommandIntent` catalog validator via context (membership + arg schema),
      fail-closed; `validate_command_args` shared with the Task-3 renderer. ✅
- [x] **2f** — `IncidentState` threaded state with additive reducers (`Annotated[list[...], add]`). ✅

---

## Findings & Decisions Log

> Format per entry: **observed → what it means → design choice → interview framing.**
> Continues the F-/D- numbering from Task 1 (last was F-11, D-1).

**D-2 — Split full state objects (`state.py`) from LLM draft schemas (`contracts.py`).**
- *Observed:* CONTRACTS §2.3 `Citation` carries `entailment_score`; the Task-1 draft `Citation`
  does not. RCAReport's full `Citation` also feeds the post-hoc confidence/citation-audit path.
- *Means:* the model must NOT grammar-emit fields that are computed after generation.
- *Choice:* `contracts.py` = lean grammar drafts; `state.py` = full objects (draft fields +
  computed fields). `IncidentType` imported from `contracts.py`, not re-declared.
- *Interview framing:* "The contract the model is constrained to and the contract the system
  stores are deliberately different objects. Keeping computed fields out of the grammar keeps
  the decoder honest — it can't hallucinate a confidence it was never asked to produce."

**F-12 — Pydantic v2 smart-copies mutable field defaults.**
- *Observed:* `IncidentContext` declares `last_deploys: list[Deploy] = []` / `related_alerts = []`
  (mutable defaults), yet instances don't share state and round-trip is clean.
- *Means:* Pydantic deep-copies a mutable default per instance — effectively `default_factory=list`.
- *Choice:* keep the spec's terse `= []`; no need for explicit `Field(default_factory=...)`.
- *Interview framing:* "Same line in a `@dataclass` is the classic shared-mutable-default bug;
  Pydantic v2 copies it per instance. Know the mechanism — the footgun returns the moment you
  drop to a dataclass or a raw function default."

**F-13 — Tuple vs named model is decided by "who emits it," not terseness.**
- *Observed:* `PostMortem.timeline` is `list[tuple[datetime,str,str]]` while `action_items` is
  `list[ActionItem]` (named model). `confidence_score` carries `Field(ge=0, le=1)` though a node
  computes it.
- *Means:* a tuple → JSON-Schema `prefixItems`; two adjacent `str` slots (actor/event) make a
  transposition silently valid. `timeline` is *code-reconstructed* from `ExecutionLog` (FR-17),
  so positional is safe; `action_items` is *LLM-emitted*, so it's a named model.
- *Choice:* keep both as specced; rule = **named models for LLM-emitted structures, tuples fine
  for code-constructed ones.** Range constraints on computed fields = defense in depth.
- *Interview framing:* "The post-mortem schema shows the discrimination in one object — terse
  tuple for what my code assembles, named model for what the model emits."

**D-3 — `Citation.chunk_id` validated via Pydantic validation context, FAIL-CLOSED.**
- *Observed (F-9):* a `Citation`'s `chunk_id` must be ∈ the retrieved set, but that set lives in
  `RetrievedContext`, not on `RCAReport`. A normal validator sees only the model's own fields.
- *Means:* need external knowledge at validation time → Pydantic v2 `info.context`
  (`model_validate(data, context={"valid_chunk_ids": {...}})`). Context is unreachable through the
  plain `__init__`, so requiring it makes ungrounded reports **unconstructable**.
- *Choice:* `@model_validator(mode="after")` reading `info.context`; **fail-closed** — absent
  context raises. Single blessed constructor `RCAReport.grounded(retrieved=...)`; helper
  `chunk_id_context(retrieved)` is the one definition of "the citable set." A hallucinated
  chunk_id raises `ValidationError`, which rides Task 1's existing 1-retry path once the RCA
  node threads context through `generate_structured` (wiring deferred to that task).
- *Consequence:* re-validating a stored `IncidentState` from a dict (checkpointer rehydration)
  must pass context too; since the persisted state carries its own `retrieved_context`, the loader
  re-derives the id set from the payload — grounding is **re-proven on every load, not trusted
  because it was stored.** (Direct nested-instance assignment isn't re-validated by default —
  `revalidate_instances='never'` — so the live path is unaffected.)
- *Interview framing:* "Citation grounding is the contract that makes the model's sources
  non-fabricable. Fail-closed: no constructor yields an RCAReport whose citations weren't checked
  against a real retrieval set, and rehydration re-checks rather than trusting the bytes on disk."

**F-14 — Coercion vs rejection, and fail-open vs fail-closed, are chosen per-validator.**
- *Observed:* `TriageDecision` coerces `incident_type → unknown` below threshold (FR-10) instead
  of raising; threshold is context-injectable, default `0.70` is a PLACEHOLDER (MF-1, calibration-
  derived); the validator is fail-OPEN (safe default exists) — the mirror of 2c's fail-closed.
  The pre-coercion guess is preserved in `llm_incident_type_raw` (advisory only).
- *Means:* low confidence is *valid* data (honest uncertainty), not invalid → coerce, don't reject;
  a retry can't fix uncertainty. The coerced type and the model's raw guess must both survive.
- *Choice:* coercion writes the safe `unknown` into `incident_type` (what routing reads) while
  preserving the original in `llm_incident_type_raw` — never routed, reusing the decision-#1
  pattern from `RCAReport.llm_confidence_raw`. Guard prevents clobbering on re-validation;
  coercion is idempotent under round-trip.
- *Interview framing:* "Three knobs per validator: reject vs coerce (invalid, or just uncertain?),
  and fail-open vs fail-closed (is there a safe default?). Triage coerces and fails open; citations
  reject and fail closed. And I never silently lose the override — the raw guess is kept advisory,
  same as raw confidence, so safety doesn't cost auditability."

**F-15 — `CommandIntent` catalog validator is the structural safety backstop (fail-closed).**
- *Observed:* `command_id` validated ∈ catalog (passed via context); args checked against the
  catalog arg schema (`validate_command_args`); no catalog in scope → reject.
- *Means:* even a prompt-injected `command_id="delete_everything"` has no catalog key → rejected
  (CI-4 demo proven at the contract layer). `args: dict[str, str|int|bool]` makes a raw shell
  string structurally unrepresentable. `bool`-is-`int` trap handled explicitly in arg typing.
- *Choice:* fail-closed (no safe default for "what's allowed"); `validate_command_args` is the
  single arg-check function, reused by the Task-3 deterministic renderer (defense in depth —
  validated at the contract boundary AND at render time). Real `catalog/commands.yml` = Task 3.
- *Interview framing:* "Unsafe actions aren't prevented by asking the model nicely — they're
  structurally impossible. The only executable shape is `{command_id, args}`, and `command_id`
  must resolve to a catalog entry or it never validates. The injection corpus proves the backstop,
  not the prompt."

**F-16 — One `Annotated` serves two consumers; the LangGraph reducer needs no LangGraph.**
- *Observed:* `errors: Annotated[list[TypedError], add] = []` validates fine under Pydantic with
  no langgraph installed; `add in IncidentState.model_fields["errors"].metadata` is `True`.
- *Means:* Pydantic consumes only the metadata it recognizes (the `list[TypedError]` type) and
  passes the rest through; LangGraph later reads `add` as the merge function. `add` is stdlib
  `operator.add`, and `add(list_a, list_b)` concatenates — so two nodes appending in the same
  superstep keep *both* writes instead of clobbering.
- *Choice:* define the threaded-state contract now; the reducer metadata sits inert until the
  orchestration task wires LangGraph. Contract-before-graph sequencing made concrete.
- *Interview framing:* "The reducer is data on the type, not behavior in the model. Pydantic and
  LangGraph read the same `Annotated` for different purposes, which is why the contract layer can
  ship before the graph engine exists — and why concurrent error/trace writes are additive, not
  last-write-wins."

---

## Worktree note

This task was built directly on `main` in `~/Desktop/IncidentIQ` (the harness-provided worktree
`.claude/worktrees/jovial-grothendieck-6c4246` was not used). Source of truth = main repo.
