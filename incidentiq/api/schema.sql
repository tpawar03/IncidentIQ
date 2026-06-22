-- incidents: raw-payload persistence (FR-04) + dedup spine (FR-24).
-- Owned by the api domain (D-7); db.connect() stays schema-agnostic.
CREATE TABLE IF NOT EXISTS incidents (
    incident_id  TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL,
    provider     TEXT NOT NULL,              -- 'alertmanager' | 'pagerduty'
    alertname    TEXT NOT NULL,
    service      TEXT,
    namespace    TEXT,
    starts_at    TIMESTAMPTZ NOT NULL,       -- anchors the post-mortem timeline (FR-17)
    raw_payload  JSONB NOT NULL,             -- source of truth for replay (FR-04)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One OPEN incident per fingerprint (FR-24). Partial: a new firing AFTER the prior
-- incident reaches a terminal status is allowed (terminal rows are excluded from the index).
CREATE UNIQUE INDEX IF NOT EXISTS uq_incidents_open_fingerprint
    ON incidents (fingerprint)
    WHERE status IN ('created', 'investigating', 'awaiting_approval', 'executing');