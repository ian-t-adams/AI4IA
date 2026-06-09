"""Deterministic, heading-aware Markdown chunker for retrieval (Phase 11B).

Content Understanding returns Markdown; we split it into bounded, overlapping
chunks for embedding into the ``doc_chunks`` vector store. The splitter packs
whole paragraphs up to ``max_chars`` (hard-wrapping any single oversized
paragraph), tracks the nearest preceding heading for grounding, and prepends a
small ``overlap`` tail from the previous chunk so a span that straddles a boundary
is still retrievable. Pure and deterministic — same input always yields the same
chunks — so it is exercised entirely by unit tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Chunk:
    index: int
    text: str
    # {"charStart": int, "charEnd": int, "heading": str | None} — the primary
    # (non-overlap) span in the source Markdown, for citations/deep-linking.
    grounding: dict[str, Any] = field(default_factory=dict)


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """Char spans of blank-line-separated paragraphs, with original offsets."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    pos = 0
    for line in text.splitlines(keepends=True):
        if line.strip() == "":
            if start is not None:
                spans.append((start, pos))
                start = None
        elif start is None:
            start = pos
        pos += len(line)
    if start is not None:
        spans.append((start, pos))
    return spans


def _heading_of(segment: str) -> str | None:
    s = segment.strip()
    if s.startswith("#"):
        return s.lstrip("#").strip() or None
    return None


def chunk_markdown(
    markdown: str, *, max_chars: int = 1200, overlap: int = 150
) -> list[Chunk]:
    text = markdown or ""
    if not text.strip():
        return []
    max_chars = max(1, int(max_chars))
    overlap = max(0, min(int(overlap), max_chars - 1))

    # Phase 1: build primary spans (start, end, heading), packing paragraphs up to
    # max_chars and hard-wrapping any oversized paragraph (no internal overlap;
    # phase 2 adds the cross-chunk overlap uniformly).
    spans: list[tuple[int, int, str | None]] = []
    heading: str | None = None
    cur_start: int | None = None
    cur_end: int | None = None
    cur_heading: str | None = None

    def flush() -> None:
        nonlocal cur_start, cur_end, cur_heading
        if cur_start is not None and cur_end is not None:
            spans.append((cur_start, cur_end, cur_heading))
        cur_start = cur_end = None
        cur_heading = None

    for s, e in _paragraph_spans(text):
        h = _heading_of(text[s:e])
        if h is not None:
            heading = h
        if e - s > max_chars:
            flush()
            pos = s
            while pos < e:
                end = min(pos + max_chars, e)
                spans.append((pos, end, heading))
                pos = end
            continue
        if cur_start is None:
            cur_start, cur_end, cur_heading = s, e, heading
        elif e - cur_start <= max_chars:
            cur_end = e
        else:
            flush()
            cur_start, cur_end, cur_heading = s, e, heading
    flush()

    # Phase 2: materialize chunks, prepending the previous chunk's overlap tail.
    chunks: list[Chunk] = []
    prev_primary = ""
    for s, e, h in spans:
        primary = text[s:e].strip()
        if not primary:
            continue
        prefix = prev_primary[-overlap:] if (overlap and prev_primary) else ""
        if prefix:
            sep = "" if prefix.endswith("\n") else "\n"
            body = f"{prefix}{sep}{primary}"
        else:
            body = primary
        chunks.append(
            Chunk(
                index=len(chunks),
                text=body,
                grounding={"charStart": s, "charEnd": e, "heading": h},
            )
        )
        prev_primary = primary
    return chunks
