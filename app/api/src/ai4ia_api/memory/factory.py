"""Build the configured memory service.

Returns a :class:`NoopMemoryService` unless a real store is selected and an
embedding deployment resolves. The two custom backends (in-memory and pgvector)
share the same embedder + service wiring; only the store differs. The ``mem0``
backend is the real ``mem0ai`` library wrapped by :class:`Mem0MemoryService` and
is built through a lazy factory closure so the database/library are only touched
when the backend is actually used. Either way the factory stays synchronous
(called from the FastAPI lifespan) and never touches the database during
construction.
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

    if kind == MemoryStoreKind.mem0:
        return _build_mem0_service(settings, catalog, deployment.deploymentName)

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


def _build_mem0_service(
    settings: Settings,
    catalog: ModelCatalog,
    embed_deployment: str,
) -> MemoryServiceProtocol:
    """Wire :class:`Mem0MemoryService` around a lazy factory closure.

    The closure builds the AAD Postgres pool, assembles the mem0 config pointed
    at our model gateway, and constructs ``AsyncMemory`` — all blocking work, so
    it is invoked off the event loop inside the service. Resolution failures
    (missing extraction deployment, no Postgres) fall back to Noop here so the
    app starts cleanly; runtime build failures degrade to "no memory" lazily.
    """
    from .mem0_service import Mem0Bundle, Mem0MemoryService, build_mem0_config
    from .mem0_store import (
        SyncAadTokenProvider,
        build_aad_pool,
        normalize_azure_openai_endpoint,
    )

    if not settings.postgres_host or not settings.postgres_user:
        logger.warning("memory disabled: mem0 requires postgres_host + postgres_user")
        return NoopMemoryService()

    extraction = catalog.resolve_deployment(settings.memory_extraction_model)
    if extraction is None:
        logger.warning(
            "memory disabled: extraction model %r has no deployment in the catalog",
            settings.memory_extraction_model,
        )
        return NoopMemoryService()

    endpoint = normalize_azure_openai_endpoint(settings.model_gateway_url)

    def factory() -> Mem0Bundle:
        import os

        # Disable mem0's PostHog telemetry before importing the library.
        os.environ.setdefault("MEM0_TELEMETRY", "false")
        from mem0 import AsyncMemory

        provider = SyncAadTokenProvider()
        pool = build_aad_pool(
            host=settings.postgres_host,  # type: ignore[arg-type]
            database=settings.postgres_database,
            user=settings.postgres_user,  # type: ignore[arg-type]
            port=settings.postgres_port,
            provider=provider,
        )

        def _close() -> None:
            try:
                pool.close()
            finally:
                provider.close()

        try:
            config = build_mem0_config(
                endpoint=endpoint,
                api_key=settings.model_gateway_api_key,
                api_version=settings.gateway_api_version,
                llm_deployment=extraction.deploymentName,
                embed_deployment=embed_deployment,
                embedding_dims=settings.memory_embedding_dimensions,
                collection_name=settings.mem0_collection_name,
                history_db_path=settings.mem0_history_db_path,
                connection_pool=pool,
            )
            memory = AsyncMemory.from_config(config)
        except Exception:
            _close()
            raise
        return Mem0Bundle(memory=memory, close=_close)

    return Mem0MemoryService(
        factory=factory,
        top_k=settings.memory_top_k,
        search_threshold=settings.mem0_search_threshold,
        min_chars_to_store=settings.memory_min_chars_to_store,
        max_injected=settings.memory_max_injected,
        max_chars_per_item=settings.memory_max_chars_per_item,
        max_total_chars=settings.memory_max_total_chars,
        add_timeout_s=settings.mem0_add_timeout_s,
        op_timeout_s=settings.mem0_op_timeout_s,
        max_concurrency=settings.mem0_max_concurrency,
    )
