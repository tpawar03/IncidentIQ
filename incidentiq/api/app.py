"""FastAPI ingestion entry point. POST /api/v1/incidents → parse + persist + 202 (no LLM).

The receiver does fast deterministic work only (D-8): parse the webhook union, fingerprint,
dedup-insert, schedule the background investigation. No LLM on this path — latency budget
<200 ms (AGENT_ORCHESTRATION §9).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError

from incidentiq.api.enricher import enrich
from incidentiq.api.fingerprint import compute_fingerprint
from incidentiq.api.store import apply_schema, get_raw_payload, insert_incident
from incidentiq.api.webhooks import AlertManagerV4, WebhookPayload

_webhook = TypeAdapter(WebhookPayload)


@asynccontextmanager
async def lifespan(app: FastAPI):
    apply_schema()          # idempotent; ensure the incidents table exists at boot
    yield


app = FastAPI(title="IncidentIQ", version="0.1.0", lifespan=lifespan)


async def run_investigation(incident_id: str) -> None:
    """Background entry point for the agent graph. Loads the persisted payload,
    rebuilds the trusted IncidentContext, then (later) drives the graph.

    STUB at the graph boundary until the LangGraph task lands — at which point the
    enriched context becomes part of the initial IncidentState for graph.ainvoke(...).
    """
    raw = get_raw_payload(incident_id)
    if raw is None:                       # incident vanished (e.g. resolved) → nothing to do
        return

    alert = _webhook.validate_python(raw).normalized()
    context = enrich(alert, raw)
    # TODO(graph task): build IncidentState(incident_context=context, ...), call graph.ainvoke(...).
    _ = context


@app.post("/api/v1/incidents")
async def ingest(payload: dict, background: BackgroundTasks) -> JSONResponse:
    try:
        model = _webhook.validate_python(payload)
    except ValidationError as e:
        return JSONResponse(status_code=422, content={"detail": e.errors(include_url=False)})

    provider = "alertmanager" if isinstance(model, AlertManagerV4) else "pagerduty"
    alert = model.normalized()

    if not alert.is_firing:
        # RESOLVED: full close + post-mortem is FR-29 (later task). Ack without creating work.
        return JSONResponse(status_code=200, content={"status": "resolved_ack"})

    fingerprint = compute_fingerprint(alert)
    incident_id, is_new = insert_incident(
        incident_id=f"inc_{uuid.uuid4().hex[:12]}", fingerprint=fingerprint, provider=provider,
        alertname=alert.alertname, service=alert.service, namespace=alert.namespace,
        starts_at=alert.starts_at, raw_payload=payload,
    )

    if not is_new:
        return JSONResponse(status_code=200, content={"incident_id": incident_id, "duplicate": True})

    task_id = uuid.uuid4().hex
    background.add_task(run_investigation, incident_id)
    return JSONResponse(status_code=202, content={"incident_id": incident_id, "task_id": task_id})