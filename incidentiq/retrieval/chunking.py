"""Chunk corpus documents under a 500-token cap (bge's hard limit is 512).

Postmortems -> flat chunks (parent_section=None).
Runbooks    -> parent-child: section headings are parents; <=500-token children
               carry parent_section (FR-07: never an orphan sub-chunk).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from incidentiq.retrieval.embedding import count_tokens

MAX_TOKENS = 500  # under bge's 512 ceiling, so nothing is silently truncated


@dataclass
class Chunk:
    chunk_id: str
    corpus: Literal["postmortem", "runbook"]
    source_doc: str
    parent_section: str | None
    text: str
    token_count: int


def _chunk_id(corpus: str, source_doc: str, section: str | None, ordinal: int) -> str:
    prefix = "pm" if corpus == "postmortem" else "rb"
    key = f"{source_doc}|{section or ''}|{ordinal}"
    return f"{prefix}_{hashlib.sha1(key.encode()).hexdigest()[:12]}"


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]

def _hard_split(text: str) -> list[str]:
    """Last resort for an oversize unit with no sentence boundaries (log dumps,
    minified blobs): pack words into <=MAX_TOKENS windows. Guarantees progress."""
    out: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for word in text.split():
        wt = count_tokens(word) or 1
        if buf and buf_tokens + wt > MAX_TOKENS:
            out.append(" ".join(buf)); buf, buf_tokens = [], 0
        buf.append(word); buf_tokens += wt
    if buf:
        out.append(" ".join(buf))
    return out


def _pack(units: list[str]) -> list[str]:
    """Greedily pack units into <=MAX_TOKENS chunks; a unit bigger than the cap
    is sentence-split so one huge paragraph can't bust the limit."""
    chunks: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for unit in units:
        ut = count_tokens(unit)
        if ut > MAX_TOKENS:                       # too big alone
            if buf:
                chunks.append("\n\n".join(buf)); buf, buf_tokens = [], 0
            sentences = _split_sentences(unit)
            if len(sentences) > 1:                 # sentence boundaries exist -> recurse
                chunks.extend(_pack(sentences))
            else:                                  # none -> last-resort word split (always progresses)
                chunks.extend(_hard_split(unit))
            continue
        if buf and buf_tokens + ut > MAX_TOKENS:
            chunks.append("\n\n".join(buf)); buf, buf_tokens = [], 0
        buf.append(unit); buf_tokens += ut
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


def chunk_postmortem(source_doc: str, text: str) -> list[Chunk]:
    out: list[Chunk] = []
    for i, body in enumerate(_pack(_split_paragraphs(text))):
        out.append(Chunk(
            chunk_id=_chunk_id("postmortem", source_doc, None, i),
            corpus="postmortem", source_doc=source_doc,
            parent_section=None, text=body, token_count=count_tokens(body),
        ))
    return out


_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)


def _split_sections(text: str) -> list[tuple[str | None, str]]:
    """Split markdown into (heading, body) segments; pre-heading text -> (None, body)."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [(None, text.strip())] if text.strip() else []
    sections: list[tuple[str | None, str]] = []
    if matches[0].start() > 0 and text[: matches[0].start()].strip():
        sections.append((None, text[: matches[0].start()].strip()))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((m.group(1).strip(), body))
    return sections


def chunk_runbook(source_doc: str, text: str) -> list[Chunk]:
    out: list[Chunk] = []
    ordinal = 0
    for heading, body in _split_sections(text):
        for body_chunk in _pack(_split_paragraphs(body)):
            out.append(Chunk(
                chunk_id=_chunk_id("runbook", source_doc, heading, ordinal),
                corpus="runbook", source_doc=source_doc,
                parent_section=heading, text=body_chunk, token_count=count_tokens(body_chunk),
            ))
            ordinal += 1
    return out