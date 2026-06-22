"""Task 5 ingestion tests.

Split by dependency (mirrors the corpus suite's DB-free stance): webhook parsing,
normalization, and fingerprinting are pure logic and always run. The endpoint tests
(dedup, status codes) need Postgres, so they skip cleanly when Docker isn't up.
"""
from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from incidentiq.api.fingerprint import compute_fingerprint
from incidentiq.api.webhooks import (
    AlertManagerV4, NormalizedAlert, PagerDutyV3, WebhookPayload,
)

_webhook = TypeAdapter(WebhookPayload)

AM = {"version": "4", "status": "firing", "alerts": [
    {"status": "firing",
     "labels": {"alertname": "adHighCpu", "service": "ad", "namespace": "otel"},
     "annotations": {"summary": "cpu high"},
     "startsAt": "2026-06-22T10:00:00Z", "fingerprint": "abc123"}]}

PD = {"event": {"event_type": "incident.triggered", "occurred_at": "2026-06-22T10:00:00Z",
                "data": {"id": "PD-9", "title": "cart latency", "service": {"summary": "cart"}}}}


# --- DB-free: parsing + normalization ---------------------------------------

def test_alertmanager_parses_to_correct_branch():
    model = _webhook.validate_python(AM)
    assert isinstance(model, AlertManagerV4)
    n = model.normalized()
    assert n.is_firing and n.alertname == "adHighCpu" and n.service == "ad"


def test_pagerduty_parses_to_correct_branch():
    model = _webhook.validate_python(PD)
    assert isinstance(model, PagerDutyV3)
    n = model.normalized()
    assert n.is_firing and n.alertname == "cart latency" and n.fingerprint == "PD-9"


def test_unrecognized_payload_rejected():
    with pytest.raises(ValidationError):
        _webhook.validate_python({"foo": "bar"})


def test_alertmanager_requires_at_least_one_alert():
    with pytest.raises(ValidationError):
        _webhook.validate_python({"version": "4", "status": "firing", "alerts": []})


def test_resolved_status_normalizes_to_not_firing():
    pd = {**PD, "event": {**PD["event"], "event_type": "incident.resolved"}}
    assert _webhook.validate_python(pd).normalized().is_firing is False


# --- DB-free: fingerprint ----------------------------------------------------

def _alert(sec, fp=None):
    return NormalizedAlert(is_firing=True, alertname="adHighCpu", service="ad", namespace="otel",
                           starts_at=datetime(2026, 6, 22, 10, 0, sec, tzinfo=timezone.utc),
                           fingerprint=fp)


def test_provider_fingerprint_is_trusted():
    assert compute_fingerprint(_alert(0, "abc123")) == "abc123"


def test_fallback_fingerprint_is_minute_stable():
    assert compute_fingerprint(_alert(5)) == compute_fingerprint(_alert(55))   # same minute
    assert compute_fingerprint(_alert(5)).startswith("fb_")


def test_fallback_fingerprint_differs_across_minutes():
    later = _alert(0).model_copy(update={"starts_at": datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc)})
    assert compute_fingerprint(_alert(0)) != compute_fingerprint(later)


# --- DB-touching: the endpoint (skips cleanly without Postgres) --------------

@pytest.fixture
def client():
    from incidentiq.db import connect
    from incidentiq.api.store import apply_schema
    try:
        apply_schema()
        with connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE incidents")
            conn.commit()
    except Exception:
        pytest.skip("Postgres not available (docker compose up)")
    from fastapi.testclient import TestClient
    from incidentiq.api.app import app
    with TestClient(app) as c:
        yield c


def test_new_incident_returns_202_with_ids(client):
    r = client.post("/api/v1/incidents", json=AM)
    assert r.status_code == 202
    body = r.json()
    assert body["incident_id"].startswith("inc_") and "task_id" in body


def test_duplicate_returns_200_no_new_incident(client):
    first = client.post("/api/v1/incidents", json=AM).json()
    r = client.post("/api/v1/incidents", json=AM)
    assert r.status_code == 200
    assert r.json() == {"incident_id": first["incident_id"], "duplicate": True}


def test_raw_payload_is_persisted(client):
    iid = client.post("/api/v1/incidents", json=AM).json()["incident_id"]
    from incidentiq.db import connect
    with connect() as c, c.cursor() as cur:
        cur.execute("SELECT raw_payload FROM incidents WHERE incident_id = %s", (iid,))
        stored = cur.fetchone()[0]
    assert stored == AM   # FR-04: exact payload round-trips


def test_malformed_payload_returns_422(client):
    assert client.post("/api/v1/incidents", json={"foo": "bar"}).status_code == 422


def test_resolved_is_acked_without_incident(client):
    pd = {**PD, "event": {**PD["event"], "event_type": "incident.resolved"}}
    r = client.post("/api/v1/incidents", json=pd)
    assert r.status_code == 200 and r.json() == {"status": "resolved_ack"}