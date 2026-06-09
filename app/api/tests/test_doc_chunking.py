"""Markdown chunker (Phase 11B): determinism, paragraph packing, heading
grounding, overlap, and oversized-paragraph hard-wrap."""
from __future__ import annotations

from ai4ia_api.library.chunking import chunk_markdown


def test_empty_or_whitespace_yields_no_chunks():
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n  ") == []


def test_single_short_paragraph_one_chunk():
    chunks = chunk_markdown("hello world", max_chars=100, overlap=0)
    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert chunks[0].text == "hello world"
    assert chunks[0].grounding["heading"] is None
    assert chunks[0].grounding["charStart"] == 0


def test_is_deterministic():
    md = "# H1\n\npara one\n\npara two\n\npara three"
    assert [c.text for c in chunk_markdown(md, max_chars=20, overlap=5)] == [
        c.text for c in chunk_markdown(md, max_chars=20, overlap=5)
    ]


def test_heading_is_tracked_for_following_paragraphs():
    md = "# Section A\n\nalpha\n\n# Section B\n\nbeta"
    chunks = chunk_markdown(md, max_chars=12, overlap=0)
    headings = {c.grounding["heading"] for c in chunks}
    # Both section headings appear as grounding on their content chunks.
    assert "Section A" in headings
    assert "Section B" in headings


def test_packs_paragraphs_up_to_max_chars():
    # Two small paragraphs that together fit under the cap → one chunk.
    md = "aaa\n\nbbb"
    chunks = chunk_markdown(md, max_chars=100, overlap=0)
    assert len(chunks) == 1
    assert "aaa" in chunks[0].text and "bbb" in chunks[0].text


def test_splits_when_exceeding_max_chars():
    md = "aaaa\n\nbbbb\n\ncccc"
    chunks = chunk_markdown(md, max_chars=5, overlap=0)
    assert len(chunks) == 3
    assert [c.index for c in chunks] == [0, 1, 2]


def test_overlap_prepends_previous_tail():
    md = "first\n\nsecond"
    chunks = chunk_markdown(md, max_chars=6, overlap=3)
    assert len(chunks) == 2
    # The second chunk carries the last 3 chars of "first" as an overlap prefix.
    assert chunks[1].text.startswith("rst")
    assert chunks[1].text.endswith("second")
    # Grounding still points at the primary span only.
    assert chunks[1].grounding["charStart"] > 0


def test_oversized_paragraph_is_hard_wrapped():
    md = "x" * 25
    chunks = chunk_markdown(md, max_chars=10, overlap=0)
    assert len(chunks) == 3
    assert chunks[0].text == "x" * 10
    assert chunks[2].text == "x" * 5
