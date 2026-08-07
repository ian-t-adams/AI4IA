"""Span-level citation provenance: server-minted source ids and their receipt.

Audit finding **P1-14** ("citations are presentation, not provenance"): a
citation used to be a claim the model made, not a receipt. Retrieval already
computed exactly the grounding a receipt needs — document id, filename, heading,
character or time range — and then threw it away during prompt assembly, so by
the time an answer was rendered a correct citation and a fabricated one were
byte-identical to the system.

This module closes the span-level half of that gap, and is deliberate about
where it stops.

**What is airtight here.** Every excerpt injected into a turn is minted a
server-owned id (``S1``, ``S2``, …) together with a SHA-256 of the exact text
injected under that id and the timestamp it was retrieved at. That registry is
persisted on the assistant message (:class:`RetrievedSource`), so the set of
sources an answer *could* have used is a durable, server-authored fact rather
than something re-derived later from a mutable index. Verification then asks one
question with a yes/no answer: does the id the model cited appear in this turn's
registry? An id that was never injected is caught with certainty, because the
registry is minted before the model sees anything and the browser never
contributes to it.

**What is NOT claimed.** ``verified`` means *this id was retrieved for this
turn*. It does **not** mean the cited span supports the sentence it is attached
to. Checking that is claim-level entailment; anything cheap enough to run inline
(lexical overlap, embedding similarity) is approximate, and an approximate check
presented as a guarantee is worse than none — so no such signal is computed or
rendered. What the reader gets instead is the receipt itself: the excerpt is
persisted verbatim beside the answer, so support is a judgement the reader can
make against the real text rather than one the app pretends to have made.

Scope: library Tier-2 retrieval excerpts (see
:mod:`ai4ia_api.library.retrieval`). Web-search results, recalled memory, and
session-uploaded documents are still cited as prose and are not attested here.

Both the token grammar and the verification rule are mirrored in
``app/web/src/lib/citations.ts``; they must stay in step.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, Field

# Hard caps. A hostile document cannot inflate a message document past these:
# the registry is bounded by retrieval's top-k, and each stored excerpt is a
# display/audit sample, not a copy of the chunk.
MAX_SOURCES = 24
MAX_EXCERPT_CHARS = 320
MAX_CITATIONS = 64
# Longest raw citation payload retained for the audit trail when a token cannot
# be resolved. Bounded because the payload is model output.
MAX_RAW_CHARS = 120

# One or more span ids inside a single token: ``[[cite:S1]]``, ``[[cite:S1,S3]]``.
# Models do group ids, so the multi-id form is parsed rather than left to fall
# through as literal text -- an unparsed token is an unchecked token, which is
# exactly the failure this module exists to remove.
_SPAN_ID = r"[sS]\d{1,3}"
CITATION_RE = re.compile(
    rf"\[\[cite:\s*({_SPAN_ID}(?:\s*[,;]\s*{_SPAN_ID})*)\s*\]\]"
)
# Catch-all for anything else shaped like a citation token (a legacy
# ``FILENAME@MM:SS`` token, an invented id format, a truncated payload). These
# resolve to nothing, so on an attested turn they are recorded as unverified
# rather than silently rendered as text.
ANY_CITATION_RE = re.compile(r"\[\[cite:([^\]\n]{0,200})\]\]")
_ID_SPLIT_RE = re.compile(r"[,;]")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CitationStatus(str, Enum):
    """Outcome of the one question this module can answer with certainty."""

    # The cited id is in this turn's server-minted registry.
    verified = "verified"
    # The answer cited something that was never injected this turn: an invented
    # id, or a token that names no id at all.
    unverified = "unverified"


class RetrievedSource(BaseModel):
    """One retrieved span, as it was injected into a turn.

    The receipt: ``contentSha256`` is taken over the exact text the model was
    shown under ``spanId``, and ``excerpt`` keeps a bounded verbatim sample of
    it. Both are deliberately immutable copies. Document chunks and search
    indexes stay rebuildable (they are derived state), which is precisely why a
    provenance record cannot point at them and still mean anything later: the
    span a six-month-old answer cited may have been re-chunked or deleted.
    """

    spanId: str
    documentId: str
    # Display label only. Identity is ``documentId`` -- resolving a citation by
    # filename is what let the first case-insensitive duplicate win.
    filename: str
    heading: str | None = None
    charStart: int | None = None
    charEnd: int | None = None
    # Audio/video grounding, when the span came from a timed transcript.
    startMs: int | None = None
    endMs: int | None = None
    speaker: str | None = None
    excerpt: str = ""
    # True when ``excerpt`` is shorter than the text actually injected.
    excerptTruncated: bool = False
    # SHA-256 of the FULL injected text, not of ``excerpt``.
    contentSha256: str = ""
    retrievedAt: datetime = Field(default_factory=_now)
    # Retrieval score, when the store reported one. Ranking evidence, not proof.
    score: float | None = None


class MessageCitation(BaseModel):
    """One citation the answer actually made, and what became of it."""

    spanId: str
    status: CitationStatus
    # Resolved from the registry; None when the citation is unverified.
    documentId: str | None = None
    filename: str | None = None
    startMs: int | None = None
    # How many times the answer cited this id.
    occurrences: int = 1
    # What the model literally wrote, when it did not resolve. Kept so an
    # unverified citation is auditable rather than merely counted.
    raw: str | None = None


class AttestableMessage(Protocol):
    """The two fields :func:`attest_message` needs.

    Structural on purpose: ``sessions.models.Message`` satisfies it without this
    module importing it, so the persisted-message model keeps depending on the
    citation vocabulary and not the other way round.
    """

    content: str
    sources: list[RetrievedSource] | None
    citations: list[MessageCitation] | None


def content_digest(text: str) -> str:
    """SHA-256 of the exact text injected under a span id."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SpanRegistry:
    """Mints the server-owned span ids for one turn.

    Ids are turn-scoped (``S1`` onwards, in injection order) and minted *before*
    the model runs, so nothing the model or the browser produces can add to the
    registry afterwards. Short by design: the id is spent from the same token
    budget as the excerpt it labels.
    """

    def __init__(self, *, limit: int = MAX_SOURCES) -> None:
        self._limit = max(0, limit)
        self._sources: list[RetrievedSource] = []

    def __len__(self) -> int:
        return len(self._sources)

    @property
    def full(self) -> bool:
        return len(self._sources) >= self._limit

    def mint(
        self,
        *,
        document_id: str,
        filename: str,
        content: str,
        heading: str | None = None,
        char_start: int | None = None,
        char_end: int | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        speaker: str | None = None,
        score: float | None = None,
    ) -> RetrievedSource | None:
        """Register one injected span and return its record, or ``None`` when the
        per-turn cap is already reached (the caller then injects nothing, so an
        unlabelled excerpt can never reach the model)."""
        if self.full:
            return None
        source = RetrievedSource(
            spanId=f"S{len(self._sources) + 1}",
            documentId=document_id,
            filename=filename,
            heading=heading,
            charStart=char_start,
            charEnd=char_end,
            startMs=start_ms,
            endMs=end_ms,
            speaker=speaker,
            excerpt=content[:MAX_EXCERPT_CHARS],
            excerptTruncated=len(content) > MAX_EXCERPT_CHARS,
            contentSha256=content_digest(content),
            score=score,
        )
        self._sources.append(source)
        return source

    def sources(self) -> list[RetrievedSource]:
        """A copy of the registry, in injection order."""
        return list(self._sources)


def normalize_span_id(value: str) -> str:
    """Canonical form of a span id as written by the model (``s3`` -> ``S3``)."""
    return value.strip().upper()


def parse_citation_tokens(text: str) -> list[tuple[str | None, str]]:
    """Every ``[[cite:…]]`` token in ``text``, in order.

    Each entry is ``(span_id, raw_payload)``; ``span_id`` is ``None`` for a token
    whose payload names no well-formed id. A grouped token (``[[cite:S1,S3]]``)
    yields one entry per id, because each id is a separate claim to check.
    """
    if not text or "[[cite:" not in text:
        return []
    out: list[tuple[str | None, str]] = []
    for match in ANY_CITATION_RE.finditer(text):
        raw = match.group(0)
        payload = match.group(1)
        if CITATION_RE.fullmatch(raw):
            for part in _ID_SPLIT_RE.split(payload):
                out.append((normalize_span_id(part), raw[:MAX_RAW_CHARS]))
        else:
            out.append((None, raw[:MAX_RAW_CHARS]))
        if len(out) >= MAX_CITATIONS:
            break
    return out


def verify_citations(
    text: str, sources: Sequence[RetrievedSource] | None
) -> list[MessageCitation]:
    """Check every citation in ``text`` against this turn's minted registry.

    Returns ``[]`` when ``sources`` is ``None`` -- a turn with no registry (the
    library was off, retrieval returned nothing, or the message predates this
    feature) has nothing to verify *against*, and claiming otherwise would label
    citations unverified on the strength of missing evidence. An empty list of
    sources is different and is honoured as such: retrieval ran and offered
    nothing, so any citation in the answer is unverified.
    """
    if sources is None:
        return []
    by_id = {source.spanId: source for source in sources}
    ordered: list[MessageCitation] = []
    seen: dict[str, MessageCitation] = {}
    for span_id, raw in parse_citation_tokens(text):
        source = by_id.get(span_id) if span_id else None
        key = span_id if span_id else f"?{raw}"
        existing = seen.get(key)
        if existing is not None:
            existing.occurrences += 1
            continue
        citation = MessageCitation(
            spanId=span_id or "",
            status=(
                CitationStatus.verified if source else CitationStatus.unverified
            ),
            documentId=source.documentId if source else None,
            filename=source.filename if source else None,
            startMs=source.startMs if source else None,
            raw=None if source else raw,
        )
        seen[key] = citation
        ordered.append(citation)
    return ordered


def attest_message(message: AttestableMessage) -> AttestableMessage:
    """Bind an assistant message's citations to its own span registry.

    Called immediately before the message is persisted, once its text is final.
    Idempotent, so a path that attests twice is harmless, and safe on a message
    with no registry (it stays unattested rather than becoming wrongly suspect).
    """
    sources = message.sources
    if sources is None:
        message.citations = None
        return message
    citations = verify_citations(message.content, sources)
    message.citations = citations or None
    return message


def unverified_count(citations: Iterable[MessageCitation] | None) -> int:
    """How many distinct citations named a span this turn never retrieved."""
    if not citations:
        return 0
    return sum(1 for c in citations if c.status is CitationStatus.unverified)
