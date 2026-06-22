"""Task 4 corpus tests: chunking invariants + the empty-corpus guard.

DB-free on purpose — the chunker and the pre-flight guard are pure logic, so the
suite stays green without Docker. The live-DB seed is verified by running init_corpus.
"""
import incidentiq.retrieval.init_corpus as ic
from incidentiq.retrieval.chunking import MAX_TOKENS, chunk_postmortem, chunk_runbook

RUNBOOK = """# Pod OOMKilled

## Meaning
The pod exceeded its memory limit and was killed by the kernel OOM killer.

## Playbook
1. Check `kubectl describe pod`.
2. Inspect memory limits.
"""

POSTMORTEM = "Title line.\n\nThe outage began at 02:00 when the cache filled.\n\nWe rolled back the flag."


def test_runbook_chunks_are_parent_child():
    chunks = chunk_runbook("rb.md", RUNBOOK)
    sections = {c.parent_section for c in chunks}
    assert "Meaning" in sections and "Playbook" in sections   # headings become parents (FR-07)
    assert all(c.corpus == "runbook" for c in chunks)


def test_postmortem_chunks_are_flat():
    chunks = chunk_postmortem("pm.md", POSTMORTEM)
    assert chunks and all(c.parent_section is None for c in chunks)
    assert all(c.corpus == "postmortem" for c in chunks)


def test_token_cap_holds_even_for_oversize_paragraph():
    huge = "word " * 4000                                       # one paragraph, far over the cap
    chunks = chunk_postmortem("big.md", huge)
    assert len(chunks) > 1                                      # the sentence/word fallback fired
    assert all(c.token_count <= MAX_TOKENS for c in chunks)


def test_chunk_ids_are_deterministic():
    a = [c.chunk_id for c in chunk_runbook("rb.md", RUNBOOK)]
    b = [c.chunk_id for c in chunk_runbook("rb.md", RUNBOOK)]
    assert a == b and len(set(a)) == len(a)                     # stable (FR-09) and unique


def test_init_refuses_empty_corpus(monkeypatch):
    monkeypatch.setattr(ic, "_collect", lambda: [])
    try:
        ic.init_corpus()
        assert False, "init_corpus should refuse an empty corpus"
    except RuntimeError as e:
        assert "empty corpus" in str(e)