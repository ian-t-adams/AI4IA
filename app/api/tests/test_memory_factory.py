"""Unit tests for build_memory_service backend selection."""
from __future__ import annotations

from dataclasses import dataclass

from ai4ia_api.config import MemoryStoreKind, Settings
from ai4ia_api.memory.cosmos_service import CosmosMemoryService
from ai4ia_api.memory.factory import build_memory_service
from ai4ia_api.memory.in_memory import InMemoryVectorStore
from ai4ia_api.memory.service import MemoryService, NoopMemoryService


@dataclass
class _Deployment:
    deploymentName: str


class StubCatalog:
    def __init__(self, deployment: _Deployment | None, *, api: str = "chat") -> None:
        self._deployment = deployment
        self._api = api

    def resolve_deployment(self, model_id, **kwargs):
        return self._deployment

    def get(self, model_id):
        return type("_Entry", (), {"api": self._api})()


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


def test_cosmos_builds_canonical_service_without_connecting(monkeypatch):
    constructed: dict[str, object] = {}

    class StubCosmosStore:
        def __init__(self, **kwargs) -> None:
            constructed.update(kwargs)

    monkeypatch.setattr(
        "ai4ia_api.memory.factory.CosmosMemoryStore", StubCosmosStore
    )
    svc = build_memory_service(
        _settings(
            memory_store=MemoryStoreKind.cosmos,
            cosmos_endpoint="https://cosmos.example",
        ),
        gateway=_GATEWAY,
        catalog=_CATALOG,
    )
    assert isinstance(svc, CosmosMemoryService)
    assert constructed == {
        "endpoint": "https://cosmos.example",
        "database": "ai4ia",
        "expected_dim": 3072,
        "embedding_model": "text-embedding-3-large-dep",
    }


def test_cosmos_planner_keeps_extraction_model_api(monkeypatch):
    monkeypatch.setattr(
        "ai4ia_api.memory.factory.CosmosMemoryStore",
        lambda **_kwargs: object(),
    )
    service = build_memory_service(
        _settings(
            memory_store=MemoryStoreKind.cosmos,
            cosmos_endpoint="https://cosmos.example",
        ),
        gateway=_GATEWAY,
        catalog=StubCatalog(
            _Deployment("gpt-5.6-sol-dep"),
            api="responses",
        ),
    )

    assert isinstance(service, CosmosMemoryService)
    assert service._planner._api == "responses"


def test_cosmos_without_endpoint_fails_closed_to_noop():
    svc = build_memory_service(
        _settings(memory_store=MemoryStoreKind.cosmos),
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
