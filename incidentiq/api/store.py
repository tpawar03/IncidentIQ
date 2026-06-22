"""Persistence for the ingestion slice: schema + race-free insert/dedup.

Mirrors the retrieval domain's apply_schema() pattern. Dedup is enforced by a partial
unique index (one OPEN incident per fingerprint), not a check-then-insert — so concurrent
duplicate webhook deliveries can't both create an incident (FR-24).
"""
from __future__ import annotations

import json
from pathlib import Path

from psycopg.types.json import Jsonb

from incidentiq.db import connect

_SCHEMA_SQL = Path(__file__).parent / "schema.sql"

# Statuses the partial unique index treats as "live" — keep in sync with schema.sql.
_ACTIVE = ("created", "investigating", "awaiting_approval", "executing")


def apply_schema() -> None:
    """Idempotent: create the incidents table + dedup index if absent."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL.read_text())
        conn.commit()


def insert_incident(
    *, incident_id: str, fingerprint: str, provider: str, alertname: str,
    service: str | None, namespace: str | None, starts_at, raw_payload: dict,
) -> tuple[str, bool]:
    """Insert a new incident, or detect a duplicate of an open one.

    Returns (incident_id, is_new). On a fingerprint collision with a live incident the
    INSERT is a no-op and we return the existing incident's id with is_new=False.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO incidents
                (incident_id, fingerprint, status, provider, alertname, service,
                 namespace, starts_at, raw_payload)
            VALUES (%s, %s, 'created', %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint)
                WHERE status IN ('created','investigating','awaiting_approval','executing')
                DO NOTHING
            RETURNING incident_id
            """,
            (incident_id, fingerprint, provider, alertname, service,
             namespace, starts_at, Jsonb(raw_payload)),
        )
        row = cur.fetchone()
        if row is not None:                       # inserted → genuinely new
            conn.commit()
            return row[0], True

        # Conflict: a live incident with this fingerprint already exists — return it.
        cur.execute(
            "SELECT incident_id FROM incidents "
            "WHERE fingerprint = %s AND status = ANY(%s) "
            "ORDER BY created_at DESC LIMIT 1",
            (fingerprint, list(_ACTIVE)),
        )
        existing = cur.fetchone()
        conn.commit()
        return (existing[0] if existing else incident_id), False