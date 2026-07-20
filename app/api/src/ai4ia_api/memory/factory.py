"""Build the configured memory service.

Returns a :class:`NoopMemoryService` unless a configured store and its catalog
models resolve. Construction stays synchronous and does not contact a database;
the selected service owns lazy warmup during the FastAPI lifespan.
"""
from __future__ import annotations

import logging

from ..catalog import ModelCatalog
from ..config import MemoryStoreKind, Settings
from ..gateway.client import ModelGatewayClient
from .cosmos_service import CosmosMemoryService
from .cosmos_store import CosmosMemoryStore
from .embedder import GatewayEmbedder
from .in_memory import InMemoryVectorStore
from .planner import MemoryPlanner
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

    if kind == MemoryStoreKind.cosmos:
        if not settings.cosmos_endpoint:
            logger.warning("memory disabled: cosmos requires cosmos_endpoint")
            return NoopMemoryService()
        extraction = catalog.resolve_deployment(settings.memory_extraction_model)
        if extraction is None:
            logger.warning(
                "memory disabled: extraction model %r has no deployment in the catalog",
                settings.memory_extraction_model,
            )
            return NoopMemoryService()
        embedder = GatewayEmbedder(gateway, deployment.deploymentName)
        store = CosmosMemoryStore(
            endpoint=settings.cosmos_endpoint,
            database=settings.cosmos_database,
            expected_dim=settings.memory_embedding_dimensions,
            embedding_model=deployment.deploymentName,
        )
        return CosmosMemoryService(
            store=store,
            embedder=embedder,
            planner=MemoryPlanner(gateway, extraction.deploymentName),
            embedding_model=deployment.deploymentName,
            top_k=settings.memory_top_k,
            min_score=settings.memory_min_score,
            max_injected=settings.memory_max_injected,
            max_chars_per_item=settings.memory_max_chars_per_item,
            max_total_chars=settings.memory_max_total_chars,
            min_chars_to_store=settings.memory_min_chars_to_store,
        )

    embedder = GatewayEmbedder(gateway, deployment.deploymentName)
    return MemoryService(
        store=InMemoryVectorStore(expected_dim=settings.memory_embedding_dimensions),
        embedder=embedder,
        top_k=settings.memory_top_k,
        min_score=settings.memory_min_score,
        max_injected=settings.memory_max_injected,
        max_chars_per_item=settings.memory_max_chars_per_item,
        max_total_chars=settings.memory_max_total_chars,
        min_chars_to_store=settings.memory_min_chars_to_store,
    )
