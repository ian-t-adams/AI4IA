"""Unit tests for build_memory_service backend selection."""
from __future__ import annotations

from dataclasses import dataclass

from ai4ia_api.config import MemoryStoreKind, Settings
from ai4ia_api.memory.factory import build_memory_service
from ai4ia_api.memory.in_memory import InMemoryVectorStore
from ai4ia_api.memory.pgvector_store import PgVectorStore
from ai4ia_api.memory.service import MemoryService, NoopMemoryService


@dataclass
class _Deployment:
    deploymentName: str


class StubCatalog:
    def __init__(self, deployment: _Deployment | None) -> None:
        self._deployment = deployment

    def resolve_deployment(self, model_id, **kwargs):
        return self._deployment


_GATEWAY = object()  # GatewayEmbedder only stores the ref; never called here.
_CATALOG = StubCatalog(_Deployment("text-embedding-3-large-dep"))


def _settings(**kw) -> Settings:
    return Settings(**kw)


def test_disabled_returns_noop():
    svc = build_memory_service(
        _settings(memory_store=MemoryStoreKind.disabled), gateway=_GATEWAY, catalog=_CATALOG
    )
    assert isinstance(svc, NoopMemoryService)
    assert svc.enabled is False


def test_in_memory_builds_service_with_in_memory_store():
    svc = build_memory_service(
        _settings(memory_store=MemoryStoreKind.in_memory), gateway=_GATEWAY, catalog=_CATALOG
    )
    assert isinstance(svc, MemoryService)
    assert isinstance(svc._store, InMemoryVectorStore)


def test_pgvector_builds_service_without_connecting():
    svc = build_memory_service(
        _settings(
            memory_store=MemoryStoreKind.pgvector,
            postgres_host="psql.example.com",
            postgres_user="api-id",
        ),
        gateway=_GATEWAY,
        catalog=_CATALOG,
    )
    assert isinstance(svc, MemoryService)
    assert isinstance(svc._store, PgVectorStore)
    # Constructed but never connected (lazy): no pool yet.
    assert svc._store._pool is None
    assert svc.enabled is True


def test_pgvector_without_host_fails_closed_to_noop():
    svc = build_memory_service(
        _settings(memory_store=MemoryStoreKind.pgvector, postgres_user="api-id"),
        gateway=_GATEWAY,
        catalog=_CATALOG,
    )
    assert isinstance(svc, NoopMemoryService)


def test_unresolved_embedding_model_disables_memory():
    svc = build_memory_service(
        _settings(memory_store=MemoryStoreKind.in_memory),
        gateway=_GATEWAY,
        catalog=StubCatalog(None),
    )
    assert isinstance(svc, NoopMemoryService)
