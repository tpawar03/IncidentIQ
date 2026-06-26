# Task 6 — Alert Enricher (Core pipeline #2)

**Goal:** Turn a `NormalizedAlert` + the raw persisted payload into a fully-populated,
*trusted* `IncidentContext` for the graph. Extract `summary`, `severity`, `traceback`,
`affected_endpoint`, `repo_url`, `deploy_commit` from the raw labels/annotations the
normalizer dropped; graceful nulls everywhere; deploy fields stubbed.

**Shipped:**
- `incidentiq/api/enricher.py` — pure `enrich(alert, raw) -> IncidentContext` + `_labels_annotations(raw)`.
- `incidentiq/api/store.py` — added `get_raw_payload(incident_id) -> dict | None`.
- `incidentiq/api/app.py` — `run_investigation` now loads → re-parses → normalizes → enriches.
- `tests/test_enricher.py` — 5 DB-free tests. Suite **62 → 67**.

---

## Findings & Decisions

### F-34 — The enricher is the trust boundary, not a method on the model

- **Observed:** `IncidentContext` needs richer fields (`summary`, `severity`, `traceback`, …) that
  `NormalizedAlert` deliberately omits — they live in the raw payload's labels/annotations.
- **Design choice:** a standalone `enrich()` in `api/` (next to `webhooks.py`), not a
  `NormalizedAlert.to_context()` method. The enricher is the *last station on the untrusted side*:
  it reads raw external data and emits the first object the rest of the system trusts. Keeping it a
  free function keeps `NormalizedAlert` a thin transport view and concentrates the trust transition
  in one place.
- **Interview framing:** "Normalization gives a provider-agnostic *shape*; enrichment crosses the
  *trust boundary*. Separating them means everything downstream of `IncidentContext` can assume
  validated, system-owned data."

### D-12 — Enrich in the background task, not the request handler

- **Observed:** `enrich()` needs the raw payload; it could run in the 202 handler or in
  `run_investigation`.
- **Design choice:** run it in `run_investigation`. The receiver's contract (D-8) is persist + dedup
  only, on a <200 ms budget; enrichment is the *first step of the investigation*. Even though
  `enrich()` is microsecond dict-walking, keeping it off the request path preserves the clean
  scope split and the latency budget.
- **Interview framing:** "Ingestion is acknowledgement; investigation is work. Enrichment is work."

### F-35 — Background task rehydrates inputs from the DB, not from memory

- **Observed:** `run_investigation(incident_id)` receives only an id. It could instead be handed the
  in-memory `alert`/`payload`.
- **Design choice:** added `get_raw_payload()`; the task reloads the persisted payload, re-parses via
  the same `TypeAdapter`, normalizes, then enriches. The incident row is the source of truth. This
  mirrors how the LangGraph Postgres checkpointer rehydrates state and how a real durable queue
  (SQS/Celery) would deliver only an id — making the task crash-resumable.
- **Interview framing:** "Pass identifiers, not objects, across an async boundary — so the worker can
  recover from the durable record after a restart instead of depending on lost process memory."

### F-36 — Graceful nulls + summary fallback (FR-24 / D-11 continuity)

- **Observed:** `IncidentContext.summary` is *required*; most other context fields are optional.
- **Design choice:** every optional field defaults to `None` when its annotation is absent
  (`traceback=None` → retriever uses keyword fallback later). `summary` falls back to `alert_name`
  so the required field is never empty. PagerDuty (no `alerts[]` array) yields empty
  labels/annotations from `_labels_annotations`, so the enricher cleanly falls back to the
  `NormalizedAlert` scalars instead of branching on provider.
- **Interview framing:** "The enricher never raises on missing optional data — partial signal is
  still useful signal; only the hard-required `summary` has a guaranteed fallback."

### Deferred (out of scope, by design)

- `last_deploys` / `deploy_gap_minutes` are **stubbed** (`[]` / `None`) — they need a real deploy
  tracking source (a later task). The triage prior that consumes `deploy_gap_minutes` already
  tolerates `None`.
- RESOLVED handling and the LangGraph `graph.ainvoke` call remain the next seams (TODO in `app.py`).
