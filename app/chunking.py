# Chunking: splits a document into evidence-sized pieces with stable identifiers.
#
# Part of the indexing boundary (ADR-007). Chunks are what selection ranks, what the
# prompt contains, and what answers cite, so their identifiers must be stable and
# meaningful (REQ-051, INT-04):
#
#   <relative path>#<ordinal>:<first 8 hex chars of SHA-256 of the chunk text>
#   e.g.  policies/leave.md#2:9f3c1a07
#
# The path says where the evidence came from, the ordinal says where in the file, and
# the hash pins the exact text: if the text changes, the identifier changes, so a cited
# identifier always refers to the content the model actually saw.
#
# Splitting is paragraph-aware: blank lines separate paragraphs, paragraphs are packed
# together up to max_chars, and a paragraph longer than max_chars is cut at the last
# whitespace before the limit (hard cut only if there is none). No overlap between
# chunks: simpler to explain, at the cost of occasionally splitting a sentence.

import hashlib
import re
from dataclasses import dataclass

from app.ingestion import Document

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n+")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    rel_path: str
    ordinal: int
    text: str


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    pieces = []
    rest = paragraph
    while len(rest) > max_chars:
        cut = rest.rfind(" ", 0, max_chars + 1)
        cut = max(cut, rest.rfind("\n", 0, max_chars + 1))
        if cut <= 0:
            cut = max_chars  # no whitespace: hard cut (e.g. a long URL or token run)
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return pieces


def chunk_text(text: str, max_chars: int) -> list[str]:
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    pieces: list[str] = []
    for paragraph in _PARAGRAPH_BREAK.split(normalised):
        paragraph = paragraph.strip()
        if paragraph:
            pieces.extend(_split_long(paragraph, max_chars))

    # Pack consecutive pieces into chunks without exceeding max_chars.
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def chunk_document(document: Document, max_chars: int) -> tuple[Chunk, ...]:
    return tuple(
        Chunk(
            chunk_id=f"{document.rel_path}#{i}:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}",
            rel_path=document.rel_path,
            ordinal=i,
            text=text,
        )
        for i, text in enumerate(chunk_text(document.text, max_chars))
    )
