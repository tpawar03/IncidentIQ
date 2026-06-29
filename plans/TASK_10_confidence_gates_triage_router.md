# Task 10 — Confidence gates + triage router

> Builds `incidentiq/graph/routing.py` (the two gates) and `incidentiq/agents/triage_router.py`
> (hybrid triage), and wires both as conditional edges in `graph/build.py`.
> Spec: `docs/AGENT_ORCHESTRATION.md` §2.1 (routing functions), §2.2 (gate chain), decision #4 (hybrid triage).
> **Done when:** unit tests cover all branches incl. unknown default; thresholds wired to config, not
> literals. (Triage-accuracy >85% is an eval-time check → deferred to Layer A. Final threshold/weight
> VALUES come from calibration, MF-1 — placeholders until then.)

## Scope (2026-06-29)

- Two **pure** routing functions (Gate A after RCA, Gate B after triage) — no LLM, fully branch-tested.
- The **hybrid triage node**: deterministic rule prior + LLM confirm → `TriageDecision` (decision #4).
- Graph wiring: conditional edges. Downstream targets (`escalation_node`, `unknown_path`, the three
  remediation entries) are **mapped to `END`** in the `path_map` for now — Tasks 11/12 swap `END`
  for the real nodes. Routing functions already return the real §6 node names.
- Suite **83 → 107** (+24). Thresholds/weights live in `config.py` (placeholders, MF-1).

## Findings & Decisions Log

### F10-1 — gates read config, never literals; `unknown` is the DEFAULT branch
- **Observed:** Gate B (`route_after_triage`) maps `incident_type → node`; an unmapped type or a
  missing decision hits `_TYPE_TO_NODE.get(..., "unknown_path")`.
- **Means:** a low-confidence or malformed triage can never silently fall through to `infra` and
  trigger runbooks (FR-10, "never infra default").
- **Choice:** `unknown` is the fail-safe default, not a special case; thresholds come from
  `config.RCA_ESCALATE_BELOW` / `config.TRIAGE_UNKNOWN_BELOW` so calibration (MF-1) is a one-line change.
- **Interview:** deterministic routing = every branch unit-testable (FR-11) and the safety story
  auditable; a literal `0.65` in a router would be a latent calibration bug.

### F10-2 — defense-in-depth on the triage gate
- Gate B re-checks `confidence < threshold` ITSELF, even though `TriageDecision`'s own post-validator
  already coerces `incident_type → unknown` below the same threshold. Two independent guarantees that
  low-confidence never reaches a command. The router does not *trust* that the coercion happened.

### F10-3 — single source of truth for the unknown threshold
- `triage_incident` validates the `TriageDecision` with `context={"triage_threshold":
  config.TRIAGE_UNKNOWN_BELOW}`, so the validator's coercion and the router's gate read the **same**
  config value. Calibration moves both atomically — no drift between "what gets coerced" and "what
  gets routed."

### F10-4 — the rule prior: specificity ordering is a deliberate design choice
- **Observed:** keyword classifier checks **config before infra** (`_CONFIG_KEYWORDS` first).
- **Why:** `failedReadinessProbe` contains "probe" (config) but readiness failures surface as
  latency/timeout (infra keywords). Checking config first lets the more *specific* diagnosis win.
- **Choice:** a small ordered rule set, first strong match wins, `(unknown, 0.20)` fallback (never
  guesses infra). `RECENT_DEPLOY_MINUTES` (config) bumps strength for deploy-correlated priors —
  a fresh deploy is the single best predictor that this incident is that change.
- **Interview:** the prior exists for explainability + an LLM anchor + disagreement-as-signal (F10-5),
  not because rules beat the model.

### F10-5 — disagreement→unknown made STRUCTURAL, not arithmetic (the headline find)
- **Observed (boundary test trap):** `_combine_confidence` on two maximally-confident *conflicting*
  estimators returned `min(1.0,1.0) − 0.30 = 0.70`, which is NOT `< 0.70` → slipped through the gate
  as a confident decision.
- **Means:** the categorical safety property "rule↔LLM disagree → unknown" was riding on a calibration
  knob (`TRIAGE_DISAGREE_PENALTY`) landing in the right spot. Calibration could lower it and silently
  break the guarantee.
- **Choice:** in `triage_incident`, on disagreement set `incident_type = unknown` **by construction**;
  the penalty now only shapes the *reported* confidence number (for the UI/breakdown). The validator
  remains a second guard for the agreement-but-weak case.
- **Interview:** a safety invariant must not depend on a fitted weight. "Fail-closed at birth, re-proven
  in-state, trusted on re-coercion" — see F10-6.

### F10-6 — fail-closed validators collide with LangGraph state coercion (systemic)
- **Observed:** the moment a **conditional edge** was added after `rca_synthesizer`, the durability
  test fail-closed inside `langgraph...state._coerce_state → IncidentState(**input)`. LangGraph
  re-coerces the channel into the schema on every branch read and every checkpoint deserialize, with
  NO validation context — so `RCAReport`'s context-requiring grounding validator (D-3) refused.
  Confirmed it fails whether the nested value is an instance or a dict; `revalidate_instances="never"`
  does not stop it. The linear skeleton dodged it only by having no conditional edge.
- **Means:** "fail-closed without context" is hostile to serialization round-trips. Same collision will
  hit `CommandIntent` (catalog context) in Task 12 — it's systemic to context-requiring validators.
- **Choice (decision, user-approved):** **enforce-with-context, trust on re-validation.**
  - `RCAReport` validator: enforce only when `valid_chunk_ids` context is present (the `.grounded()`
    birth path — raw LLM output still rejected); context-absent → no-op.
  - New `IncidentState`-level `model_validator`: re-checks `rca_report` citations against the
    `retrieved_context` **sibling** in state. Runs on every coercion; idempotent. This is *stronger*
    than the old standalone snapshot — it re-proves grounding against the real retrieval set.
- **Interview:** the production reconciliation of strict validation with serialization —
  "fail-closed at birth (blessed constructor), re-proven in-state (sibling check), trusted on
  context-free re-coercion." Loses only the naked-`RCAReport(**fields)` self-reject, which never
  occurs in the pipeline.

### F10-7 — `model_copy(update=)` bypasses validators (reinforced twice)
- Threading state past a fail-closed validator: use `model_copy(update=...)` (what LangGraph does —
  no revalidation). TESTING a validator: use `model_validate` / `__init__` (which run it). Test D first
  failed (DID NOT RAISE) precisely because it used `model_copy` to test the new `IncidentState` check.

## Files
- `incidentiq/graph/routing.py` — `route_after_rca` (Gate A), `route_after_triage` (Gate B).
- `incidentiq/agents/triage_router.py` — `rule_prior`, `build_triage_prompt`, `_combine_confidence`,
  `triage_incident`.
- `incidentiq/graph/build.py` — `make_triage_node` + conditional edges (downstream → `END` placeholders).
- `incidentiq/state.py` — relaxed `RCAReport` validator + new `IncidentState` sibling-grounding check.
- `incidentiq/config.py` — `RECENT_DEPLOY_MINUTES`, `TRIAGE_AGREE_BONUS`, `TRIAGE_DISAGREE_PENALTY`.
- `tests/test_routing.py` (NEW, 22) + updates to `tests/test_state_validators.py`, `tests/test_graph_durability.py`.

## Deferred / follow-ups
- **Task 11:** real `escalation_node` + `unknown_path` (replace `END` placeholders); decide whether a
  triage-node typed error should route to escalation rather than `unknown_path` (currently a missing
  decision → `unknown_path`).
- **Task 12:** real remediation entries; apply the same F10-6 relaxation to `CommandIntent`'s catalog
  validator (it will hit the identical coercion collision).
- **Calibration (MF-1):** fit `TRIAGE_UNKNOWN_BELOW`, `RCA_ESCALATE_BELOW`, `W_SELF/W_RET`,
  `TRIAGE_AGREE_BONUS/TRIAGE_DISAGREE_PENALTY` from the golden set.
