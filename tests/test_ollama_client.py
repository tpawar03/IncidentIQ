import asyncio

from pydantic import BaseModel

from incidentiq.llm.ollama_client import OllamaClient


class WeatherReport(BaseModel):
    city: str
    temperature_celsius: int
    conditions: str


def test_generate_structured_returns_valid_model():
    async def run():
        client = OllamaClient()
        try:
            return await client.generate_structured(
                "Weather in Tokyo right now, make it up.", WeatherReport
            )
        finally:
            await client.aclose()

    report = asyncio.run(run())
    assert report.city  # non-empty
    assert isinstance(report.temperature_celsius, int)

import httpx
import pytest

from incidentiq.errors import LLMCallError


class _FakeResp:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"message": {"content": self._content}}


def test_invalid_output_retries_once_then_raises(monkeypatch):
    client = OllamaClient(max_attempts=2)
    calls = {"n": 0}

    async def fake_post(url, json):
        calls["n"] += 1
        return _FakeResp('{"wrong": "shape"}')  # missing required WeatherReport fields

    monkeypatch.setattr(client._client, "post", fake_post)

    async def run():
        try:
            await client.generate_structured("x", WeatherReport)
        finally:
            await client.aclose()

    with pytest.raises(LLMCallError) as exc:
        asyncio.run(run())

    assert exc.value.typed_error.kind == "invalid_json"
    assert calls["n"] == 2  # original + exactly one retry


def test_timeout_fails_fast_without_retry(monkeypatch):
    client = OllamaClient(max_attempts=2)
    calls = {"n": 0}

    async def fake_post(url, json):
        calls["n"] += 1
        raise httpx.ReadTimeout("simulated hang")

    monkeypatch.setattr(client._client, "post", fake_post)

    async def run():
        try:
            await client.generate_structured("x", WeatherReport)
        finally:
            await client.aclose()

    with pytest.raises(LLMCallError) as exc:
        asyncio.run(run())

    assert exc.value.typed_error.kind == "llm_timeout"
    assert calls["n"] == 1  # no retry on timeout


from incidentiq.contracts import RCADraft


def test_rca_draft_generates_and_validates():
    prompt = (
        "Incident: checkout-service p99 latency spiked to 4s after deploy abc123.\n"
        "Evidence chunks:\n"
        "  [chunk_42] postmortem: a prior latency spike was caused by a missing DB index.\n"
        "  [chunk_77] runbook: checkout-service connects to the orders Postgres.\n"
        "Diagnose the probable root cause. Cite chunk_ids you used."
    )

    async def run():
        client = OllamaClient()
        try:
            return await client.generate_structured(prompt, RCADraft)
        finally:
            await client.aclose()

    draft = asyncio.run(run())
    assert draft.root_service                       # non-empty
    assert len(draft.source_citations) >= 1         # min_length enforced
    assert len(draft.probable_cause) <= 600         # max_length respected