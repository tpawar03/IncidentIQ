# Task 14 — Patch generator (regen → diff → validate)

**Goal:** Replace Gate B's code-path `END` placeholders (`patch_generator`,
`code_context_only`) with real nodes. For a localized Python/JS bug, the LLM rewrites the
WHOLE broken function; deterministic code computes the unified diff and syntax-checks it
(decision #3, FR-14). On 2 failures or an out-of-scope rewrite (SF-5) → `code_context_only`
(location + TODO, no diff). The model never emits a diff or a line number.

**Done when:** Py/JS produce applying, syntax-valid diffs; a double syntax-fail or scope
violation degrades to `code_context_only` (no misleading patch); the code path lands at
`human_checkpoint` (still an `END` placeholder — HITL is a later task). PR creation is NOT
here (draft PR happens post-approval at execution, FR-38).

**Scope decision (pre-build, 2026-07-02):** at the user's call, **Go was dropped from the
patch set alongside C#/Rust** — only **Python + JavaScript** produce patches. Rationale: those
two are exactly the languages with a ZERO-INSTALL native syntax validator already on this box
(stdlib `compile()`, `node --check` — node v24 present). Go would need a toolchain (`gofmt`)
we can't validate against here, and a wrong-but-unvalidated diff is worse than an honest
"location + TODO". Consequence: Task 13's `_PATCH_SUPPORTED_LANGUAGES` narrowed from
`{python, javascript, go}` → `{python, javascript}` (updates F13-4).

Spec: `docs/AGENT_ORCHESTRATION.md` Flow A steps 7-8 + failure topology ("Patch syntax fail
×2", "Fix outside localized function"); `docs/DESIGN_DECISIONS_EXPLAINED.md` Decision 3 +
SF-5; `docs/CONTRACTS.md` §2.5 `Patch`. Builds on Task 13 (`CodeContext`, the clone cache).

---

## Findings & Decisions Log

### F14-1 — Regenerate-the-function, compute-the-diff-in-code (decision #3, made concrete)
- **What shipped:** the LLM contract is `PatchDraft` = `{new_function_body, summary}` — it only
  ever rewrites one function. `incidentiq/retrieval/patching.py` then *splices* that body over
  the localized `[start_line, end_line]` span, `difflib.unified_diff`s old-vs-new at the FILE
  level (so the diff reads like a PR: `--- a/path` / `+++ b/path`), and syntax-checks the
  spliced file. The model never counts a line.
- **Why file-level, not excerpt-level:** compiling/`node --check`ing the whole modified file
  gives a real syntax gate with correct surrounding context (a method needs its class; a
  top-level function needs the module). Diffing at file level also yields the exact artifact a
  reviewer/PR wants. The clone is a cache hit from Task 13 (<1ms), so re-materializing the file
  is free.

### F14-2 — The SF-5 scope guard is a tree-sitter shape check on the NEW body
- **Observed:** "don't produce a misleading patch" (SF-5) needs a concrete, testable trigger.
- **Design choice:** `_scope_ok` parses `new_function_body` with the language grammar and
  requires it to be EXACTLY ONE top-level function whose name == the localized
  `function_name`. This rejects: a rename, the fix split across new helper functions, extra
  top-level statements, or an unparseable fragment (`root.has_error`). Because we only ever
  splice over the one function's span, a fix that really belongs in a *caller* can't be
  expressed here anyway — the guard catches the model trying to smuggle it in.
- **Interview framing:** scope safety is enforced structurally (AST shape), not by asking the
  model to behave. A syntactically-valid-but-out-of-scope rewrite (`scope_ok=True` syntax,
  `False` scope) still degrades — proven by `test_scope_guard_rejects_extra_function`.

### F14-3 — Patch failures DEGRADE (code_context_only), they don't ESCALATE
- **Observed:** everywhere else an `LLMCallError` → escalation (SF-4). Here it shouldn't:
  `ast_code_retriever` already produced a good `CodeContext` (the location), so the honest,
  more-useful fallback is "here's where to look", not "we gave up".
- **Design choice:** `generate_patch` runs up to `MAX_ATTEMPTS=2`, catching `LLMCallError` as
  just another failed attempt (with reflexion feedback fed into the retry prompt). Exhausting
  attempts — whether from syntax fails, scope fails, or LLM errors — returns `None`.
  `make_patch_node` ALSO catches a stray `LLMCallError` (e.g. a clone cache-miss failure) and
  maps it to the same `None`/degrade, so this node can never escalate. `route_after_patch`
  is therefore binary: valid+in-scope patch → `human_checkpoint`; anything else →
  `code_context_only`.

### F14-4 — The code-path RemediationPlan carries the patch out-of-band (class=patch, steps=[])
- **Observed:** the catalog's `patch` remediation_class is "produced by the patch generator,
  not a shell command" (SCOPE.md). A `Patch` is not a `CommandIntent`.
- **Design choice:** on success the node sets BOTH `state.patch` (the diff/validation) and
  `state.remediation_plan = RemediationPlan(remediation_class=patch, summary=draft.summary,
  steps=[], references=rca.citations)`. Empty `steps` means the F12-1 catalog re-validation on
  `IncidentState` is a no-op (nothing to check), and no shell command can ride out — the patch
  is applied as a draft PR at execution, gated on approval. `code_context_only` emits the same
  no-command shape but `remediation_class=none` (catalog: "no safe/confident action").

### F14-5 — `route_after_ast` now also requires `function_name`
- A patch-supported language whose fault landed OUTSIDE any function (module-level line; a
  keyword hit at file granularity) has nothing to regenerate. `route_after_ast` was tightened
  to `patch_supported AND function_name` → `patch_generator`, else `code_context_only`. Two
  Task 13 tests were updated for the new branch (the parametrized router case + the e2e AST
  test, which now injects a `patch_fn` stub so it stays a pure AST-wiring test).

**RESULT:** suite **166 → 189 passing** (26s). Real deterministic pipeline verified against
the fixture repo for valid/invalid Python + JS and the scope guard; `generate_patch` verified
for first-try success, second-attempt recovery, double-fail/scope/LLM-error degrade, and the
two skip-preconditions; graph nodes + `route_after_patch` + a full code-bug run producing a
syntax-valid class=patch plan. Env unchanged; NOT committed.

**NEXT:** **HITL `human_checkpoint`** — `interrupt()` at the point all three remediation paths
(infra/config plan, patch, code_context_only) converge; render RCA + plan + citations + diff;
resume on `ApprovalDecision`. Then `execution_layer` (catalog render / draft PR) +
`post_mortem_writer` re-pointing the terminal sinks away from `END`.
