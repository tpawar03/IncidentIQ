"""Local embedding model: BAAI/bge-base-en-v1.5 (768-dim, L2-normalized).

bge is ASYMMETRIC: a query gets an instruction prefix, a passage does not.
Encoding that asymmetry here once means the corpus loader and the retriever
can never disagree about how text was embedded.
"""
from __future__ import annotations

from functools import lru_cache

from sentence_transformers import SentenceTransformer

from transformers.utils import logging as _hf_logging

_hf_logging.set_verbosity_error()  # the chunker intentionally tokenizes >512-token units to split them

MODEL_NAME = "BAAI/bge-base-en-v1.5"
EMBED_DIM = 768
# bge-en-v1.5 query instruction; passages use NO prefix.
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


@lru_cache(maxsize=1)
def get_embedder() -> SentenceTransformer:
    """Singleton load — also the pre-warm entry point (MF-2; warmup.py TODO)."""
    return SentenceTransformer(MODEL_NAME)


def embed_passages(texts: list[str]) -> list[list[float]]:
    """Embed corpus chunks (NO prefix). Returns normalized 768-dim vectors."""
    model = get_embedder()
    vecs = model.encode(
        texts, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
    )
    return vecs.tolist()


def embed_query(text: str) -> list[float]:
    """Embed a search query (WITH bge instruction prefix). Used by the retriever."""
    model = get_embedder()
    vec = model.encode(
        _QUERY_PREFIX + text,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return vec.tolist()

def count_tokens(text: str) -> int:
    """Token count under bge's own tokenizer — the 500-cap is measured in these."""
    return len(get_embedder().tokenizer.tokenize(text))