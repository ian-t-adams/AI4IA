"""Deterministic, heading-aware Markdown chunker for retrieval.

Content Understanding returns Markdown; we split it into bounded, overlapping
chunks for embedding into the ``doc_chunks`` vector store. The splitter packs
whole paragraphs up to ``max_chars`` (hard-wrapping any single oversized
paragraph), tracks the nearest preceding heading for grounding, and prepends a
small ``overlap`` tail from the previous chunk so a span that straddles a boundary
is still retrievable. Pure and deterministic — same input always yields the same
chunks — so it is exercised entirely by unit tests.

The :func:`chunk_audiovisual` helper is the time-grounded analogue for audio /
video: Content Understanding returns ``contents[]`` segments carrying
``transcriptPhrases`` (speaker + start/end ms). We pack phrases up to
``max_chars`` and ground each chunk on its time span + speaker so retrieval can
cite ``mm:ss`` ranges and the UI can deep-link back into the media. When a
segment has no phrases we fall back to chunking its Markdown, stamped with the
segment's coarse start/end ms — so the chunker is correct whether or not the
analyzer was configured to return phrase-level detail.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Chunk:
    index: int
    text: str
    # Document chunks: {"charStart": int, "charEnd": int, "heading": str | None}.
    # Audio/video chunks: {"startMs": int | None, "endMs": int | None,
    # "speaker": str | None, "segment": int | None}. The primary (non-overlap)
    # span/time-range in the source, for citations/deep-linking.
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

    # Step 1: build primary spans (start, end, heading), packing paragraphs up to
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

    # Step 2: materialize chunks, prepending the previous chunk's overlap tail.
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


def format_timestamp(ms: int | None) -> str:
    """Render a millisecond offset as a compact ``M:SS`` (or ``H:MM:SS``) label.

    ``None``, non-integer, or negative inputs render as ``""`` so callers can
    unconditionally format a (possibly absent) grounding time. Used for the
    time-grounded audio/video citations in retrieval.
    """
    total = _as_int_ms(ms)
    if total is None or total < 0:
        return ""
    total //= 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def chunk_audiovisual(
    contents: Sequence[Any], *, max_chars: int = 1200, overlap: int = 150
) -> list[Chunk]:
    """Time-grounded chunking for audio/video Content Understanding output.

    ``contents`` is ``CUResult.contents`` — one dict per analyzed segment. For a
    segment with ``transcriptPhrases`` we pack phrase texts up to ``max_chars``,
    grounding each chunk on its first phrase's ``startTimeMs``, its last phrase's
    ``endTimeMs``, and the speaker when the packed phrases share one. A phrase
    longer than ``max_chars`` is hard-wrapped (keeping its time/speaker). A
    segment with no phrases falls back to :func:`chunk_markdown` of its Markdown,
    stamped with the segment's coarse start/end ms. Returns ``[]`` when nothing is
    groundable so the caller can fall back to chunking the concatenated Markdown.

    Pure and deterministic. ``segment`` grounding is the 0-based content index, or
    ``None`` when there is a single segment.
    """
    if not contents:
        return []
    max_chars = max(1, int(max_chars))
    overlap = max(0, min(int(overlap), max_chars - 1))
    multi = len(contents) > 1
    chunks: list[Chunk] = []
    for seg_idx, content in enumerate(contents):
        if not isinstance(content, dict):
            continue
        segment = seg_idx if multi else None
        phrases = _valid_phrases(content.get("transcriptPhrases"))
        prev_primary = ""
        if phrases:
            for primary, start_ms, end_ms, speaker in _pack_phrases(
                phrases, max_chars=max_chars
            ):
                prev_primary = _emit_av(
                    chunks,
                    primary,
                    prev_primary,
                    overlap,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    speaker=speaker,
                    segment=segment,
                )
            continue
        md = content.get("markdown")
        if isinstance(md, str) and md.strip():
            seg_start = _as_int_ms(content.get("startTimeMs"))
            seg_end = _as_int_ms(content.get("endTimeMs"))
            for mc in chunk_markdown(md, max_chars=max_chars, overlap=overlap):
                grounding = dict(mc.grounding)
                grounding.update(
                    startMs=seg_start, endMs=seg_end, speaker=None, segment=segment
                )
                chunks.append(Chunk(index=len(chunks), text=mc.text, grounding=grounding))
    return chunks


# Cap markers per segment so a pathological analyzer response can't produce an
# unbounded sidecar; deep-linking only needs scene/shot boundaries, not every frame.
_MAX_MARKERS_PER_SEGMENT = 2000


def _marker_times(value: Any) -> list[int]:
    """Sorted, de-duplicated, non-negative millisecond markers from a CU time list.

    Accepts a ``keyFrameTimesMs`` / ``cameraShotTimesMs`` array; coerces each entry
    via :func:`_as_int_ms`, drops ``None``/negative values, sorts + de-duplicates,
    and caps the count. Returns ``[]`` for any non-list input.
    """
    if not isinstance(value, list):
        return []
    seen: set[int] = set()
    for item in value:
        ms = _as_int_ms(item)
        if ms is not None and ms >= 0:
            seen.add(ms)
    out = sorted(seen)
    if len(out) > _MAX_MARKERS_PER_SEGMENT:
        out = out[:_MAX_MARKERS_PER_SEGMENT]
    return out


def media_timeline(contents: Sequence[Any]) -> dict[str, Any]:
    """Extract a deep-link scene timeline from audio/video CU ``contents[]``.

    Surfaces each segment's time span plus the analyzer's ``keyFrameTimesMs`` and
    ``cameraShotTimesMs`` (keyframe / scene surfacing) so the UI
    can deep-link a media player to scene boundaries. Returns::

        {"durationMs": int | None,
         "segments": [{"index": int, "startMs": int | None, "endMs": int | None,
                       "keyframes": [int, ...], "shots": [int, ...]}, ...]}

    Only segments carrying a usable span or any marker are included, so the timeline
    is empty (``segments == []``) when nothing is groundable and the caller can skip
    persisting a sidecar. Pure and deterministic — presentation metadata for
    deep-linking, not retrieval grounding.
    """
    segments: list[dict[str, Any]] = []
    duration: int | None = None
    for idx, content in enumerate(contents or []):
        if not isinstance(content, dict):
            continue
        start_ms = _as_int_ms(content.get("startTimeMs"))
        end_ms = _as_int_ms(content.get("endTimeMs"))
        keyframes = _marker_times(content.get("keyFrameTimesMs"))
        shots = _marker_times(content.get("cameraShotTimesMs"))
        if start_ms is None and end_ms is None and not keyframes and not shots:
            continue
        segments.append(
            {
                "index": idx,
                "startMs": start_ms,
                "endMs": end_ms,
                "keyframes": keyframes,
                "shots": shots,
            }
        )
        for candidate in (
            end_ms,
            start_ms,
            keyframes[-1] if keyframes else None,
            shots[-1] if shots else None,
        ):
            if candidate is not None and (duration is None or candidate > duration):
                duration = candidate
    return {"durationMs": duration, "segments": segments}


def _as_int_ms(value: Any) -> int | None:
    """Coerce a CU millisecond value to ``int`` (ints + integer floats only)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _valid_phrases(value: Any) -> list[dict[str, Any]]:
    """Transcript-phrase dicts carrying non-empty text (others are dropped)."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for phrase in value:
        if (
            isinstance(phrase, dict)
            and isinstance(phrase.get("text"), str)
            and phrase["text"].strip()
        ):
            out.append(phrase)
    return out


def _phrase_speaker(phrase: dict[str, Any]) -> str | None:
    speaker = phrase.get("speaker")
    if isinstance(speaker, str) and speaker.strip():
        return speaker.strip()
    return None


def _pack_phrases(
    phrases: list[dict[str, Any]], *, max_chars: int
) -> list[tuple[str, int | None, int | None, str | None]]:
    """Group phrases into ``<= max_chars`` primary spans.

    Yields ``(text, start_ms, end_ms, speaker)`` tuples; ``speaker`` is set only
    when every packed phrase shares it. An oversized single phrase is emitted as
    consecutive hard-wrapped spans that all keep that phrase's time + speaker.
    """
    out: list[tuple[str, int | None, int | None, str | None]] = []
    texts: list[str] = []
    start_ms: int | None = None
    end_ms: int | None = None
    speakers: set[str] = set()
    length = 0

    def flush() -> None:
        nonlocal texts, start_ms, end_ms, speakers, length
        if texts:
            speaker = next(iter(speakers)) if len(speakers) == 1 else None
            out.append((" ".join(texts), start_ms, end_ms, speaker))
        texts = []
        start_ms = end_ms = None
        speakers = set()
        length = 0

    for phrase in phrases:
        text = phrase["text"].strip()
        if not text:
            continue
        p_start = _as_int_ms(phrase.get("startTimeMs"))
        p_end = _as_int_ms(phrase.get("endTimeMs"))
        p_speaker = _phrase_speaker(phrase)
        if len(text) > max_chars:
            flush()
            for pos in range(0, len(text), max_chars):
                out.append((text[pos : pos + max_chars], p_start, p_end, p_speaker))
            continue
        sep = 1 if texts else 0
        if texts and length + sep + len(text) > max_chars:
            flush()
            sep = 0
        if not texts:
            start_ms = p_start
        if p_end is not None:
            end_ms = p_end
        texts.append(text)
        length += sep + len(text)
        if p_speaker:
            speakers.add(p_speaker)
    flush()
    return out


def _emit_av(
    chunks: list[Chunk],
    primary: str,
    prev_primary: str,
    overlap: int,
    *,
    start_ms: int | None,
    end_ms: int | None,
    speaker: str | None,
    segment: int | None,
) -> str:
    """Append one time-grounded chunk, prepending the previous primary's overlap
    tail (mirroring :func:`chunk_markdown`). Returns the new ``prev_primary``."""
    primary = (primary or "").strip()
    if not primary:
        return prev_primary
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
            grounding={
                "startMs": start_ms,
                "endMs": end_ms,
                "speaker": speaker,
                "segment": segment,
            },
        )
    )
    return primary
