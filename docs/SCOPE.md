# IncidentIQ — Scope: What It Can (and Can't) Diagnose

> **Purpose.** A one-page, honest answer to "what types of incidents can it handle?" — the
> incident-type × remediation-class × scenario matrix, plus the explicit boundaries.
> Derived from [`contracts.py`](../incidentiq/contracts.py) (`IncidentType`),
> [`state.py`](../incidentiq/state.py) (`RemediationClass`), and
> [`AGENT_ORCHESTRATION.md`](AGENT_ORCHESTRATION.md) / [`DESIGN_BRIEF.md`](DESIGN_BRIEF.md).
>
> **One-liner.** *A self-hosted, human-in-the-loop assistant that, for a defined set of incident
> types, produces an evidence-cited diagnosis and a catalog-bounded remediation plan — and
> escalates honestly when it can't.*

---

## The design stance

IncidentIQ does **not** try to diagnose "any" incident. Its thesis is **calibrated honesty**: it
is either confidently right *or* it says `unknown`/escalates — and the calibration task proves the
low-confidence cases really are the wrong ones. "What it handles" therefore means "what it handles
**confidently**"; everything else gets an honest hand-off, not a guess.

Three structural boundaries make this true:
- **Retrieval-grounded** — it reasons only from its corpus (runbooks, postmortems). No relevant
  evidence → weak-retrieval signal → escalate. It does not hallucinate a root cause to fill a gap.
- **Human-in-the-loop** — it drafts an RCA + a remediation *plan*; a person approves at the
  `interrupt()` gate before anything runs. It never autonomously executes.
- **Catalog-bounded action** — the only executable shape is a `{command_id, args}` intent whose
  `command_id` exists in [`catalog/commands.yml`](../catalog/commands.yml). Unsafe actions are
  structurally impossible, not merely discouraged (see `plans/TASK_03_*` F-18).

---

## Axis 1 — Incident types (the triage classification)

The triage vote is deliberately **low-cardinality (4-way)** — it votes on incident *type*, not on
the high-cardinality root service, to keep N=3 self-consistency from collapsing to `unknown`.

| `IncidentType` | What it means | Diagnosis path | Typical `RemediationClass` |
|---|---|---|---|
| **`infra`** | resource/capacity/dependency fault (CPU, memory, saturation, dependency down) | metrics + runbook retrieval | `kubectl` (restart) or `flag_rollback` |
| **`config`** | a config / feature-flag change broke things | deploy-diff + config analysis | `config_revert` |
| **`code_bug`** | a defect localized to a function (has a traceback / fault location) | AST retrieve @ deploy commit → bounded regen | `patch` (regen → diff → validate) |
| **`unknown`** | evidence too weak, or triage rule vs. LLM disagree | terminal sink, **no command** | `none` → escalate |

## Axis 2 — Remediation classes (the only ways it can propose to act)

From `RemediationClass` — every plan resolves to one of these; there is no free-form action.

| Class | Action | Source command(s) in catalog |
|---|---|---|
| `flag_rollback` | disable a feature flag via flagd | `flag_rollback` |
| `kubectl` | restart a deployment | `kubectl_rollout_restart` |
| `config_revert` | revert a config key to its prior deploy value | `config_revert` |
| `patch` | apply a syntax-validated unified diff (draft PR) | *(code path — produced by the patch generator, not a shell command)* |
| `none` | no safe/confident action — escalate or `code_context_only` | — |

## Axis 3 — Concrete demo scenarios (OTel Astronomy Shop fault injections)

Built and graded against the OTel demo's feature-flag faults. **6 supported under Docker Compose**,
each run end-to-end: flag → alert → investigation → plan → approval → post-mortem.

| Scenario (representative) | Type | Path | Notes |
|---|---|---|---|
| `adServiceHighCpu` | `infra` | runbook → plan | named in TASKS.md as the infra exemplar |
| service-failure flags (ad / cart / product-catalog) | `infra`/`config` | → `flag_rollback` | the flag-rollback exemplars |
| recommendation **cache-failure** | `infra` | memory-limit runbook | needs mem-limit tuning |
| `failedReadinessProbe` | `config` | config-revert | **K8s-only (MF-4)** — see below |

> **The exact 6-scenario roster is not locked yet.** It is finalized in the
> `services.yml + flagd triggering` task (Foundation is currently 3/4 — not built). What *is*
> fixed: the incident **types** and **remediation classes** above. The scenario list is still being
> reconciled against real, discovered Prometheus metric names.

---

## Explicitly out of scope (and why)

| Out of scope | Reason |
|---|---|
| Arbitrary production infra | Built/evaluated against the OTel Astronomy Shop demo only. |
| `failedReadinessProbe` under Docker Compose | Depends on `kube_pod_container_status_ready` (kube-state-metrics), **absent in Compose**. Runs only under a separate **K8s demo mode** (MF-4); a startup metric-source check marks it unavailable rather than crashing. |
| Autonomous remediation | All actions gate on `ApprovalDecision.approved` (FR-16). It plans; a human approves. |
| C# / Rust code patches | Patch generator returns `CodeContext` + TODO for these; only Py/JS/Go produce applying diffs (decision #3). |
| Multi-function / caller-spanning bugs | `scope_ok=False` → `code_context_only` — no misleading single-function patch (SF-5). |
| Concurrent / horizontally-scaled incident load | Single global Ollama semaphore; incidents are serial; self-hosted on a 16 GB box. |
| Novel incidents with no corpus evidence | Weak-retrieval signal → `unknown`/escalate by design. |

## How "I don't know" is decided (the honesty machinery)

- **Triage disagreement** — deterministic rule prior vs. LLM confirmation disagree → `unknown`
  (decision #4).
- **Confidence gates** — composite confidence `< 0.65` → escalate; triage `< 0.70` → `unknown`.
  *(These thresholds are **placeholders** until the calibration task sets them from the reliability
  curve — MF-1; never hardcoded as final.)*
- **Weak retrieval** — too few chunks clear the min-score threshold → confidence penalty → escalate.
- **Self-consistency** — N=3 RCA samples disagree on root cause → low agreement → composite
  confidence drops → `unknown`.

Every one of these routes to a **single central escalation / unknown terminal node that emits zero
commands** (decision #10) — so a low-confidence incident is safe by construction, not by luck.
