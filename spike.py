import asyncio
import json

import httpx
from pydantic import BaseModel


# A trivial schema, just to prove constrained decoding works at all.
class WeatherReport(BaseModel):
    city: str
    temperature_celsius: int
    conditions: str


async def main() -> None:
    schema = WeatherReport.model_json_schema()  # Pydantic -> JSON Schema dict

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "http://localhost:11434/api/chat",
            json={
                "model": "qwen3:8b",
                "messages": [
                    {"role": "user", "content": "Weather in Tokyo right now, make it up."}
                ],
                "stream": False,
                "format": schema,        # <-- THIS is the grammar constraint
            },
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json()

    content = body["message"]["content"]   # the model's text output (a JSON string)
    print("RAW:", content)

    report = WeatherReport.model_validate_json(content)  # parse + validate against schema
    print("PARSED:", report)


if __name__ == "__main__":
    asyncio.run(main())