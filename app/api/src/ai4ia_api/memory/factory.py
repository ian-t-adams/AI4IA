"""Build the configured memory service.

Returns a :class:`NoopMemoryService` unless a real store is selected and an
embedding deployment resolves. Both real backends (in-memory and pgvector) share
the same embedder + service wiring; only the store differs. The pgvector store
connects lazily, so the factory stays synchronous (called from the FastAPI
lifespan) and never touches the database during construction.
"""
from __future__ import annotations

import logging

from ..catalog import ModelCatalog
from ..config import MemoryStoreKind, Settings
from ..gateway.client import ModelGatewayClient
from .base import MemoryStore
from .embedder import GatewayEmbedder
from .in_memory import InMemoryVectorStore
from .pgvector_store import PgVectorStore
from .service import MemoryService, MemoryServiceProtocol, NoopMemoryService

logger = logging.getLogger(__name__)


def build_memory_service(
    settings: Settings,
    *,
    gateway: ModelGatewayClient,
    catalog: ModelCatalog,
) -> MemoryServiceProtocol:
    kind = settings.memory_store
    if kind == MemoryStoreKind.disabled:
        return NoopMemoryService()

    deployment = catalog.resolve_deployment(settings.memory_embedding_model)
    if deployment is None:
        logger.warning(
            "memory disabled: embedding model %r has no deployment in the catalog",
            settings.memory_embedding_model,
        )
        return NoopMemoryService()

    store = _build_store(kind, settings)
    if store is None:
        return NoopMemoryService()

    embedder = GatewayEmbedder(gateway, deployment.deploymentName)
    return MemoryService(
        store=store,
        embedder=embedder,
        top_k=settings.memory_top_k,
        min_score=settings.memory_min_score,
        max_injected=settings.memory_max_injected,
        max_chars_per_item=settings.memory_max_chars_per_item,
        max_total_chars=settings.memory_max_total_chars,
        min_chars_to_store=settings.memory_min_chars_to_store,
    )


def _build_store(kind: MemoryStoreKind, settings: Settings) -> MemoryStore | None:
    """Construct the selected backing store, or ``None`` to disable memory."""
    if kind == MemoryStoreKind.pgvector:
        if not settings.postgres_host or not settings.postgres_user:
            # validate_runtime already enforces this; guard here too so a
            # misconfig fails closed (Noop) rather than building a broken store.
            logger.warning("memory disabled: pgvector requires postgres_host + postgres_user")
            return None
        return PgVectorStore(
            host=settings.postgres_host,
            database=settings.postgres_database,
            user=settings.postgres_user,
            port=settings.postgres_port,
            expected_dim=settings.memory_embedding_dimensions,
        )
    return InMemoryVectorStore(expected_dim=settings.memory_embedding_dimensions)
