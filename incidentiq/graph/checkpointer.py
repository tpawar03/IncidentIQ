"""Postgres checkpointer for durable, resumable graph runs (FR-33).

TASK — to build here, per docs/AGENT_ORCHESTRATION.md §2.3:
  - Wrap langgraph's Postgres saver against the same DB as db.py (host 5433).
  - One graph run at a time (incidents are SERIAL); checkpoint after each node so a
    process killed mid-incident resumes from the last node in <10s.

Nothing implemented yet — this is the home, not the work.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from incidentiq.db import DATABASE_URL


@asynccontextmanager
async def postgres_checkpointer(conn_string: str = DATABASE_URL):
    """Yield an AsyncPostgresSaver bound to the app DB, checkpoint tables ensured (FR-33).

    One Postgres for vector store + app + checkpoints (§2.3). `setup()` is idempotent,
    so this is safe on every boot. Async because the graph runs via `ainvoke`.

    Usage:
        async with postgres_checkpointer() as cp:
            app = build_graph(client=..., checkpointer=cp)
            await app.ainvoke(state, {"configurable": {"thread_id": incident_id}})
    """
    async with AsyncPostgresSaver.from_conn_string(conn_string) as saver:
        await saver.setup()
        yield saver