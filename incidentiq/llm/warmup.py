"""Startup pre-warm so cold model-load never lands on a live incident's latency path (MF-2)."""

from pydantic import BaseModel, Field

from incidentiq.llm.ollama_client import OllamaClient


class _Warmup(BaseModel):
    ok: bool = Field(description="Return true.")


async def prewarm_llm(client: OllamaClient) -> bool:
    """One throwaway constrained generation to force the model into memory.

    Never raises: a warmup failure must not block startup — the first real call
    will surface any genuine problem with its own typed error.
    """
    try:
        await client.generate_structured("Return {\"ok\": true}.", _Warmup)
        return True
    except Exception:
        return False

    # TODO(retriever task): also prewarm bge-base-en-v1.5 embedder + bge-reranker-base
    # here, so the first retrieval doesn't pay their cold-load either.