from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

ErrorKind = Literal[
    "clone_timeout",
    "empty_retrieval",
    "invalid_json",
    "llm_timeout",          # NEW: model generation stalled/timed out
    "low_confidence",
    "unsupported_language",
    "patch_failed",
    "other",
]


class TypedError(BaseModel):
    """A structured failure record — written into IncidentState, read by escalation (decision #10)."""
    node: str
    kind: ErrorKind
    reason: str
    ts: datetime


class LLMCallError(Exception):
    """Control-flow wrapper raised by the harness; carries the TypedError for the node to record."""

    def __init__(self, typed_error: TypedError) -> None:
        self.typed_error = typed_error
        super().__init__(f"{typed_error.kind}: {typed_error.reason}")


def llm_error(kind: ErrorKind, reason: str, node: str = "ollama_client") -> LLMCallError:
    """Small constructor so call sites stay terse."""
    return LLMCallError(TypedError(node=node, kind=kind, reason=reason, ts=datetime.now(timezone.utc)))