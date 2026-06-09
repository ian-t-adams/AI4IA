"""Selects and constructs the document ingest pipeline (Phase 11B).

Returns ``None`` when document understanding is disabled (or the library repo was
not built), so the upload endpoint refuses and nothing is constructed — the
default-OFF, zero-regression posture. When enabled, the backing IO mirrors the
rest of the app: an in-memory blob store + chunk store locally, and Azure Blob +
Postgres/pgvector when configured. The Content Understanding client is only wired
when ``cu_base_url`` is set; without it, ``enrich`` is a no-op and a document stays
at ``stored`` with its instant quick-text summary.
"""
from __future__ import annotations

import logging

from ..catalog import ModelCatalog
from ..config import Settings
from ..content_understanding.client import ContentUnderstandingClient
from ..gateway.client import ModelGatewayClient
from ..memory.embedder import GatewayEmbedder
from ..usage.service import UsageService
from .blob_store import AzureBlobStore, BlobStore, InMemoryBlobStore
from .doc_chunks import DocChunkStore, InMemoryDocChunkStore, PgDocChunkStore
from .ingest import DocumentIngestor
from .repository import DocumentLibraryRepository
from .retrieval import DocumentRetrievalService

logger = logging.getLogger(__name__)


def _build_blob_store(settings: Settings) -> BlobStore:
    if settings.document_blob_account_url:
        return AzureBlobStore(
            settings.document_blob_account_url, settings.document_blob_container
        )
    return InMemoryBlobStore()


def _build_chunk_store(settings: Settings) -> DocChunkStore:
    if settings.postgres_host and settings.postgres_user:
        return PgDocChunkStore(
            host=settings.postgres_host,
            database=settings.postgres_database,
            user=settings.postgres_user,
            port=settings.postgres_port,
            expected_dim=settings.memory_embedding_dimensions,
        )
    return InMemoryDocChunkStore(expected_dim=settings.memory_embedding_dimensions)


def build_document_ingestor(
    settings: Settings,
    *,
    library: DocumentLibraryRepository | None,
    gateway: ModelGatewayClient,
    catalog: ModelCatalog,
    usage: UsageService,
) -> DocumentIngestor | None:
    if not settings.document_understanding_enabled or library is None:
        return None

    blob_store = _build_blob_store(settings)
    cu_client = (
        ContentUnderstandingClient(settings) if settings.cu_base_url else None
    )

    embedder: GatewayEmbedder | None = None
    chunk_store: DocChunkStore | None = None
    deployment = catalog.resolve_deployment(settings.memory_embedding_model)
    if deployment is None:
        # Without an embedding deployment we can still store + summarize, just not
        # build the retrieval index. Log and continue (chunks skipped in enrich).
        logger.warning(
            "document ingest: embedding model %r has no deployment; chunks disabled",
            settings.memory_embedding_model,
        )
    else:
        embedder = GatewayEmbedder(gateway, deployment.deploymentName)
        chunk_store = _build_chunk_store(settings)

    return DocumentIngestor(
        library=library,
        blob_store=blob_store,
        settings=settings,
        usage=usage,
        cu_client=cu_client,
        embedder=embedder,
        chunk_store=chunk_store,
    )


def build_document_retrieval(
    settings: Settings,
    *,
    ingestor: DocumentIngestor | None,
) -> DocumentRetrievalService | None:
    """Construct the retrieval consumer (Phase 11B-2) that surfaces a user's
    *ready* library in chat.

    Returns ``None`` when document understanding is disabled (no ingestor was
    built) — the default-OFF, zero-regression posture: chat injects no library
    context and the ``fetch_document`` tool is never advertised.

    The service deliberately *reuses* the ingestor's backing IO (repository, blob
    store, chunk store, embedder) rather than constructing its own. For the
    in-memory stores used locally and in tests this is required for correctness
    (a document indexed by the producer must be visible to the consumer); for the
    Azure/Postgres stores it keeps a single source of IO truth.
    """
    if ingestor is None:
        return None
    return DocumentRetrievalService(
        library=ingestor.library,
        blob_store=ingestor.blob,
        chunk_store=ingestor.chunks,
        embedder=ingestor.embedder,
        settings=settings,
    )
