# Task 11 — Central escalation + unknown nodes (Orchestration #3)

**Goal:** Replace two of the four `END` placeholders in the graph with real terminal sinks.
Single escalation sink reads typed errors → evidence summary; unknown sink emits evidence +
ranked hypotheses. **No commands** on either path (decision #10).

**Done when:** every Layer-C failure routes here cleanly; zero commands emitted on
unknown/escalation; branch coverage + structural safety property tested.

Spec: `docs/AGENT_ORCHESTRATION.md` Flow C (unknown, §"Flow C") + Flow D (escalation, §"Flow D"),
routing table §6. Continues from Task 10 (`route_after_rca`/`route_after_triage` already point
their low-confidence branches at `escalation_node` / `unknown_path`).

---

## Findings & Decisions Log

### F11-1 — "No commands" is a STRUCTURAL property, not a discipline
- **Observed:** `RemediationPlan.steps: list[CommandIntent] = []` with the contract comment
  "empty for unknown/escalation paths" (state.py §2.5). An empty `steps` list runs *zero*
  `CommandIntent` catalog validators and contains nothing executable.
- **What it means:** the safety guarantee ("zero commands on escalation/unknown") can be made a
  one-line invariant: `remediation_class == none and steps == []`. It does not ride on the node
  author remembering not to emit commands — it's the shape of the object.
- **Design choice:** both terminal nodes emit `RemediationPlan(remediation_class=RemediationClass.none,
  steps=[], ...)`. Mirrors Task 10's "structural disagreement→unknown" (F10-5).
- **Interview framing:** "I made the unsafe state unrepresentable on the terminal paths rather
  than relying on the node to behave — the empty plan is the proof, and one assertion covers it."

### F11-2 — The sinks are DETERMINISTIC (no LLM)
- **Observed:** decision #10 says escalation *reads* typed errors; the unknown path needs
  "evidence + ranked hypotheses", and `RCAReport.top_hypotheses` already carries them.
- **What it means:** neither sink needs to generate text via the model. Adding an LLM call at the
  very end would reopen the prompt-injection surface the whole pipeline works to close.
- **Design choice:** `synthesize_escalation` / `synthesize_unknown` are pure functions of
  `IncidentState` — string assembly from typed fields, no `OllamaClient`.
- **Interview framing:** "The terminal sink is the one place you most want determinism — it's the
  audit/Slack artifact. I kept it LLM-free so there's no late injection vector."

### F11-3 — Low-confidence escalation has no upstream TypedError; the node records one
- **Observed:** Gate A escalates when `rca_report` is `None` (an LLM error already in `state.errors`)
  OR when `confidence_score < RCA_ESCALATE_BELOW` (a valid report, **no error recorded**). The gate
  is a pure function — it cannot write state.
- **What it means:** a low-confidence escalation would otherwise reach the sink with an empty
  `errors` list and no typed reason for the post-mortem/Slack summary.
- **Design choice:** `synthesize_escalation` synthesizes a `TypedError(kind="low_confidence",
  node="escalation_node")` when `state.errors` is empty, so the reason is always typed and durable.
  The node returns it via `{"errors": [...]}` so the `add` reducer appends it.
- **Interview framing:** "The router can't write state, so the sink backfills the typed reason —
  every escalation ends up with a machine-readable cause, whether it failed hard or just wasn't sure."

  **Two mechanism notes underneath this (LangGraph specifics):**
  - *Why the gate can't do it:* a LangGraph **conditional-edge function returns only a node name** —
    its return value IS the routing target (a `str`), so it has no channel to emit a state delta.
    Only a *node* returns a `dict` that gets merged into state. Hence the typed reason must be
    written by a node (the sink), never by the router. This also keeps the gate a pure, trivially
    unit-testable predicate (the Task-10 design goal) instead of a function with side effects.
  - *Why returning `{"errors": []}` is safe:* `IncidentState.errors` is `Annotated[list, add]`, so
    LangGraph merges node output with `operator.add` (`existing + update`). An empty-list update is
    a no-op — the hard-failure path (error already typed upstream → `new_errors == []`) returns
    `{"errors": []}` and the channel is unchanged; the low-confidence path returns the one
    backfilled error and it appends. The node returns the same key unconditionally; the reducer,
    not a branch in the node, decides whether anything is added.

---

## Result (2026-06-29) — DONE

- **Shipped:** `incidentiq/agents/terminal.py` (`synthesize_escalation` → `(plan, new_errors)`,
  `synthesize_unknown` → `plan`, shared `_evidence_lines`); `incidentiq/graph/build.py`
  (`make_escalation_node`/`make_unknown_node`, two nodes registered, both `END` placeholders
  swapped for real edges, sinks → `END` until post_mortem_writer lands); `tests/test_terminal_nodes.py`
  (11 tests: builder branches, node wrappers, the F11-1 structural invariant, 3 in-memory e2e
  graph runs through both gates).
- **Suite: 107 → 118 passing** (17.5s). No Postgres needed for the e2e tests — `build_graph`
  compiles + `ainvoke`s with `checkpointer=None` and fake nodes.
- **Edge confirmed in e2e:** hard LLM failure carries the original `llm_timeout` to the sink and is
  NOT re-backfilled with `low_confidence` (the `if not state.errors` guard, F11-3); low-confidence
  RCA *is* backfilled.
- **Still open downstream:** both sinks currently route to `END`; they re-point at
  `post_mortem_writer` in the post-mortem task. Gate B's three remediation entries
  (`runbook_executor`/`config_diff_analyzer`/`ast_code_retriever`) remain `END` placeholders → Task 12,
  where the F10-6 `CommandIntent` catalog-context collision recurs.
