# Task 5 — Ingestion slice (`POST /api/v1/incidents`)

> **Vertical slice goal (TASKS.md):** the FastAPI entry point. AlertManager v4 / PagerDuty
> Pydantic union → **202 in <200 ms** + background task, raw-payload persistence (FR-01/02/04),
> fingerprint dedup (FR-24). _Done when: paste a payload → record in Postgres <1s, 202 before any
> LLM call._
>
> Depends on: contract layer (Task 2). Foundation 4/4 is complete.

## Scope decisions (what this slice does and does NOT do)

- **D-8 — the receiver does not build `IncidentContext`.** Per `AGENT_ORCHESTRATION.md` §3 state
  table, the webhook receiver sets only `incident_id`, `status`, `raw_payload`,
  `alertmanager_fingerprint`. `incident_context` is set by the **enricher** (next task). So this
  slice parses the *envelope*, fingerprints, persists raw, returns 202 — **no LLM, no graph yet**.
- **D-9 — new contract: the inbound webhook union.** `contracts.py`/`state.py` model the *internal*
  state, not the *external* webhook bodies. This task introduces the AlertManager v4 + PagerDuty
  inbound payload models (a discriminated union) — a separate, untrusted-input contract.
- **D-10 — package location: `incidentiq/api/`** (per D-7's deferred plan). Owns its DDL
  (`api/schema.sql` → `incidents` table) the same way `retrieval/` owns the `chunks` table.

## Sub-steps

1. Inbound webhook contract — AlertManager v4 + PagerDuty discriminated union.
2. Fingerprint + dedup (FR-24): payload fingerprint, fallback `hash(alertname, service, ns, startsAt@min)`.
3. `incidents` table DDL + persistence helpers.
4. FastAPI app: `POST /api/v1/incidents` → parse → dedup → persist → 202 + BackgroundTask.
5. Tests: valid AM, valid PD, dedup→200, RESOLVED, 202 shape, raw persisted.

## Result — COMPLETE (2026-06-22)

Files shipped: `incidentiq/api/{__init__,webhooks,fingerprint,store,app}.py` + `api/schema.sql`;
`tests/test_ingestion.py` (13 tests: 8 DB-free parse/normalize/fingerprint + 5 DB-touching endpoint,
skip cleanly without Postgres). **Full suite = 62 passing** (was 49). Deps added: `fastapi`,
`uvicorn[standard]`. Endpoint verified via TestClient: new→202, dup→200, malformed→422, resolved→ack.
`run_investigation` is the stub seam for the LangGraph task. Next: **Alert enricher** (builds
`IncidentContext` — owner/repo/deploys/`deploy_gap_minutes`/`traceback`), then Hybrid retriever.

---

## Findings & Decisions Log

_(observed → what it means → design choice → interview framing)_

### F-29 — Callable discriminator over plain union for the webhook boundary

- **Observed:** AlertManager v4 and PagerDuty v3 share no common tag field (AM has `version:"4"`
  + `alerts[]`; PD has `event{}`). A plain `AlertManagerV4 | PagerDutyV3` union would try members
  in order and, on failure, merge errors from both branches — and could silently coerce a malformed
  AM payload into the PD branch.
- **What it means:** at an *untrusted* input boundary, ambiguous parsing is a safety bug, not just a
  DX annoyance.
- **Design choice:** `Discriminator(_provider)` callable inspects the raw dict, picks the branch by
  the distinguishing field, validates only that branch → clean errors, no silent mis-parse. Each model
  exposes `normalized() -> NormalizedAlert` so the rest of the system never branches on provider.
- **Interview framing:** "I made the provider explicit at the parse boundary instead of relying on
  union-fallback ordering — the trust boundary fails loud in the right branch."
- **Verified:** both AM and PD sample payloads parse and normalize correctly (REPL check, 2026-06-22).

### F-30 — Two-tier fingerprint; fallback is coarse on the noisy field

- **Observed:** at-least-once webhook delivery + AlertManager retries mean the *same* logical alert
  arrives multiple times with `startsAt` wobbling by seconds.
- **Design choice:** `compute_fingerprint()` — tier 1 trusts the provider fingerprint (AM label-set
  hash / PD incident id); tier 2 hashes `(alertname, service, namespace, startsAt@minute)`. Truncating
  to the minute absorbs redelivery jitter so duplicates collapse to one key. `fb_` prefix marks
  fallback keys (provenance in logs). Pure function → DB-free, unit-testable.
- **Interview framing:** "The dedup key is deliberately coarse on the one noisy field — full-precision
  timestamps would make every redelivery look like a new incident and defeat dedup."
- **Verified:** two same-minute fallbacks produce identical `fb_...`; next-minute differs; provider fp
  passes through (REPL check, 2026-06-22).

### F-31 — Dedup enforced by a PARTIAL UNIQUE INDEX, not check-then-insert

- **Observed:** the 202 path is concurrent (FastAPI handles webhooks in parallel before the serial
  graph). A SELECT-then-INSERT dedup has a TOCTOU race → two duplicate deliveries → two incidents.
- **Design choice:** partial unique index `uq_incidents_open_fingerprint ON incidents(fingerprint)
  WHERE status IN (active)` + `INSERT ... ON CONFLICT (fingerprint) WHERE ... DO NOTHING RETURNING`.
  The DB enforces "one LIVE incident per fingerprint" atomically. Partial scope = a new firing after
  the prior incident reaches a terminal status is allowed (terminal rows excluded from the index).
  `insert_incident()` returns `(incident_id, is_new)`; on conflict it returns the existing live id.
- **Interview framing:** "The uniqueness invariant is 'one live incident per fingerprint,' and a
  partial index expresses exactly that — race-free, in the database, not in app code."
- **Verified:** first insert `('i-1', True)`, duplicate `('i-1', False)` (same id, not new). 2026-06-22.

### F-32 — psycopg3 does NOT expand a tuple into SQL `IN (...)`

- **Observed:** `WHERE status IN %s` with a tuple param → `SyntaxError: syntax error at or near "$2"`.
- **Design choice:** use `status = ANY(%s)` with a Python **list** — the psycopg3 idiom for set
  membership. (psycopg3 dropped the implicit `IN %s` tuple-expansion that psycopg2 did.)
- **Interview framing:** small but real driver-portability gotcha worth knowing on a psycopg2→3 migration.

### F-33 — A `Discriminator` callable must RETURN a non-matching tag, not raise

- **Observed:** when `_provider` raised a bare `ValueError` on an unrecognized payload, Pydantic let
  it propagate as-is — it never became a `ValidationError`, so the endpoint's `except ValidationError`
  missed it and it would surface as a **500** instead of a 422.
- **Design choice:** the callable returns `"unknown"` (a tag matching no `Tag`), which makes Pydantic
  itself raise a proper `ValidationError` (`union_tag_invalid`) → caught → 422.
- **Interview framing:** "Discriminator callables signal failure by returning an unmatched tag, not by
  throwing — so malformed input becomes a clean validation error at the boundary, not a server crash."
- **Verified:** new 202, dup 200, bad 422, resolved → `resolved_ack` (TestClient, 2026-06-22).

### D-11 — Endpoint scope decisions (status codes + deferrals)

- 202 `{incident_id, task_id}` on new; 200 `{incident_id, duplicate:true}` on dup; 422 on malformed;
  200 `{status:resolved_ack}` on RESOLVED.
- Body taken as raw `dict` (not `payload: WebhookPayload`) to persist the **exact** raw payload (FR-04);
  validated manually via `TypeAdapter`.
- `run_investigation` is a **stub** — the seam where the LangGraph task plugs in (`graph.ainvoke`).
- RESOLVED full handling (close + post-mortem, FR-29) deliberately deferred to its own task.
