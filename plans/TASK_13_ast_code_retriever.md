# Task 13 — AST code retriever

**Goal:** Replace the `ast_code_retriever` placeholder (`incidentiq/graph/build.py`, Gate B's
third arm) with a real node that localizes a code-bug fault to a function: cache-aware shallow
clone @ deploy commit → tree-sitter parse → offending function + callers → `CodeContext`.

**Done when:** fault localization recall >75% top-3 (against fixture repo cases); cache hit
<5s; missing tree-sitter grammar warns, doesn't crash (FR-27). Traceback present → parse
file/line (FR-13, `via="traceback"`); traceback absent → keyword fallback on
`probable_cause`/`summary` (FR-13, `via="keyword_fallback"`). Clone/index failure → typed
error (`clone_timeout`) → escalation. `patch_supported` True only for python/javascript/go
(C#/Rust localize only — `patch_generator` in Task 14 decides diff vs `code_context_only`,
so this task just sets the flag correctly and stops at `CodeContext`).

Spec: `docs/AGENT_ORCHESTRATION.md` §1 (AST node), Flow A steps 6-7, Failure Topology
(missing traceback / clone timeout rows); `docs/CONTRACTS.md` §2.5 `CodeContext`;
`docs/TASKS.md:41`. Builds on Task 10 gates (`route_after_triage` already sends
`code_bug` → `ast_code_retriever`), Task 2 contract (`CodeContext` already defined,
untouched).

**Scope decision (pre-build, 2026-07-01):** the services.yml task (real demo-service →
repo/commit mapping) hasn't landed, so there's no locked real repo/commit to build against yet.
Decided: build fully generic capability against a **local fixture git repo**
(`tests/fixtures/sample_repo/`, real commits, one file per supported language) instead of a
live network clone in tests — matches the injectable-dependency pattern used by every other
node (`RetrieveFn`/`SynthesizeFn`/`RemediationFn` + fakes). Clone mechanism: shell out to the
`git` CLI (`subprocess`) for shallow clone-at-commit — no new heavy dependency, matches the
docs' "shallow clone" language, trivially fakeable via an injectable `clone_fn`.

---

## Findings & Decisions Log

### F13-1 — Exact-commit shallow clone needs a specific recipe, not `git clone --depth 1`
- **Observed:** `git clone --depth 1 <url>` only fetches the tip of a branch — it cannot
  target an arbitrary historical `deploy_commit`. The standard workaround (verified against
  both a local two-commit repo and the fixture repo): `git init dest && git -C dest remote
  add origin <url> && git -C dest fetch --depth 1 origin <commit> && git -C dest checkout -q
  FETCH_HEAD`. This works even over local (`file://`-less path) transport, which is what
  makes testing against a real repo without network access possible.
- **Design choice:** `incidentiq/retrieval/code_clone.py` implements exactly this recipe,
  shelling out via `subprocess` (per the pre-build decision to avoid a GitPython dependency).
  Cache key = `sha256(repo_url + "@" + commit)[:16]`, so the same commit from two different
  repos never collides. A cache "hit" is a pure path check (`(dest/".git").exists()`) — no
  subprocess at all, confirmed <1ms in testing (budget was <5s, FR-26).
- **Failure handling:** any `CalledProcessError`/`TimeoutExpired`/`OSError` during clone
  `shutil.rmtree`s the partial `dest` before re-raising as `TypedError(kind="clone_timeout")`
  — a half-cloned dir must never look like a cache hit on the next attempt.

### F13-2 — One location algorithm for five languages, via `child_by_field_name("name")`
- **Observed:** every tree-sitter grammar (python/javascript/go/csharp/rust) exposes the
  identifier of a function-like node through the SAME field name, `"name"` — verified by
  parsing real snippets in each language and pulling `child_by_field_name("name")`.
- **What it means:** `function_locator.py` needs exactly one per-language table (function
  *node types* — `function_definition`, `function_declaration`/`method_definition`, etc.)
  and zero per-language name-extraction branches. `keyword_locator.py` reuses the same table
  (`FUNCTION_NODE_TYPES`, exported non-underscored) rather than duplicating it.
- **Smallest-enclosing-function correctness:** `_enclosing_function` does a stack-based
  (non-recursive) DFS and overwrites `best` on every function-type node whose span contains
  the target row. This is safe regardless of traversal/sibling order: a node only becomes a
  candidate if its span contains the row, and the parent-before-child structure of a stack
  DFS guarantees an ancestor is always processed before its own descendants — so among
  candidates, the last one written is always the most deeply nested, i.e. innermost.

### F13-3 — Traceback fallback extended one step further than the doc's literal wording
- **Observed:** AGENT_ORCHESTRATION's failure topology only documents "missing traceback →
  keyword fallback" (FR-13). It doesn't say what to do when a traceback IS present and
  parses cleanly, but the file it names isn't in the tree at `deploy_commit` (a rename, or a
  stale/mismatched trace).
- **Design choice:** `ast_code_retriever.py` falls through to keyword search in that case too,
  rather than treating it as a hard failure. Treated as a resilience extension of FR-13's
  stated intent ("continues" on incomplete traceback signal), not a contract change.

### F13-4 — `patch_supported` is a pure language flag, not a "function was found" flag
- **Observed:** the contract's comment (`state.py`) says `patch_supported: bool  # True only
  for py/js/go`. It says nothing about whether a function was actually localized.
- **Design choice:** `_from_located` sets `patch_supported = language in {...}` unconditionally
  — even when `function_name` ends up `None` (e.g. the traceback line sits at module level,
  outside any function). Task 14's `patch_generator` is the one that must guard on
  `function_name is not None` before attempting a regen; conflating the two flags here would
  hide a real "nothing to patch" case behind a language-support flag.
- **UPDATE (Task 14, 2026-07-02):** the supported set was narrowed from `{python, javascript,
  go}` → `{python, javascript}` (Go joined C#/Rust in `code_context_only`), because only py/js
  have a zero-install native syntax validator on this box. `route_after_ast` was also tightened
  to require `function_name` for the patch arm. See `plans/TASK_14_patch_generator.md` (F14-5).

### F13-5 — Error-kind split: `"other"` (bad metadata) vs `"clone_timeout"` (bad clone)
- **Design choice:** a missing `repo_url`/`deploy_commit` on `IncidentContext` raises
  `TypedError(kind="other")` — it never reached git. Only an actual failed/timed-out `git`
  subprocess raises `kind="clone_timeout"`, matching the Failure Topology table's specific
  "Repo clone timeout → ast_code_retriever typed error → escalation" row.

### F13-6 — Test fixture: real git repo built at test time, not vendored
- Per the pre-build scope decision (top of this file): `tests/fixtures/git_repo.py` builds a
  REAL two-commit git repo in a pytest `tmp_path` (not a vendored nested `.git`, which would
  fight the parent repo's own git tooling). `v1` = clean `get_user_balance`; `v2` = a
  divide-by-zero bug in `service.py` + mirror files in JS/Go/C#/Rust (same shape, same
  variable names) + one Ruby file to exercise the "unsupported language" branch.
- **Known limitation surfaced by this symmetry:** because the mirror files share identical
  function/variable names across languages, `keyword_locator`'s simple substring-count
  scoring can tie across files (e.g. "balance"/"pending" appear identically in all 5). Tests
  that need a deterministic winner use a keyword unique to one file (`"handle_request"`,
  which only exists in `main.py`). A real repo won't have five near-identical mirrors, so
  this is a fixture artifact, not a retrieval bug — noted rather than "fixed" by adding
  smarter ranking that the current scope doesn't need.

**RESULT:** `ast_code_retriever` replaces the `END` placeholder at Gate B's third arm
(`incidentiq/graph/build.py`). New `route_after_ast` (routing.py): no/failed localization →
`escalation_node`; else → `patch_generator`/`code_context_only` (both still `END`
placeholders — real nodes land in Task 14). Suite **132 → 166 tests** (34 new, all passing).
Verified end-to-end against the real fixture repo for traceback localization in all 5
languages, keyword fallback, caller detection, cache hit/miss, and clone failure. Cache-hit
latency measured <1ms (budget: <5s). `docs/TASKS.md` line 41 checkbox flipped.

**NEXT:** **Patch generator (Task 14)** — regen the localized function body (py/js/go only,
gated on `CodeContext.patch_supported` AND `function_name is not None`) → deterministic
unified diff → syntax validate → `code_context_only` on 2x failure or out-of-scope edit
(SF-5). Also still open from Task 12: HITL `interrupt()` at `human_checkpoint`, and
`post_mortem_writer` re-pointing the terminal sinks away from `END`.
