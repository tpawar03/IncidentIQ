"""Deterministic dedup key for an inbound alert (FR-24).

Two tiers: trust the provider's fingerprint if present; otherwise hash the identifying
fields with startsAt truncated to the minute (absorbs redelivery timestamp jitter).
"""
from __future__ import annotations

import hashlib

from incidentiq.api.webhooks import NormalizedAlert


def compute_fingerprint(alert: NormalizedAlert) -> str:
    """Stable key for one logical incident. Same alert re-fired → same fingerprint."""
    if alert.fingerprint:
        return alert.fingerprint

    minute = alert.starts_at.replace(second=0, microsecond=0).isoformat()
    parts = [alert.alertname, alert.service or "", alert.namespace or "", minute]
    digest = hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]
    return f"fb_{digest}"   # fb_ = "fallback", distinguishes computed keys from provider-given ones