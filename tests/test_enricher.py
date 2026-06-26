"""Task 6 enricher tests — pure logic, always run (no DB).

The enricher is the last station on the untrusted side: it turns a NormalizedAlert
plus the raw payload into a trusted IncidentContext. Coverage: full extraction from
an AlertManager payload, graceful nulls when annotations are absent, and the
PagerDuty path (no alerts[] array) falling back to NormalizedAlert scalars.
"""
from datetime import datetime, timezone

from pydantic import TypeAdapter

from incidentiq.api.enricher import enrich
from incidentiq.api.webhooks import WebhookPayload

_webhook = TypeAdapter(WebhookPayload)

# A rich AlertManager payload exercising every extractable field.
AM_FULL = {"version": "4", "status": "firing", "alerts": [
    {"status": "firing",
     "labels": {"alertname": "adHighCpu", "service": "ad", "namespace": "otel",
                "severity": "critical", "repo_url": "https://github.com/acme/ad"},
     "annotations": {"summary": "cpu high", "traceback": "Traceback...\nKeyError",
                     "affected_endpoint": "/api/ad", "deploy_commit": "deadbeef"},
     "startsAt": "2026-06-22T10:00:00Z", "fingerprint": "abc123"}]}

# A bare AlertManager payload: only the labels the normalizer reads, no annotations.
AM_BARE = {"version": "4", "status": "firing", "alerts": [
    {"status": "firing",
     "labels": {"alertname": "adHighCpu", "service": "ad"},
     "startsAt": "2026-06-22T10:00:00Z"}]}

PD = {"event": {"event_type": "incident.triggered", "occurred_at": "2026-06-22T10:00:00Z",
                "data": {"id": "PD-9", "title": "cart latency", "service": {"summary": "cart"}}}}


def _enrich(raw):
    return enrich(_webhook.validate_python(raw).normalized(), raw)


# --- full extraction ---------------------------------------------------------

def test_full_alertmanager_extraction():
    ctx = _enrich(AM_FULL)
    assert ctx.service == "ad"
    assert ctx.alert_name == "adHighCpu"
    assert ctx.namespace == "otel"
    assert ctx.severity == "critical"
    assert ctx.summary == "cpu high"
    assert ctx.traceback.startswith("Traceback")
    assert ctx.affected_endpoint == "/api/ad"
    assert ctx.repo_url == "https://github.com/acme/ad"
    assert ctx.deploy_commit == "deadbeef"
    assert ctx.starts_at == datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)


# --- graceful nulls ----------------------------------------------------------

def test_missing_annotations_become_none():
    ctx = _enrich(AM_BARE)
    assert ctx.severity is None
    assert ctx.traceback is None          # null → retriever uses keyword fallback
    assert ctx.affected_endpoint is None
    assert ctx.repo_url is None
    assert ctx.deploy_commit is None
    assert ctx.namespace is None


def test_summary_falls_back_to_alert_name():
    # summary is required on IncidentContext; absent annotation → alert_name, never empty.
    ctx = _enrich(AM_BARE)
    assert ctx.summary == "adHighCpu"


def test_deploy_fields_are_stubbed():
    ctx = _enrich(AM_FULL)
    assert ctx.last_deploys == []
    assert ctx.deploy_gap_minutes is None


# --- PagerDuty: no labels/annotations → fall back to scalars -----------------

def test_pagerduty_falls_back_to_normalized_scalars():
    ctx = _enrich(PD)
    assert ctx.service == "cart"          # from NormalizedAlert, not labels
    assert ctx.alert_name == "cart latency"
    assert ctx.summary == "cart latency"  # no annotations → alert_name
    assert ctx.severity is None
    assert ctx.traceback is None
