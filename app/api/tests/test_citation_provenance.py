"""Span-level citation provenance (audit P1-14).

The point of every test here is the pair: a fabricated citation must be
*demonstrably* caught, and the byte-identical answer with a real span id must
pass. A verifier that flagged everything would satisfy the first half alone and
prove nothing, so each catching test has a passing control beside it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai4ia_api.citations import (
    MAX_EXCERPT_CHARS,
    MAX_SOURCES,
    CitationStatus,
    MessageCitation,
    RetrievedSource,
    SpanRegistry,
    attest_message,
    content_digest,
    parse_citation_tokens,
    unverified_count,
    verify_citations,
)
from ai4ia_api.library.blob_store import InMemoryBlobStore
from ai4ia_api.library.doc_chunks import DocChunkRecord, InMemoryDocChunkStore
from ai4ia_api.library.memory_repo import InMemoryDocumentLibraryRepository
from ai4ia_api.library.models import DocumentStatus, UserDocument
from ai4ia_api.library.retrieval import DocumentRetrievalService
from ai4ia_api.sessions.models import Message, MessageRole
from tests.conftest import make_settings


class FakeEmbedder:
    def __init__(self, vector=None) -> None:
        self._vector = list(vector or [1.0, 0.0, 0.0])

    async def embed(self, inputs):
        return [list(self._vector) for _ in inputs]

    async def embed_one(self, text):
        return list(self._vector)


def _service(*, library, blob, chunks=None, embedder=None, **overrides):
    return DocumentRetrievalService(
        library=library,
        blob_store=blob,
        chunk_store=chunks,
        embedder=embedder,
        settings=make_settings(document_understanding_enabled=True, **overrides),
    )


async def _seed(library, *, user="u1", filename="report.pdf", summary="Q3 filing"):
    doc = UserDocument(
        userId=user, filename=filename, status=DocumentStatus.ready, summary=summary
    )
    await library.create_document(doc)
    return doc


async def _chunk(chunks, doc, *, content, index=0, **kwargs):
    rec = DocChunkRecord(
        user_id=doc.userId,
        document_id=doc.id,
        chunk_index=index,
        content=content,
        **kwargs,
    )
    await chunks.add_many([rec], [[1.0, 0.0, 0.0]])
    return rec


def _source(span_id="S1", **kwargs) -> RetrievedSource:
    base = {
        "spanId": span_id,
        "documentId": "doc-1",
        "filename": "report.pdf",
        "excerpt": "Revenue grew twenty percent.",
        "contentSha256": content_digest("Revenue grew twenty percent."),
    }
    base.update(kwargs)
    return RetrievedSource(**base)


def _assistant(content: str, sources) -> Message:
    return Message(
        sessionId="s1", userId="u1", role=MessageRole.assistant, content=content,
        sources=sources,
    )


# --- The registry is server-minted, ordered, and hashes what it injected ------


def test_registry_mints_ordered_ids_and_hashes_the_injected_text():
    registry = SpanRegistry()
    first = registry.mint(document_id="d1", filename="a.pdf", content="alpha")
    second = registry.mint(document_id="d2", filename="b.pdf", content="beta")

    assert first is not None and second is not None
    assert [s.spanId for s in registry.sources()] == ["S1", "S2"]
    # The receipt is over the exact injected text, so a later re-chunk or edit of
    # the document cannot silently redefine what an old answer cited.
    assert first.contentSha256 == content_digest("alpha")
    assert first.contentSha256 != second.contentSha256


def test_registry_stores_a_bounded_excerpt_and_flags_the_truncation():
    registry = SpanRegistry()
    long_text = "x" * (MAX_EXCERPT_CHARS + 50)
    source = registry.mint(document_id="d1", filename="a.pdf", content=long_text)

    assert source is not None
    assert len(source.excerpt) == MAX_EXCERPT_CHARS
    assert source.excerptTruncated is True
    # The hash covers the FULL injected text, not the stored sample.
    assert source.contentSha256 == content_digest(long_text)


def test_registry_refuses_to_mint_past_the_cap():
    registry = SpanRegistry(limit=2)
    assert registry.mint(document_id="d", filename="f", content="a") is not None
    assert registry.mint(document_id="d", filename="f", content="b") is not None
    assert registry.full is True
    # None, not a silent extra id: the caller must drop the excerpt entirely.
    assert registry.mint(document_id="d", filename="f", content="c") is None
    assert len(registry.sources()) == 2


def test_default_registry_cap_matches_the_declared_maximum():
    registry = SpanRegistry()
    for i in range(MAX_SOURCES):
        assert registry.mint(document_id="d", filename="f", content=str(i)) is not None
    assert registry.mint(document_id="d", filename="f", content="over") is None


# --- Token parsing: an unparsed token would be an unchecked token -------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("See [[cite:S1]].", ["S1"]),
        ("lower [[cite:s2]] case", ["S2"]),
        ("padded [[cite: S3 ]]", ["S3"]),
        ("grouped [[cite:S1,S3]]", ["S1", "S3"]),
        ("semicolons [[cite:S1; S2]]", ["S1", "S2"]),
        ("twice [[cite:S1]] and [[cite:S2]]", ["S1", "S2"]),
    ],
)
def test_parses_every_well_formed_span_token(text, expected):
    assert [span for span, _ in parse_citation_tokens(text)] == expected


def test_grouped_token_is_split_so_one_bad_id_cannot_hide_behind_a_good_one():
    citations = verify_citations("both [[cite:S1,S9]]", [_source("S1")])

    assert [(c.spanId, c.status) for c in citations] == [
        ("S1", CitationStatus.verified),
        ("S9", CitationStatus.unverified),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "legacy [[cite:lecture.mp3@2:13]]",
        "invented [[cite:source 4]]",
        "empty [[cite:]]",
    ],
)
def test_unresolvable_token_shapes_are_recorded_not_ignored(text):
    # A token the parser cannot turn into an id is not silently rendered as
    # text -- that would be an unchecked citation, which is the whole defect.
    parsed = parse_citation_tokens(text)
    assert parsed and parsed[0][0] is None

    citations = verify_citations(text, [_source("S1")])
    assert len(citations) == 1
    assert citations[0].status is CitationStatus.unverified
    assert citations[0].raw is not None


def test_text_without_any_token_parses_to_nothing():
    assert parse_citation_tokens("No citations at all here.") == []
    assert verify_citations("No citations at all here.", [_source()]) == []


# --- Verification: the non-vacuity pair ---------------------------------------


def test_real_citation_verifies_and_the_same_answer_with_a_fake_id_does_not():
    sources = [_source("S1", documentId="doc-7", filename="q3.pdf")]
    answer = "Revenue grew twenty percent [[cite:{id}]]."

    real = verify_citations(answer.format(id="S1"), sources)
    fake = verify_citations(answer.format(id="S4"), sources)

    # Same sentence, same shape, opposite verdicts -- the control that proves the
    # check is reading the registry and not just counting tokens.
    assert [c.status for c in real] == [CitationStatus.verified]
    assert real[0].documentId == "doc-7"
    assert real[0].filename == "q3.pdf"
    assert [c.status for c in fake] == [CitationStatus.unverified]
    assert fake[0].documentId is None
    assert unverified_count(real) == 0
    assert unverified_count(fake) == 1


def test_repeated_citation_of_one_span_is_counted_once_with_occurrences():
    citations = verify_citations("[[cite:S1]] and again [[cite:S1]]", [_source("S1")])

    assert len(citations) == 1
    assert citations[0].occurrences == 2


def test_an_empty_registry_is_evidence_and_a_missing_one_is_not():
    # Retrieval ran and injected nothing: a cited id is provably fabricated.
    assert verify_citations("[[cite:S1]]", [])[0].status is CitationStatus.unverified
    # Retrieval never ran: there is nothing to check against, so nothing is
    # claimed either way. Marking this unverified would be an accusation built
    # out of missing evidence.
    assert verify_citations("[[cite:S1]]", None) == []


def test_media_span_carries_its_own_document_id_and_offset():
    sources = [
        _source("S1", documentId="dup-a", filename="lecture.mp3", startMs=133000),
        _source("S2", documentId="dup-b", filename="lecture.mp3", startMs=900000),
    ]

    citations = verify_citations("later [[cite:S2]]", sources)

    # Two ready documents share a filename; the citation still resolves to
    # exactly one of them, which filename matching could not do.
    assert citations[0].documentId == "dup-b"
    assert citations[0].startMs == 900000


# --- attest_message: what actually lands on the persisted row ------------------


def test_attest_stamps_citations_onto_an_attested_message():
    message = _assistant("Grew twenty percent [[cite:S1]].", [_source("S1")])

    attest_message(message)

    assert message.citations is not None
    assert message.citations[0].status is CitationStatus.verified


def test_attest_leaves_an_unattested_turn_alone():
    message = _assistant("Grew twenty percent [[cite:S1]].", None)

    attest_message(message)

    # No registry means no verdict -- an old row must not start rendering as
    # suspect just because the feature shipped.
    assert message.citations is None


def test_attest_is_idempotent_so_a_double_call_cannot_double_count():
    message = _assistant("[[cite:S1]] [[cite:S1]]", [_source("S1")])

    attest_message(message)
    attest_message(message)

    assert message.citations is not None
    assert len(message.citations) == 1
    assert message.citations[0].occurrences == 2


def test_attest_clears_a_stale_verdict_when_the_registry_disappears():
    message = _assistant("[[cite:S1]]", None)
    message.citations = [MessageCitation(spanId="S1", status=CitationStatus.verified)]

    attest_message(message)

    assert message.citations is None


def test_message_round_trips_provenance_through_serialization():
    message = _assistant("Grew [[cite:S1]].", [_source("S1")])
    attest_message(message)

    revived = Message.model_validate(message.model_dump(mode="json"))

    assert revived.sources is not None
    assert revived.sources[0].contentSha256 == content_digest(
        "Revenue grew twenty percent."
    )
    assert revived.citations is not None
    assert revived.citations[0].status is CitationStatus.verified


def test_a_message_written_before_the_feature_still_deserializes():
    # Additive + optional: no Cosmos migration. A stored document with neither
    # field must load, and must load as unattested rather than as empty.
    legacy = {
        "id": "m1",
        "sessionId": "s1",
        "userId": "u1",
        "role": "assistant",
        "content": "Listen [[cite:lecture.mp3@12:34]] here",
    }

    revived = Message.model_validate(legacy)

    assert revived.sources is None
    assert revived.citations is None


# --- Retrieval mints the registry the verifier later checks against -----------


async def test_retrieval_returns_a_registry_matching_the_injected_excerpts():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore()
    svc = _service(library=library, blob=blob, chunks=chunks, embedder=FakeEmbedder())
    doc = await _seed(library)
    await _chunk(
        chunks, doc, content="Revenue grew twenty percent.", heading="Results",
        char_start=0, char_end=28,
    )

    built = await svc.context("u1", "revenue?", nonce="n1")

    assert len(built.sources) == 1
    source = built.sources[0]
    assert source.spanId == "S1"
    assert source.documentId == doc.id
    assert source.filename == "report.pdf"
    assert source.heading == "Results"
    assert source.charStart == 0 and source.charEnd == 28
    # The id in the registry is the id the model is shown, or verification would
    # reject every honest citation.
    assert f"cite-as: [[cite:{source.spanId}]]" in built.block
    assert source.contentSha256 == content_digest("Revenue grew twenty percent.")


async def test_retrieved_span_hash_matches_the_text_actually_in_the_prompt():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore()
    svc = _service(library=library, blob=blob, chunks=chunks, embedder=FakeEmbedder())
    doc = await _seed(library)
    body = "The filing states revenue grew twenty percent year over year."
    await _chunk(chunks, doc, content=body)

    built = await svc.context("u1", "revenue?", nonce="n1")

    assert body in built.block
    assert built.sources[0].contentSha256 == content_digest(body)


async def test_an_answer_citing_a_retrieved_span_verifies_end_to_end():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore()
    svc = _service(library=library, blob=blob, chunks=chunks, embedder=FakeEmbedder())
    doc = await _seed(library)
    await _chunk(chunks, doc, content="Revenue grew twenty percent.")

    built = await svc.context("u1", "revenue?", nonce="n1")
    real = _assistant("Revenue grew twenty percent [[cite:S1]].", built.sources)
    fabricated = _assistant("Revenue fell [[cite:S2]].", built.sources)
    attest_message(real)
    attest_message(fabricated)

    assert real.citations is not None
    assert real.citations[0].status is CitationStatus.verified
    assert real.citations[0].documentId == doc.id
    assert fabricated.citations is not None
    assert fabricated.citations[0].status is CitationStatus.unverified


async def test_retrieval_with_no_excerpts_still_reports_an_empty_registry():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    svc = _service(library=library, blob=blob)
    await _seed(library)

    built = await svc.context("u1", "anything", nonce="n1")

    # Tier-1 cards only: the block exists, so the turn is attestable and any
    # cited span id in the answer is provably fabricated.
    assert built.block
    assert built.sources == []


async def test_empty_library_yields_no_block_and_no_registry():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    svc = _service(library=library, blob=blob)

    built = await svc.context("u1", "anything", nonce="n1")

    assert built.block == ""
    assert built.sources == []


async def test_span_ids_are_unique_across_documents_in_one_turn():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore()
    svc = _service(library=library, blob=blob, chunks=chunks, embedder=FakeEmbedder())
    first = await _seed(library, filename="a.pdf")
    second = await _seed(library, filename="b.pdf")
    await _chunk(chunks, first, content="Alpha body text.")
    await _chunk(chunks, second, content="Beta body text.")

    built = await svc.context("u1", "text?", nonce="n1")

    ids = [s.spanId for s in built.sources]
    assert len(ids) == len(set(ids)) == 2
    assert {s.documentId for s in built.sources} == {first.id, second.id}


async def test_no_excerpt_is_injected_without_an_id_when_the_registry_is_full():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore()
    svc = _service(
        library=library, blob=blob, chunks=chunks, embedder=FakeEmbedder(),
        document_retrieval_top_k=MAX_SOURCES + 4,
    )
    doc = await _seed(library)
    for i in range(MAX_SOURCES + 4):
        await _chunk(chunks, doc, content=f"Body number {i}.", index=i)

    built = await svc.context("u1", "body?", nonce="n1")

    # Every excerpt in the prompt carries an id, and the count matches the
    # registry exactly -- an unlabelled excerpt would be usable and unattestable.
    assert len(built.sources) == MAX_SOURCES
    assert built.block.count("cite-as: [[cite:") == MAX_SOURCES


# --- The shared contract with the web parser ---------------------------------
#
# The grammar and the verification rule are implemented twice, here and in
# ``app/web/src/lib/citations.ts``. Two implementations that quietly disagree
# would show a reader a green chip for a citation this side recorded as
# fabricated, so both suites assert the same committed case table. A grammar
# change on one side alone fails that side's CI job.

_CONTRACT_PATH = Path(__file__).with_name("citation_contract.json")
_CONTRACT = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))


def test_shared_contract_is_not_vacuous():
    cases = _CONTRACT["cases"]
    statuses = {
        item["status"] for case in cases for item in case["expect"]
    }
    # A table that only ever expected one verdict would make the parity claim a
    # statement about nothing.
    assert statuses == {"verified", "unverified"}
    assert any(case["attested"] for case in cases)
    assert any(not case["attested"] for case in cases)
    assert len(cases) >= 10


@pytest.mark.parametrize("case", _CONTRACT["cases"], ids=lambda c: c["text"])
def test_api_matches_the_shared_citation_contract(case):
    sources = [RetrievedSource(**item) for item in _CONTRACT["registry"]]
    citations = verify_citations(
        case["text"], sources if case["attested"] else None
    )

    assert [
        {
            "spanId": c.spanId or None,
            "status": c.status.value,
            "documentId": c.documentId,
        }
        for c in citations
    ] == case["expect"]
