# Task 12 — Infra + config remediation paths

**Goal:** Replace two of Gate B's three remediation placeholders with real nodes:
`runbook_executor` (infra) and `config_diff_analyzer` (config). Each emits a
`RemediationPlan` of **catalog command intents** — the LLM picks a `command_id` + fills
args (doc line 249, FR-12/36), the catalog backstops; **never a shell string**.

**Done when:** `adServiceHighCpu` → runbook plan; flag scenario → `flag_rollback` intent;
no shell strings; unsafe-action = 0%. (`ast_code_retriever` stays an `END` placeholder —
that's the separate AST/code task.)

Spec: `docs/AGENT_ORCHESTRATION.md` Flow B + routing table §6 + line 249 (LLM emits id+args).
Builds on Task 3 catalog/renderer, Task 10 gates, Task 11 sinks.

---

## Findings & Decisions Log

### F12-1 — The F10-6 CommandIntent ↔ LangGraph collision recurs (and is now LIVE)
- **Observed:** `CommandIntent.command_is_in_catalog` is fail-closed: no `catalog` in
  `info.context` → `raise`. LangGraph coerces `IncidentState(**input)` with NO context on every
  conditional edge and every checkpoint deserialize. Task 11's sinks had empty `steps` so zero
  `CommandIntent` validators ran — the collision was latent. Task 12 emits NON-empty `steps`, so
  the first coercion after a remediation node would raise.
- **What it means:** the exact systemic issue flagged in Task 10's F10-6 is now load-bearing. A
  fail-closed nested validator cannot survive context-free re-validation.
- **Design choice (same as RCAReport's F10-6 fix):** *enforce-with-context, trust on re-validation.*
  1. `CommandIntent.command_is_in_catalog` **no-ops when `catalog` context is absent** (still
     enforced at birth via the blessed path).
  2. New blessed constructor `CommandIntent.from_catalog(*, catalog, **fields)` — the agents mint
     intents through it, proving the command against the catalog at construction.
  3. New `IncidentState`-level `model_validator` re-checks every `remediation_plan.steps` entry
     against the REAL catalog (loaded from the file-backed allowlist, lazy import to avoid the
     `catalog ↔ state` import cycle). This is the durable guarantee — it runs on every coercion and
     is idempotent, mirroring `rca_citations_grounded_in_retrieval`.
- **Why the catalog (not an in-state sibling) is the re-validation source:** unlike RCA grounding
  (sibling `retrieved_context` lives in state), the catalog is a static, trusted, file-backed
  allowlist — the validator loads it directly. A tampered checkpoint injecting a command is still
  rejected because the real catalog is consulted, not state.
- **Defense in depth (unchanged):** birth-validation (with context) + state-level re-assert +
  the renderer's independent re-validation at execution time = three enforcement points, one
  catalog source of truth.
- **Interview framing:** "Fail-closed is right at the trust boundary but fatal under an ORM/graph
  that re-instantiates models context-free. The fix keeps the boundary strict and moves the durable
  guarantee to the state level, where the trusted allowlist can always be consulted — I predicted
  this collision in Task 10 and the same fix dropped in cleanly."

### F12-2 — A per-path MENU is a second allowlist on top of the catalog
- **Observed:** the catalog says what is *executable at all*; it does not say what is *appropriate
  for the infra vs config path*. Without a per-path filter the config agent could legitimately
  return `kubectl_rollout_restart` (a catalog command) — valid, but wrong for that branch.
- **Design choice:** each path builds its MENU from the catalog's own `remediation_class`
  (`_INFRA_CLASSES={"kubectl"}`, `_CONFIG_CLASSES={"flag_rollback","config_revert"}`). An
  in-catalog-but-out-of-menu pick is treated as a **safety violation → escalation**, not a valid
  action. Because the menu is keyed by `remediation_class`, a new catalog command auto-joins the
  right path with zero code change — the YAML stays the single source of truth.
- **Interview framing:** "Two allowlists: the catalog gates *executability*, the per-path menu
  gates *appropriateness*. The model can only ever pick from the intersection, and both are
  derived from the catalog so there's still one source of truth."

### F12-3 — `RemediationDraft.args` is an untyped `dict` (deliberate trade) + grammar risk
- **Observed:** `args: dict[str, str|int|bool]` matches `CommandIntent.args` rather than a tighter
  per-command model (e.g. `FlagRollbackArgs`).
- **Design choice / why:** a per-command args model would be a SECOND static source of truth needing
  hand-sync with `commands.yml`, and adding a catalog command would require a code change — killing
  the catalog-driven property. The generic dict keeps validation data-driven (`validate_command_args`
  against the catalog spec, enforced identically at the contract boundary AND the renderer).
- **Risk (noted, out of build scope):** grammar-constraining an open `dict` with a union value type
  is weaker than a closed object schema, so the local model could emit a malformed/odd args object.
  This is acceptable because `from_catalog` rejects anything that fails the catalog's type/enum/
  pattern checks (→ escalation) — looseness costs an escalation, never an unsafe action. Worth
  measuring in the eval (does the 8B reliably fill args for the union-dict schema?).

### F12-4 — Remediation failures re-use the single escalation sink (validates Task 11)
- **Observed:** three failure modes (empty menu, out-of-menu pick, invalid args) all raise
  `LLMCallError`. The node catches it → `{status: escalated, errors:[typed]}`, leaves
  `remediation_plan` unset; `route_after_remediation` then sends it to `escalation_node`.
- **What it means:** Task 11's "single terminal escalation sink" claim is now exercised by a NEW
  caller (a remediation node, not just Gate A). The `test_graph_remediation_failure_routes_to_
  escalation_sink` e2e proves a bad LLM pick ends with `status=escalated` and an **empty** plan.
- **Interview framing:** "Remediation failure isn't a special case — it writes a typed error and
  falls into the same central escalation sink as a low-confidence RCA. One failure path, audited."

---

## Result (2026-06-29) — DONE

- **Shipped:** `incidentiq/contracts.py` (`RemediationDraft`); `incidentiq/agents/remediation.py`
  (`plan_infra_remediation`/`plan_config_remediation`, per-path `_menu`, DATA-enveloped prompt);
  `incidentiq/state.py` (F12-1: `CommandIntent` validator now enforce-with-context + `from_catalog`
  blessed constructor + `IncidentState.remediation_steps_grounded_in_catalog`); `incidentiq/graph/
  routing.py` (`route_after_remediation`); `incidentiq/graph/build.py` (`make_remediation_node`,
  two nodes wired, Gate B placeholders swapped, post-remediation edges → HITL placeholder /
  escalation sink). Tests: `tests/test_remediation.py` (14 NEW), updated `test_state_validators.py`
  (the fail-closed test → the layered F12-1 contract) and `test_graph_durability.py` (inject a fake
  `infra_fn` so the resume test no longer dead-ends at an `END` placeholder).
- **Suite: 118 → 132 passing** (23s). Both "done-when" scenarios green (adServiceHighCpu → kubectl
  runbook plan; flag scenario → flag_rollback intent); unsafe-action = 0% (out-of-menu / non-catalog
  / injected-args all escalate with no command minted); "no shell strings" asserted structurally.
- **Two regressions caught during the build (both Edit-placement / import slips, not design):**
  (1) the new `IncidentState` validator initially OVERWROTE `rca_citations_grounded_in_retrieval` —
  restored, both now present; (2) `RemediationPlan` wasn't added to build.py's state import → a
  `NameError` at the `RemediationFn` alias — fixed.
- **Still open downstream:** `ast_code_retriever` remains an `END` placeholder (the AST/code task).
  Both remediation paths route to `END` as a `human_checkpoint` placeholder → HITL task wires the
  `interrupt()` there. Terminal sinks still → `END` until post_mortem_writer lands.
