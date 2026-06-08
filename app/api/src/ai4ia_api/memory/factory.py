"""Build the configured memory service.

Returns a :class:`NoopMemoryService` unless a real store is selected and an
embedding deployment resolves. The pgvector backend is reserved for the next
increment and fails closed here (so selecting it can't silently no-op).
"""
from __future__ import annotations

import logging

from ..catalog import ModelCatalog
from ..config import MemoryStoreKind, Settings
from ..gateway.client import ModelGatewayClient
from .embedder import GatewayEmbedder
from .in_memory import InMemoryVectorStore
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

    if kind == MemoryStoreKind.pgvector:
        # Defined as a config target, but the asyncpg/pgvector backend (and its
        # Postgres connectivity + AAD auth) lands in the next increment.
        raise NotImplementedError(
            "memory_store=pgvector is not implemented in this build; "
            "use 'in_memory' or 'disabled'."
        )

    deployment = catalog.resolve_deployment(settings.memory_embedding_model)
    if deployment is None:
        logger.warning(
            "memory disabled: embedding model %r has no deployment in the catalog",
            settings.memory_embedding_model,
        )
        return NoopMemoryService()

    embedder = GatewayEmbedder(gateway, deployment.deploymentName)
    store = InMemoryVectorStore(expected_dim=settings.memory_embedding_dimensions)
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
