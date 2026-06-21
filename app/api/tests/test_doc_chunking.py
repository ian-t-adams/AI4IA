"""Markdown and audiovisual chunkers: determinism, grounding, overlap,
oversized-paragraph hard-wrap, timestamp formatting, and media timelines."""
from __future__ import annotations

from ai4ia_api.library.chunking import (
    chunk_audiovisual,
    chunk_markdown,
    format_timestamp,
    media_timeline,
)


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


# --- format_timestamp ---
def test_format_timestamp_minutes_seconds():
    assert format_timestamp(0) == "0:00"
    assert format_timestamp(2480) == "0:02"
    assert format_timestamp(133000) == "2:13"


def test_format_timestamp_hours():
    assert format_timestamp(3_661_000) == "1:01:01"


def test_format_timestamp_absent_or_invalid():
    assert format_timestamp(None) == ""
    assert format_timestamp(-5) == ""
    assert format_timestamp("nope") == ""  # type: ignore[arg-type]


# --- chunk_audiovisual ---
def _phrase(text, start, end, speaker="Speaker 1"):
    return {"text": text, "startTimeMs": start, "endTimeMs": end, "speaker": speaker}


def test_audiovisual_empty_contents_yields_no_chunks():
    assert chunk_audiovisual([]) == []
    # A segment with neither phrases nor markdown contributes nothing.
    assert chunk_audiovisual([{"startTimeMs": 0, "endTimeMs": 10}]) == []


def test_audiovisual_grounds_chunks_on_phrase_time_and_speaker():
    contents = [
        {
            "kind": "audioVisual",
            "startTimeMs": 0,
            "endTimeMs": 5000,
            "transcriptPhrases": [
                _phrase("Hello there.", 1000, 2000),
                _phrase("Welcome to the talk.", 2000, 4000),
            ],
        }
    ]
    chunks = chunk_audiovisual(contents, max_chars=100, overlap=0)
    assert len(chunks) == 1
    g = chunks[0].grounding
    assert g["startMs"] == 1000
    assert g["endMs"] == 4000
    assert g["speaker"] == "Speaker 1"
    # Single segment → no segment index.
    assert g["segment"] is None
    assert "Hello there." in chunks[0].text


def test_audiovisual_packs_phrases_up_to_max_chars():
    contents = [
        {
            "transcriptPhrases": [
                _phrase("aaaa", 0, 1000),
                _phrase("bbbb", 1000, 2000),
                _phrase("cccc", 2000, 3000),
            ]
        }
    ]
    # "aaaa bbbb" = 9 chars > 5 → each phrase becomes its own chunk.
    chunks = chunk_audiovisual(contents, max_chars=5, overlap=0)
    assert [c.text for c in chunks] == ["aaaa", "bbbb", "cccc"]
    assert chunks[0].grounding["startMs"] == 0
    assert chunks[1].grounding["startMs"] == 1000


def test_audiovisual_speaker_none_when_phrases_differ():
    contents = [
        {
            "transcriptPhrases": [
                _phrase("hi", 0, 1000, speaker="Speaker 1"),
                _phrase("yo", 1000, 2000, speaker="Speaker 2"),
            ]
        }
    ]
    chunks = chunk_audiovisual(contents, max_chars=100, overlap=0)
    assert len(chunks) == 1
    assert chunks[0].grounding["speaker"] is None


def test_audiovisual_segment_index_when_multiple_segments():
    contents = [
        {"transcriptPhrases": [_phrase("one", 0, 1000)]},
        {"transcriptPhrases": [_phrase("two", 5000, 6000)]},
    ]
    chunks = chunk_audiovisual(contents, max_chars=100, overlap=0)
    assert [c.grounding["segment"] for c in chunks] == [0, 1]
    assert chunks[1].grounding["startMs"] == 5000


def test_audiovisual_oversized_phrase_is_hard_wrapped_keeping_time():
    contents = [{"transcriptPhrases": [_phrase("x" * 25, 1000, 2000)]}]
    chunks = chunk_audiovisual(contents, max_chars=10, overlap=0)
    assert [len(c.text) for c in chunks] == [10, 10, 5]
    assert all(c.grounding["startMs"] == 1000 for c in chunks)
    assert all(c.grounding["endMs"] == 2000 for c in chunks)


def test_audiovisual_falls_back_to_markdown_when_no_phrases():
    contents = [
        {
            "startTimeMs": 7000,
            "endTimeMs": 9000,
            "markdown": "WEBVTT transcript body here",
        }
    ]
    chunks = chunk_audiovisual(contents, max_chars=100, overlap=0)
    assert len(chunks) == 1
    # Coarse segment timing stamped; no speaker from a markdown-only segment.
    assert chunks[0].grounding["startMs"] == 7000
    assert chunks[0].grounding["endMs"] == 9000
    assert chunks[0].grounding["speaker"] is None
    assert "transcript body" in chunks[0].text


def test_audiovisual_overlap_prepends_previous_primary():
    contents = [
        {
            "transcriptPhrases": [
                _phrase("first", 0, 1000),
                _phrase("second", 1000, 2000),
            ]
        }
    ]
    chunks = chunk_audiovisual(contents, max_chars=6, overlap=3)
    assert len(chunks) == 2
    assert chunks[1].text.startswith("rst")
    assert chunks[1].grounding["startMs"] == 1000


def test_audiovisual_is_deterministic():
    contents = [
        {
            "transcriptPhrases": [
                _phrase("alpha", 0, 1000),
                _phrase("beta", 1000, 2000),
            ]
        }
    ]
    first = [(c.text, c.grounding) for c in chunk_audiovisual(contents, max_chars=8, overlap=2)]
    second = [(c.text, c.grounding) for c in chunk_audiovisual(contents, max_chars=8, overlap=2)]
    assert first == second


# --- media_timeline scene/keyframe surfacing ---
def test_media_timeline_extracts_keyframes_and_shots():
    contents = [
        {
            "startTimeMs": 0,
            "endTimeMs": 30000,
            "keyFrameTimesMs": [0, 5000, 10000],
            "cameraShotTimesMs": [0, 12000],
        },
        {
            "startTimeMs": 30000,
            "endTimeMs": 60000,
            "keyFrameTimesMs": [35000],
            "cameraShotTimesMs": [],
        },
    ]
    tl = media_timeline(contents)
    assert tl["durationMs"] == 60000
    assert len(tl["segments"]) == 2
    assert tl["segments"][0]["index"] == 0
    assert tl["segments"][0]["startMs"] == 0
    assert tl["segments"][0]["endMs"] == 30000
    assert tl["segments"][0]["keyframes"] == [0, 5000, 10000]
    assert tl["segments"][0]["shots"] == [0, 12000]
    assert tl["segments"][1]["keyframes"] == [35000]
    assert tl["segments"][1]["shots"] == []


def test_media_timeline_sorts_dedupes_and_drops_negatives():
    contents = [
        {
            "startTimeMs": 0,
            "endTimeMs": 9000,
            "keyFrameTimesMs": [9000, 1000, 1000, -50, 3000],
            "cameraShotTimesMs": [2000, 2000],
        }
    ]
    tl = media_timeline(contents)
    # sorted, de-duplicated, negatives dropped
    assert tl["segments"][0]["keyframes"] == [1000, 3000, 9000]
    assert tl["segments"][0]["shots"] == [2000]


def test_media_timeline_skips_segments_without_span_or_markers():
    contents = [
        {"markdown": "no timing here"},  # no span, no markers -> skipped
        {"startTimeMs": 1000, "endTimeMs": 2000},  # span only -> kept
        "not-a-dict",
    ]
    tl = media_timeline(contents)
    assert len(tl["segments"]) == 1
    assert tl["segments"][0]["startMs"] == 1000
    assert tl["segments"][0]["keyframes"] == []


def test_media_timeline_empty_when_nothing_groundable():
    assert media_timeline([]) == {"durationMs": None, "segments": []}
    assert media_timeline([{"markdown": "x"}]) == {"durationMs": None, "segments": []}


def test_media_timeline_duration_from_markers_when_no_end():
    contents = [{"startTimeMs": 0, "keyFrameTimesMs": [0, 4000, 8000]}]
    tl = media_timeline(contents)
    assert tl["durationMs"] == 8000
    assert tl["segments"][0]["endMs"] is None
