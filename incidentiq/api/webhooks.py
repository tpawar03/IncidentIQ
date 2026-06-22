"""Inbound webhook contracts — the untrusted trust boundary (D-9).

Two alert providers, two JSON shapes, one normalized interface. These model the
*external* payloads (vs contracts.py/state.py which model internal state). A callable
discriminator (not a plain union) picks the provider by a field that truly distinguishes
them, so a malformed payload fails loudly in the right branch instead of silently coercing
into the wrong one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Discriminator, Field, Tag


class NormalizedAlert(BaseModel):
    """The provider-agnostic view the receiver fingerprints + persists on."""
    is_firing: bool
    alertname: str
    service: str | None = None
    namespace: str | None = None
    starts_at: datetime
    fingerprint: str | None = None   # provider-supplied; may be absent → we compute a fallback


# --- AlertManager v4 -------------------------------------------------------

class AMAlert(BaseModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime
    endsAt: datetime | None = None
    fingerprint: str | None = None


class AlertManagerV4(BaseModel):
    version: Literal["4"]
    status: Literal["firing", "resolved"]
    alerts: list[AMAlert] = Field(min_length=1)

    def normalized(self) -> NormalizedAlert:
        first = self.alerts[0]            # alerts[1:] are logged, not processed (FR-24)
        return NormalizedAlert(
            is_firing=first.status == "firing",
            alertname=first.labels.get("alertname", "unknown"),
            service=first.labels.get("service"),
            namespace=first.labels.get("namespace"),
            starts_at=first.startsAt,
            fingerprint=first.fingerprint,
        )


# --- PagerDuty v3 ----------------------------------------------------------

class PDData(BaseModel):
    id: str
    title: str = "unknown"
    status: str | None = None
    service: dict | None = None


class PDEvent(BaseModel):
    event_type: str                       # e.g. "incident.triggered" / "incident.resolved"
    occurred_at: datetime
    data: PDData


class PagerDutyV3(BaseModel):
    event: PDEvent

    def normalized(self) -> NormalizedAlert:
        ev = self.event
        return NormalizedAlert(
            is_firing=not ev.event_type.endswith("resolved"),
            alertname=ev.data.title,
            service=(ev.data.service or {}).get("summary"),
            namespace=None,
            starts_at=ev.occurred_at,
            fingerprint=ev.data.id,       # PD incident id is a stable dedup key
        )


# --- The union + callable discriminator ------------------------------------

def _provider(payload) -> str:
    """Pick the branch by a field that truly distinguishes the two providers."""
    get = payload.get if isinstance(payload, dict) else lambda k: getattr(payload, k, None)
    if get("version") is not None:
        return "alertmanager"
    if get("event") is not None:
        return "pagerduty"
    return "unknown"   # matches no Tag → Pydantic raises a proper ValidationError (→ 422)"


WebhookPayload = Annotated[
    Union[
        Annotated[AlertManagerV4, Tag("alertmanager")],
        Annotated[PagerDutyV3, Tag("pagerduty")],
    ],
    Discriminator(_provider),
]