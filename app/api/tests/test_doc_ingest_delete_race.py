"""Delete-during-enrich must not resurrect a deleted document.

When document understanding is enabled + CU configured, a long-running ``enrich``
runs concurrently with the request loop. If the user deletes the document while
the crack is in flight, the delete must win on BOTH resurrection vectors:

1. the manifest (Cosmos/in-memory ``update_document``), and
2. the storage/index artifacts (blob ``parsed.md``/``chunks.jsonl`` + pgvector
   rows, written directly by ``_persist_enrichment``, bypassing the repo).

These tests drive the in-memory repo/blob/chunk stores and inject fakes that
delete the manifest at each critical point (during the CU poll and during
persistence), asserting that nothing is left behind. They also cover the
startup recovery sweep, the chunk cap + batched embed, and the tracked-task
schedule/cancel path the router uses.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ai4ia_api.content_understanding.models import CUResult
from ai4ia_api.library.blob_store import InMemoryBlobStore, document_prefix
from ai4ia_api.library.doc_chunks import InMemoryDocChunkStore
from ai4ia_api.library.ingest import (
    MAX_CONCURRENT_DOCUMENT_ENRICHMENTS,
    DocumentIngestor,
    EnrichScheduleOutcome,
)
from ai4ia_api.library.memory_repo import InMemoryDocumentLibraryRepository
from ai4ia_api.library.models import (
    DocumentAnnotation,
    DocumentStatus,
    UserDocument,
    Visibility,
)
from ai4ia_api.library.repository import DocumentNotFoundError
from tests.conftest import make_settings

_QUERY = [1.0, 1.0, 0.0]


class _Usage:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def record_completion(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _Embedder:
    def __init__(self, on_embed=None) -> None:
        self.on_embed = on_embed
        self.embedded: list[str] = []
        self.calls = 0

    async def embed(self, inputs):
        self.calls += 1
        self.embedded.extend(inputs)
        if self.on_embed is not None:
            await self.on_embed()
        return [[float(len(t) % 5), 1.0, 0.0] for t in inputs]


class _CU:
    def __init__(self, markdown: str, on_analyze=None) -> None:
        self._markdown = markdown
        self.on_analyze = on_analyze

    async def analyze(self, analyzer_id, data, content_type, *, api_version=None):
        if self.on_analyze is not None:
            await self.on_analyze()
        return CUResult(
            status="Succeeded", analyzer_id="prebuilt-documentSearch", markdown=self._markdown
        )


class _BlockingCU:
    """``analyze`` blocks until released (or cancelled) — models a long poll."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def analyze(self, analyzer_id, data, content_type, *, api_version=None):
        self.started.set()
        await self.release.wait()
        return CUResult(status="Succeeded", analyzer_id="a", markdown="# x\n\nbody")


def _build(*, cu, embedder=None, chunks=None, library=None, blob=None, usage=None, **over):
    settings = make_settings(
        document_understanding_enabled=True,
        document_chunk_chars=40,
        document_chunk_overlap=5,
        **over,
    )
    return DocumentIngestor(
        library=library or InMemoryDocumentLibraryRepository(),
        blob_store=blob or InMemoryBlobStore(),
        settings=settings,
        usage=usage or _Usage(),
        cu_client=cu,
        embedder=embedder,
        chunk_store=chunks,
    )


def _blob_keys(blob: InMemoryBlobStore, user_id: str, doc_id: str) -> list[str]:
    prefix = document_prefix(user_id, doc_id)
    return [k for k in blob._data if k.startswith(prefix)]


async def test_delete_during_cu_poll_does_not_resurrect():
    """Vector 1: delete lands while CU is analyzing → pre-persist re-check aborts."""
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore(expected_dim=3)
    embedder = _Embedder()
    cu = _CU("# T\n\n" + ("word " * 30))
    ingestor = _build(
        cu=cu, embedder=embedder, chunks=chunks, library=library, blob=blob
    )

    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"BYTES"
    )
    doc_id = stored.document.id

    async def delete_now() -> None:
        # Models the DELETE handler: remove manifest, then purge artifacts.
        await library.delete_document("u1", doc_id)
        await ingestor.purge("u1", doc_id)

    cu.on_analyze = delete_now

    await ingestor.enrich(
        user_id="u1", document_id=doc_id, data=b"BYTES", content_type="application/pdf"
    )

    with pytest.raises(DocumentNotFoundError):
        await library.get_document("u1", doc_id)
    assert await chunks.search("u1", _QUERY, top_k=50) == []
    assert _blob_keys(blob, "u1", doc_id) == []
    # Nothing was indexed because persistence never ran.
    assert embedder.embedded == []


async def test_delete_during_persist_does_not_resurrect():
    """Vector 2: delete lands mid-persist (after the re-check) → terminal write
    detects the deletion and purges what persistence wrote. The fake deletes only
    the manifest (no purge) so the test proves the terminal-write path cleans the
    blob + index itself."""
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore(expected_dim=3)
    cu = _CU("# T\n\n" + ("word " * 30))
    embedder = _Embedder()
    ingestor = _build(
        cu=cu, embedder=embedder, chunks=chunks, library=library, blob=blob
    )

    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"BYTES"
    )
    doc_id = stored.document.id

    async def delete_manifest_only() -> None:
        await library.delete_document("u1", doc_id)

    # Fires inside _persist_enrichment, after the pre-persist existence re-check
    # has already passed — exercising the terminal non-resurrecting write.
    embedder.on_embed = delete_manifest_only

    await ingestor.enrich(
        user_id="u1", document_id=doc_id, data=b"BYTES", content_type="application/pdf"
    )

    with pytest.raises(DocumentNotFoundError):
        await library.get_document("u1", doc_id)
    assert await chunks.search("u1", _QUERY, top_k=50) == []
    assert _blob_keys(blob, "u1", doc_id) == []


async def test_cancel_enrich_on_delete_does_not_resurrect(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.library.ingest.emit_custom_event",
        lambda name, attributes: events.append((name, attributes)),
    )
    """Fix 3: a tracked enrich blocked in analyze is cancelled on delete; the
    finally path writes-then-detects the deletion and purges, never resurrecting."""
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore(expected_dim=3)
    cu = _BlockingCU()
    ingestor = _build(
        cu=cu, embedder=_Embedder(), chunks=chunks, library=library, blob=blob
    )

    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"BYTES"
    )
    doc_id = stored.document.id

    ingestor.schedule_enrich(
        user_id="u1", document_id=doc_id, content_type="application/pdf"
    )
    await asyncio.wait_for(cu.started.wait(), timeout=5)

    # DELETE handler: drop the manifest, cancel the in-flight crack, purge.
    await library.delete_document("u1", doc_id)
    await ingestor.cancel_enrich("u1", doc_id)
    await ingestor.purge("u1", doc_id)

    assert ("u1", doc_id) not in ingestor._tasks
    with pytest.raises(DocumentNotFoundError):
        await library.get_document("u1", doc_id)
    assert await chunks.search("u1", _QUERY, top_k=50) == []
    assert _blob_keys(blob, "u1", doc_id) == []
    assert any(
        name == "document_ingest_terminal" and attributes["status"] == "cancelled"
        for name, attributes in events
    )


async def test_enrich_preserves_revoked_acl_and_owner_metadata(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.library.ingest.emit_custom_event",
        lambda name, attributes: events.append((name, attributes)),
    )
    library = InMemoryDocumentLibraryRepository()
    cu = _BlockingCU()
    ingestor = _build(
        cu=cu,
        embedder=_Embedder(),
        chunks=InMemoryDocChunkStore(expected_dim=3),
        library=library,
    )
    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"BYTES"
    )
    shared = await library.get_document("u1", stored.document.id)
    shared.visibility = Visibility.shared
    shared.acl = ["bob@example.com"]
    await library.update_document(shared)

    task = asyncio.create_task(
        ingestor.enrich(
            user_id="u1",
            document_id=stored.document.id,
            data=b"BYTES",
            content_type="application/pdf",
        )
    )
    await asyncio.wait_for(cu.started.wait(), timeout=5)
    revoked = await library.get_document("u1", stored.document.id)
    revoked.visibility = Visibility.private
    revoked.acl = []
    revoked.annotations.append(DocumentAnnotation(body="owner note"))
    await library.update_document(revoked)
    cu.release.set()
    await task

    final = await library.get_document("u1", stored.document.id)
    assert final.status == DocumentStatus.ready
    assert final.visibility == Visibility.private
    assert final.acl == []
    assert [annotation.body for annotation in final.annotations] == ["owner note"]
    terminal = [
        attributes
        for name, attributes in events
        if name == "document_ingest_terminal"
    ][-1]
    assert terminal["status"] == "ready"
    assert terminal["persistenceOutcome"] == "committed"


async def test_schedule_enrich_noop_when_cu_disabled():
    library = InMemoryDocumentLibraryRepository()
    ingestor = _build(cu=None, library=library)
    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"X"
    )
    ingestor.schedule_enrich(
        user_id="u1",
        document_id=stored.document.id,
        content_type="application/pdf",
    )
    assert ingestor._tasks == {}


async def test_schedule_enrich_runs_to_ready():
    """The tracked-task path (used by the router) completes a normal enrich."""
    library = InMemoryDocumentLibraryRepository()
    chunks = InMemoryDocChunkStore(expected_dim=3)
    cu = _CU("# T\n\n" + ("alpha beta " * 8))
    ingestor = _build(cu=cu, embedder=_Embedder(), chunks=chunks, library=library)
    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"X"
    )
    doc_id = stored.document.id
    ingestor.schedule_enrich(
        user_id="u1", document_id=doc_id, content_type="application/pdf"
    )
    task = ingestor._tasks[("u1", doc_id)]
    await asyncio.wait_for(task, timeout=5)
    doc = await library.get_document("u1", doc_id)
    assert doc.status == DocumentStatus.ready


async def test_scheduled_enrich_reloads_canonical_blob_after_admission():
    class CapturingCU(_CU):
        seen: bytes | None = None

        async def analyze(self, analyzer_id, data, content_type, *, api_version=None):
            self.seen = data
            return await super().analyze(
                analyzer_id,
                data,
                content_type,
                api_version=api_version,
            )

    blob = InMemoryBlobStore()
    cu = CapturingCU("# ready")
    ingestor = _build(cu=cu, blob=blob)
    stored = await ingestor.ingest(
        user_id="u1", filename="d.txt", content_type="text/plain", data=b"upload"
    )
    assert stored.document.rawPath is not None
    await blob.put(stored.document.rawPath, b"canonical", "text/plain")

    assert ingestor.schedule_enrich(
        user_id="u1",
        document_id=stored.document.id,
        content_type="text/plain",
    ) is EnrichScheduleOutcome.scheduled
    await asyncio.wait_for(ingestor._tasks[("u1", stored.document.id)], timeout=5)

    assert cu.seen == b"canonical"


async def test_enrichment_concurrency_is_global_across_ingestors():
    assert MAX_CONCURRENT_DOCUMENT_ENRICHMENTS == 4
    class CountingCU:
        def __init__(self) -> None:
            self.active = 0
            self.maximum = 0
            self.started = 0
            self.at_limit = asyncio.Event()
            self.release = asyncio.Event()

        async def analyze(self, analyzer_id, data, content_type, *, api_version=None):
            self.active += 1
            self.started += 1
            self.maximum = max(self.maximum, self.active)
            if self.active == MAX_CONCURRENT_DOCUMENT_ENRICHMENTS:
                self.at_limit.set()
            try:
                await self.release.wait()
                return CUResult(status="Succeeded", analyzer_id="a", markdown="# ready")
            finally:
                self.active -= 1

    cu = CountingCU()
    ingestors = [_build(cu=cu), _build(cu=cu)]
    for index in range(6):
        ingestor = ingestors[index % 2]
        stored = await ingestor.ingest(
            user_id=f"u{index}",
            filename=f"{index}.txt",
            content_type="text/plain",
            data=f"doc-{index}".encode(),
        )
        assert ingestor.schedule_enrich(
            user_id=f"u{index}",
            document_id=stored.document.id,
            content_type="text/plain",
        ) is EnrichScheduleOutcome.scheduled

    await asyncio.wait_for(cu.at_limit.wait(), timeout=5)
    await asyncio.sleep(0)
    assert cu.maximum == 4
    assert cu.started == 4

    cu.release.set()
    tasks = [task for ingestor in ingestors for task in ingestor._tasks.values()]
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert cu.started == 6


async def test_enrichment_admission_cap_is_explicit_and_releases_on_failure(
    monkeypatch,
):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ai4ia_api.library.ingest.MAX_PENDING_DOCUMENT_ENRICHMENTS", 1
    )
    monkeypatch.setattr(
        "ai4ia_api.library.ingest.emit_custom_event",
        lambda name, attributes: events.append((name, attributes)),
    )
    cu = _BlockingCU()
    ingestor = _build(cu=cu)
    first = await ingestor.ingest(
        user_id="u1", filename="1.txt", content_type="text/plain", data=b"one"
    )
    second = await ingestor.ingest(
        user_id="u1", filename="2.txt", content_type="text/plain", data=b"two"
    )

    assert ingestor.schedule_enrich(
        user_id="u1", document_id=first.document.id, content_type="text/plain"
    ) is EnrichScheduleOutcome.scheduled
    assert ingestor.schedule_enrich(
        user_id="u1", document_id=second.document.id, content_type="text/plain"
    ) is EnrichScheduleOutcome.saturated
    assert any(
        name == "document_ingest_saturated" and attrs["stage"] == "admission"
        for name, attrs in events
    )

    failed, settlement = await ingestor.settle_saturated(second.document)
    assert settlement == "committed"
    assert failed.status is DocumentStatus.failed
    assert failed.error is not None and "Re-upload to retry" in failed.error

    await ingestor.cancel_enrich("u1", first.document.id)
    retried = await ingestor.ingest(
        user_id="u1", filename="2.txt", content_type="text/plain", data=b"two"
    )
    assert retried.deduped is False
    assert retried.document.status is DocumentStatus.stored
    assert ingestor.schedule_enrich(
        user_id="u1", document_id=retried.document.id, content_type="text/plain"
    ) is EnrichScheduleOutcome.scheduled
    await ingestor.cancel_enrich("u1", retried.document.id)


async def test_enrichment_admission_permit_releases_after_failed_task(monkeypatch):
    class FailThenSucceedCU:
        def __init__(self) -> None:
            self.calls = 0

        async def analyze(self, analyzer_id, data, content_type, *, api_version=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider failed")
            return CUResult(status="Succeeded", analyzer_id="a", markdown="# ready")

    monkeypatch.setattr(
        "ai4ia_api.library.ingest.MAX_PENDING_DOCUMENT_ENRICHMENTS", 1
    )
    cu = FailThenSucceedCU()
    ingestor = _build(cu=cu)
    first = await ingestor.ingest(
        user_id="u1", filename="1.txt", content_type="text/plain", data=b"one"
    )
    second = await ingestor.ingest(
        user_id="u1", filename="2.txt", content_type="text/plain", data=b"two"
    )

    assert ingestor.schedule_enrich(
        user_id="u1", document_id=first.document.id, content_type="text/plain"
    ) is EnrichScheduleOutcome.scheduled
    await asyncio.wait_for(ingestor._tasks[("u1", first.document.id)], timeout=5)
    await asyncio.sleep(0)
    assert ingestor.schedule_enrich(
        user_id="u1", document_id=second.document.id, content_type="text/plain"
    ) is EnrichScheduleOutcome.scheduled
    await asyncio.wait_for(ingestor._tasks[("u1", second.document.id)], timeout=5)
    assert cu.calls == 2


async def test_enrich_failure_midpersist_purges_partial_chunks():
    """A clean enrich failure (embed errors on a later batch) must not leave the
    earlier batch's chunks indexed under the resulting ``failed`` document — while
    the raw upload + quick-text summary are retained."""
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore(expected_dim=3)

    calls = {"n": 0}

    async def fail_second_batch() -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("embed gateway down")

    embedder = _Embedder(on_embed=fail_second_batch)
    cu = _CU("# T\n\n" + ("word " * 200))  # many chunks at 40 chars/chunk
    ingestor = _build(
        cu=cu,
        embedder=embedder,
        chunks=chunks,
        library=library,
        blob=blob,
        document_embed_batch=2,
    )
    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"BYTES"
    )
    doc_id = stored.document.id

    await ingestor.enrich(
        user_id="u1", document_id=doc_id, data=b"BYTES", content_type="application/pdf"
    )

    failed = await library.get_document("u1", doc_id)
    assert failed.status == DocumentStatus.failed
    # First batch indexed before the failure was purged — no orphan chunks.
    assert await chunks.search("u1", _QUERY, top_k=50) == []
    # The raw upload is retained (failed enrich keeps the user's document).
    assert _blob_keys(blob, "u1", doc_id) != []


async def test_recover_interrupted_fails_stuck_analyzing():
    library = InMemoryDocumentLibraryRepository()
    stuck = await library.create_document(
        UserDocument(userId="u1", filename="x.pdf", status=DocumentStatus.analyzing)
    )
    healthy = await library.create_document(
        UserDocument(userId="u1", filename="y.pdf", status=DocumentStatus.ready)
    )
    ingestor = _build(cu=_CU(""), library=library)

    swept = await ingestor.recover_interrupted(
        now=datetime.now(timezone.utc) + timedelta(hours=2)
    )
    assert swept == 1
    failed = await library.get_document("u1", stuck.id)
    assert failed.status == DocumentStatus.failed
    assert "interrupted" in (failed.error or "").lower()
    # A ready document is untouched.
    assert (await library.get_document("u1", healthy.id)).status == DocumentStatus.ready


async def test_recover_interrupted_purges_partial_artifacts():
    """A worker cancelled mid-persist leaves blob/chunk artifacts under a manifest
    still at ``analyzing``. The startup sweep must flip it to ``failed`` AND purge
    those artifacts so a failed doc contributes nothing to retrieval."""
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    chunks = InMemoryDocChunkStore(expected_dim=3)
    ingestor = _build(
        cu=_CU(""), embedder=_Embedder(), chunks=chunks, library=library, blob=blob
    )
    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"BYTES"
    )
    doc = stored.document
    # Simulate an interrupted enrich: artifacts written, but the manifest never
    # reached the terminal ready/failed write — it is stuck at ``analyzing``.
    await ingestor._persist_enrichment(
        "u1",
        doc,
        CUResult(status="Succeeded", analyzer_id="a", markdown="# T\n\n" + ("word " * 30)),
    )
    doc.status = DocumentStatus.analyzing
    await library.update_document(doc)
    assert _blob_keys(blob, "u1", doc.id) != []
    assert await chunks.search("u1", _QUERY, top_k=50) != []

    swept = await ingestor.recover_interrupted(
        now=datetime.now(timezone.utc) + timedelta(hours=2)
    )

    assert swept == 1
    failed = await library.get_document("u1", doc.id)
    assert failed.status == DocumentStatus.failed
    # The retrieval-reachable index and manifest pointers are gone, but the raw
    # upload survives a replica restart so "Re-upload to retry" is truthful.
    assert failed.rawPath is not None
    assert failed.rawPath in _blob_keys(blob, "u1", doc.id)
    assert failed.parsedPath is None
    assert failed.chunksPath is None
    assert failed.chunkCount == 0
    assert await chunks.search("u1", _QUERY, top_k=50) == []


async def test_recovery_preserves_concurrent_access_and_owner_metadata():
    class RecoveryRaceRepository(InMemoryDocumentLibraryRepository):
        def __init__(self) -> None:
            super().__init__()
            self.interleaved = False

        async def patch_ingest_fields(self, document, changes, **kwargs):
            if not self.interleaved:
                self.interleaved = True
                latest = await self.get_document(document.userId, document.id)
                latest.visibility = Visibility.private
                latest.acl = []
                latest.annotations.append(DocumentAnnotation(body="keep"))
                latest.sessionLinks.append("session-new")
                await self.update_document(latest)
            return await super().patch_ingest_fields(document, changes, **kwargs)

    library = RecoveryRaceRepository()
    stuck = await library.create_document(
        UserDocument(
            userId="u1",
            filename="x.pdf",
            status=DocumentStatus.analyzing,
            visibility=Visibility.shared,
            acl=["bob@example.com"],
        )
    )
    ingestor = _build(cu=_CU(""), library=library)
    assert await ingestor.recover_interrupted(
        now=datetime.now(timezone.utc) + timedelta(hours=2)
    ) == 1
    final = await library.get_document("u1", stuck.id)
    assert final.status == DocumentStatus.failed
    assert final.visibility == Visibility.private
    assert final.acl == []
    assert [annotation.body for annotation in final.annotations] == ["keep"]
    assert final.sessionLinks == ["session-new"]


async def test_recovery_skips_document_no_longer_analyzing():
    class StatusRaceRepository(InMemoryDocumentLibraryRepository):
        async def patch_ingest_fields(self, document, changes, **kwargs):
            latest = await self.get_document(document.userId, document.id)
            latest.status = DocumentStatus.ready
            await self.update_document(latest)
            return await super().patch_ingest_fields(document, changes, **kwargs)

    library = StatusRaceRepository()
    stuck = await library.create_document(
        UserDocument(userId="u1", filename="x.pdf", status=DocumentStatus.analyzing)
    )
    ingestor = _build(cu=_CU(""), library=library)
    assert await ingestor.recover_interrupted(
        now=datetime.now(timezone.utc) + timedelta(hours=2)
    ) == 0
    assert (
        await library.get_document("u1", stuck.id)
    ).status == DocumentStatus.ready


async def test_recovery_does_not_steal_a_fresh_row_from_another_replica():
    """A rolling deploy starts the sweep while the old replica still works.

    Status alone cannot distinguish a lost task from a healthy remote one; age
    is the lease. This is the production race the old sweep lost.
    """
    library = InMemoryDocumentLibraryRepository()
    active = await library.create_document(
        UserDocument(userId="u1", filename="x.pdf", status=DocumentStatus.analyzing)
    )
    ingestor = _build(cu=_CU(""), library=library)

    assert await ingestor.recover_interrupted(now=datetime.now(timezone.utc)) == 0
    assert (
        await library.get_document("u1", active.id)
    ).status == DocumentStatus.analyzing


async def test_reupload_of_failed_dedupe_hit_resets_and_retries():
    library = InMemoryDocumentLibraryRepository()
    blob = InMemoryBlobStore()
    ingestor = _build(cu=_CU(""), library=library, blob=blob)
    first = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"SAME"
    )
    failed = first.document
    failed.status = DocumentStatus.failed
    failed.error = "interrupted"
    await library.update_document(failed)

    retry = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"SAME"
    )

    assert retry.deduped is False
    assert retry.document.id == first.document.id
    assert retry.document.status == DocumentStatus.stored
    assert retry.document.error is None
    assert retry.document.rawPath is not None
    assert retry.document.rawPath in _blob_keys(blob, "u1", first.document.id)


async def test_chunk_cap_truncates_and_batches():
    library = InMemoryDocumentLibraryRepository()
    chunks = InMemoryDocChunkStore(expected_dim=3)
    embedder = _Embedder()
    cu = _CU("# T\n\n" + ("word " * 200))  # many chunks at 40 chars/chunk
    ingestor = _build(
        cu=cu,
        embedder=embedder,
        chunks=chunks,
        library=library,
        document_max_chunks=3,
        document_embed_batch=2,
    )
    stored = await ingestor.ingest(
        user_id="u1", filename="d.pdf", content_type="application/pdf", data=b"BYTES"
    )
    await ingestor.enrich(
        user_id="u1", document_id=stored.document.id, data=b"BYTES", content_type="application/pdf"
    )
    doc = await library.get_document("u1", stored.document.id)
    # Capped to 3 chunks, indexed in batches of 2 → ceil(3/2) = 2 embed calls.
    assert doc.chunkCount == 3
    assert len(embedder.embedded) == 3
    assert embedder.calls == 2
    assert len(await chunks.search("u1", _QUERY, top_k=50)) == 3
