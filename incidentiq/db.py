"""Shared Postgres connection point: vector store + app DB + (later) checkpointer.

Schema-agnostic on purpose — each domain owns its own DDL (e.g. the chunks table
lives in incidentiq/retrieval/). This module only knows how to connect."""
import os

import psycopg

# Host 5433 → container 5432 (D-6). Override via env in other environments.
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://incidentiq:incidentiq@localhost:5433/incidentiq",
)


def connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL)