"""Task 1 exit-criterion harness: measures structured-output validity rate + worst-case
warm latency for the grammar-constrained Ollama harness. See plans/TASK_01_*.md (3f)."""

import asyncio
import statistics
import time
from dataclasses import dataclass

from incidentiq.contracts import RCADraft, TriageDraft
from incidentiq.errors import LLMCallError
from incidentiq.llm.ollama_client import OllamaClient
from incidentiq.llm.warmup import prewarm_llm

REPEATS = 5  # ~100 samples for a tighter invalid-rate bound — see note below

# (service, symptom, evidence chunks) — OTel Astronomy Shop domain
SCENARIOS = [
    ("checkoutservice", "p99 latency jumped to 4s after deploy d4f1a",
     "[chunk_10] prior checkout latency = missing DB index; [chunk_11] checkout uses orders Postgres"),
    ("cartservice", "returning 500s, 'redis connection refused' in logs",
     "[chunk_20] cart stores sessions in redis; [chunk_21] runbook: restart redis on conn refused"),
    ("paymentservice", "error rate 30%, 'invalid currency code' in logs",
     "[chunk_30] payment validates against currencyservice; [chunk_31] currency list changed last deploy"),
    ("productcatalogservice", "high CPU and OOMKilled restarts",
     "[chunk_40] catalog loads full product list into memory; [chunk_41] mem-limit is 128Mi"),
    ("recommendationservice", "p95 latency doubled after flag rec_cache_off was flipped",
     "[chunk_50] rec uses a cache toggled by rec_cache_off; [chunk_51] flag flipped 10m before alert"),
    ("adservice", "long GC pauses, java heap steadily growing",
     "[chunk_60] ad service JVM heap is 256m; [chunk_61] prior incident was an ad cache leak"),
    ("currencyservice", "intermittent timeouts to an upstream dependency",
     "[chunk_70] currency calls an external FX api; [chunk_71] FX api SLA is 99.5%"),
    ("shippingservice", "quote requests failing with a nil pointer panic",
     "[chunk_80] shipping computes quotes from cart weight; [chunk_81] recent commit changed the quote fn"),
    ("emailservice", "emails not being sent, smtp auth error",
     "[chunk_90] email uses smtp creds from env; [chunk_91] those creds were rotated yesterday"),
    ("frontend", "5xx spike driven by upstream checkout errors",
     "[chunk_99] frontend proxies to checkout; [chunk_98] checkout deploy d4f1a in the same window"),
]


def rca_prompt(svc, sym, ev):
    return (f"Incident: {svc} — {sym}.\nEvidence chunks: {ev}\n"
            "Diagnose the probable root cause and cite the chunk_ids you used.")


def triage_prompt(svc, sym, ev):
    return (f"Incident: {svc} — {sym}.\nEvidence: {ev}\n"
            "Classify this incident as infra, config, code_bug, or unknown, with a brief rationale.")


@dataclass
class Result:
    label: str
    kind: str          # "rca" or "triage"
    ok: bool
    latency_s: float
    error: str | None = None


async def run_one(client, label, kind, prompt, schema):
    t0 = time.perf_counter()
    try:
        await client.generate_structured(prompt, schema)
        return Result(label, kind, True, time.perf_counter() - t0)
    except LLMCallError as e:
        return Result(label, kind, False, time.perf_counter() - t0, e.typed_error.kind)


async def main():
    client = OllamaClient()
    results: list[Result] = []
    try:
        await prewarm_llm(client)  # measure WARM latency, not cold (F-10)
        for _ in range(REPEATS):
            for svc, sym, ev in SCENARIOS:
                results.append(await run_one(client, svc, "rca", rca_prompt(svc, sym, ev), RCADraft))
                results.append(await run_one(client, svc, "triage", triage_prompt(svc, sym, ev), TriageDraft))
    finally:
        await client.aclose()
    report(results)


def report(results):
    n = len(results)
    fails = [r for r in results if not r.ok]
    lat = sorted(r.latency_s for r in results)
    rca_lat = [r.latency_s for r in results if r.kind == "rca"]
    invalid_rate = 100 * len(fails) / n

    print("=" * 56)
    print(f"samples: {n}  (REPEATS={REPEATS})")
    print(f"invalid-JSON rate: {invalid_rate:.1f}%  ({len(fails)} failures)   [target <1%]")
    for r in fails:
        print(f"  FAIL {r.kind:6} {r.label}: {r.error}")
    print(f"latency (warm)  min={lat[0]:.2f}s  median={statistics.median(lat):.2f}s  max={lat[-1]:.2f}s")
    worst_rca = max(rca_lat)
    print(f"worst single RCA call: {worst_rca:.2f}s")
    print(f"N=3 self-consistency RCA worst-case projection: {3 * worst_rca:.1f}s   [vs <60s budget]")
    print("=" * 56)


if __name__ == "__main__":
    asyncio.run(main())