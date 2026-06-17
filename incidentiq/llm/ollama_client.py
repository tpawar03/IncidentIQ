import asyncio
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from incidentiq.errors import llm_error

# The ONE global gate over Ollama (decision #7). Every model call acquires this.
_OLLAMA_SEMAPHORE = asyncio.Semaphore(1)

T = TypeVar("T", bound=BaseModel)


class OllamaClient:
    def __init__(
        self,
        model: str = "qwen3:8b",
        base_url: str = "http://localhost:11434",
        timeout_s: float = 30.0,
        max_attempts: int = 2,   # 1 original try + 1 retry on bad output
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate_structured(self, prompt: str, schema_model: type[T]) -> T:
        """Grammar-constrained generation. Returns a validated schema_model, or raises LLMCallError."""
        schema = schema_model.model_json_schema()
        last_reason = ""

        async with _OLLAMA_SEMAPHORE:  # serialize access to the model (decision #7)
            for attempt in range(1, self.max_attempts + 1):
                try:
                    resp = await self._client.post(
                        "/api/chat",
                        json={
                            "model": self.model,
                            "messages": [{"role": "user", "content": prompt}],
                            "stream": False,
                            "format": schema,
                            "think": False,
                        },
                    )
                    resp.raise_for_status()
                    content = resp.json()["message"]["content"]
                except httpx.TimeoutException as e:
                    # Hung generation: fail fast — retrying just doubles the latency hit (MF-2).
                    raise llm_error("llm_timeout", f"generation exceeded {self.timeout_s}s") from e

                try:
                    return schema_model.model_validate_json(content)
                except ValidationError as e:
                    # Bad shape/truncation: this is the ONLY case we retry (fallback, not mechanism).
                    last_reason = f"attempt {attempt}/{self.max_attempts}: {e.error_count()} validation error(s)"
                    continue

            raise llm_error("invalid_json", last_reason)