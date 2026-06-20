# Task 3 — Safe Command Catalog + Deterministic Renderer

> **Status:** ✅ COMPLETE (2026-06-19) — all sub-steps done; 22 catalog/render/injection tests, 44 total pass
> **Source:** Third Foundation task in [`../docs/TASKS.md`](../docs/TASKS.md)
> **Implements:** [`../docs/CONTRACTS.md`](../docs/CONTRACTS.md) §4 (Safe Command Catalog)
> **Builds on:** Task 2's `CommandIntent` validator + `validate_command_args`
>   in [`../incidentiq/state.py`](../incidentiq/state.py) (the contract side; this task adds
>   the YAML catalog + the deterministic renderer that reuse them).

---

## Goal

Make unsafe actions **structurally impossible** (FR-12/36, decision #11). The LLM only ever
emits `{command_id, args}`. A deterministic, no-LLM renderer:
1. loads `catalog/commands.yml` (the single source of truth),
2. re-validates the intent against the catalog (defense in depth — already validated once at
   the `CommandIntent` contract boundary in Task 2),
3. enforces `allowed_namespaces`,
4. fills the template by **safe substitution** (no shell interpolation of unvalidated values),
5. surfaces `approval_required` so execution stays gated.

If a `command_id` isn't in the catalog, no string can ever be produced for it. That is the
backstop the CI-4 injection corpus will later prove.

## Exit Criteria (Done when)

- [x] `catalog/commands.yml` exists with the three §4 commands (flag_rollback,
      kubectl_rollout_restart, config_revert).
- [x] A loader parses the YAML into the exact dict shape `CommandIntent`'s validator +
      `validate_command_args` already consume (one catalog, two enforcement points).
- [x] The real loaded catalog round-trips Task 2's `CommandIntent` validator (closes the loop).
- [x] `render_command(...) -> RenderedCommand` fills the template by safe substitution.
- [x] A non-catalog `command_id` is **impossible to render** (raises `CatalogError`, never a string).
- [x] `allowed_namespaces` violations rejected.
- [x] Unsafe-action test = 0%: 9-entry injection corpus, each rejected at BOTH the renderer and
      the `CommandIntent` contract boundary (18 parametrized assertions, all pass).

## Sub-steps

- [x] **3a** — `catalog/commands.yml` (data) + loader producing the validator-shaped dict;
      test that the real catalog drives Task 2's `CommandIntent` validator. ✅ (2 tests pass)
- [x] **3b** — 🎯 deterministic `render_command()`: re-validate + namespace enforcement + safe
      substitution + approval signal; catalog tightened with patterns. ✅ (4 tests pass)
- [x] **3c** — safety tests: non-catalog id, namespace violation, injection corpus (unsafe = 0%),
      asserted at both enforcement points. ✅ (44 total tests pass)

---

## Findings & Decisions Log

> Format per entry: **observed → what it means → design choice → interview framing.**
> Continues the F-/D- numbering from Task 2 (last was F-16, D-3).

**F-17 — `yaml.safe_load` is a security control here, not a style choice.**
- *Observed:* the loader uses `yaml.safe_load`; `yaml.load` would also work.
- *Means:* `yaml.load` honours tags like `!!python/object/apply:os.system` and can construct
  arbitrary Python objects → a known PyYAML RCE class. Using the *unsafe* loader on the one file
  whose entire job is to define "what is safe to execute" would plant a code-execution sink inside
  the safety backstop.
- *Choice:* `safe_load` only (plain scalars/lists/dicts). The renderer is deterministic and
  no-LLM; the loader must be deterministic-and-no-arbitrary-code too.
- *Interview framing:* "The catalog is the trust root for execution, so it gets loaded with the
  loader that can't execute. `safe_load` vs `load` is the difference between parsing data and
  potentially running it."

**D-4 — The catalog stays a raw `dict`, deliberately un-modelled.**
- *Observed:* every other contract in `state.py` is a Pydantic model; the catalog is left as the
  plain dict `load_catalog` returns.
- *Means:* the catalog is not data flowing *through* the system — it's the *schema* that
  `validate_command_args` interprets at runtime (`spec["args"][name]["type"]/["enum"]/["pattern"]`).
  Forcing it into a fixed model would mean enumerating every possible arg-constraint key up front.
- *Choice:* keep it declaratively open so later keys (`enum_from`, future constraints) need no
  code change; the validation that matters runs on the *intents*, not on the catalog itself.
  `enum_from: services.yml#flags` was dropped from `flag_key` for now — it resolves only once
  `services.yml` exists (later task); until then `flag_key` is a plain `string`.
- *Interview framing:* "I validate the messages, not the rulebook. The catalog is configuration
  the validator reads, so it stays a dict; the Pydantic models guard the things the model emits."

**F-18 — Two safety layers, two attack classes; neither is sufficient alone (the headline).**
- *Observed:* the system now enforces both `command_id ∈ catalog` (Task 2) and per-arg
  `enum`/`pattern` (catalog), and `flag_key`/`flagd_url`/`deployment` were given allowlist
  patterns in 3b.
- *Means:* **Layer 1 (command_id allowlist)** stops *fabricated verbs* — `delete_everything`,
  `rm -rf`, anything the model invents — but alone lets a payload ride inside an allowed command's
  unconstrained string arg (`flag_key="x; rm -rf / #"` is a *valid* `flag_rollback`). **Layer 2
  (per-arg allowlist)** stops *malicious operands* — shell metachars, traversal, wrong namespace —
  but alone validates args for a command that should never exist. Together: the model chooses
  only from fixed verbs AND fills each slot only with allowlisted operands; no half is free-form,
  so there is no free-text path from model output to a command string.
- *Choice:* enforce both, at two code points (CommandIntent validator + renderer). `type: string`
  is necessary but **not sufficient** for any value that reaches a template — those args get a
  `pattern`/`enum`. Third leg (deferred to the execution task): the rendered string is an *audit*
  artifact; execution will use argv arrays, never `shell=True`.
- *Interview framing:* "Unsafe-action = 0% isn't one check, it's two allowlists guarding two
  different attack classes — fabricated commands and injected operands — plus a no-shell execution
  contract. Drop either allowlist and I can show you the exploit that gets through."

**F-19 — `str.format_map` on a *trusted* template is safe substitution; the danger is a template
you don't control.**
- *Observed:* `render_command` fills the template with `spec["template"].format_map(effective)`;
  the flag_rollback template carries doubled braces `{{"state":"DISABLED"}}`.
- *Means:* the classic `str.format` exploit (`{0.__class__.__init__.__globals__...}`) needs an
  attacker-controlled *format string*. Here the template comes from the catalog (trusted) and the
  *values* are attacker-influenced but inserted **literally** — `format` does not recursively
  re-expand a substituted value, so `flag_key="{flagd_url}"` renders as literal text, not a
  re-substitution. Doubled braces collapse to literal JSON braces while real placeholders fill.
- *Choice:* `format_map(validated_args)` over a trusted template; values are already
  type/enum/pattern-validated, so substitution can't introduce a metacharacter the pattern
  disallows. KeyError (catalog-authoring bug) is caught → `CatalogError`.
- *Interview framing:* "Format-string injection is a property of who controls the format string,
  not the values. My templates are configuration, not model output, so substitution is safe — and
  the values were allowlisted before they ever reached the template."

**F-20 — The injection corpus maps 1:1 to the two layers — that *is* the "need both" proof.**
- *Observed:* the 9-entry corpus is rejected at both the renderer and the contract boundary.
  Splitting by *which* layer fires: **only Layer 1** (command_id) catches `delete_everything`
  and `rm` — no catalog spec exists, so the arg checks never run. **Only Layer 2** (arg
  pattern/enum/unknown-arg) catches `flag_key="x; rm -rf / #"`, `namespace="kube-system"`,
  `commit="HEAD; rm -rf /"`, and the smuggled `evil` arg — those commands *are* in the catalog,
  so Layer 1 waves them through.
- *Means:* the two layers aren't redundant; each is the *sole* defense for a disjoint class of
  attack. Remove Layer 1 → invented verbs render; remove Layer 2 → legit verbs carry injected
  operands.
- *Choice:* keep the corpus as a living regression artifact (later promoted to the named
  `eval/injection_corpus/` for CI-4); assert at BOTH points so neither enforcement site can
  silently rot.
- *Interview framing:* "I can point at the corpus and say which line each layer is responsible
  for. That's how I know 0% unsafe-action is a structural property, not a lucky test pass."

---

## Worktree note

Built directly on `main` in `~/Desktop/IncidentIQ`. Source of truth = main repo.
