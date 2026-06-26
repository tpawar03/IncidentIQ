"""Alert enricher — NormalizedAlert + raw payload → IncidentContext.

Still the untrusted-input side of the system (D-9): this is the last station
that touches raw external data before the graph receives a trusted
IncidentContext. Every field is extracted defensively; missing annotations
become None (graceful nulls, D-11). deploy_gap_minutes / last_deploys are
stubbed — real deploy tracking is a later task.
"""
from __future__ import annotations

from incidentiq.api.webhooks import NormalizedAlert
from incidentiq.state import IncidentContext


def enrich(alert: NormalizedAlert, raw: dict) -> IncidentContext:
    """Build IncidentContext from a NormalizedAlert and the raw inbound payload.

    `raw` is the exact dict persisted to incidents.raw_payload (untrusted).
    NormalizedAlert is the already-validated provider-agnostic view; we trust
    its scalar fields and reach into `raw` only for the richer label/annotation
    data the normalizer dropped.
    """
    labels, annotations = _labels_annotations(raw)

    return IncidentContext(
        service=alert.service or labels.get("service", "unknown"),
        alert_name=alert.alertname,
        namespace=alert.namespace or labels.get("namespace"),
        severity=labels.get("severity"),
        summary=annotations.get("summary") or alert.alertname,
        affected_endpoint=annotations.get("affected_endpoint"),
        traceback=annotations.get("traceback"),
        repo_url=labels.get("repo_url") or annotations.get("repo_url"),
        deploy_commit=annotations.get("deploy_commit"),
        last_deploys=[],          # stub — deploy tracking is a later task
        deploy_gap_minutes=None,  # stub — depends on deploy tracking
        starts_at=alert.starts_at,
    )


def _labels_annotations(raw: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Pull (labels, annotations) from the first alert of an AlertManager payload.

    PagerDuty (and anything without an `alerts[]` array) yields two empty dicts,
    so the caller cleanly falls back to NormalizedAlert's scalar fields.
    """
    alerts = raw.get("alerts") or []
    if not alerts:
        return {}, {}
    first = alerts[0]
    return first.get("labels") or {}, first.get("annotations") or {}