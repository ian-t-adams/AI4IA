"""Document Search configuration is a startup contract, not a health probe."""
from __future__ import annotations

import pytest

from ai4ia_api.catalog import load_catalog
from ai4ia_api.library.ai_search_chunks import AzureSearchDocChunkStore
from ai4ia_api.library.doc_chunks import DocChunkRecord, InMemoryDocChunkStore
from ai4ia_api.library.ingest_factory import build_document_ingestor
from ai4ia_api.library.memory_repo import InMemoryDocumentLibraryRepository
from ai4ia_api.main import create_app
from tests.conftest import FakeUsage, make_settings


def _settings(**overrides):
    values = dict(
        env="dev",
        auth_provider="entra",
        entra_tenant_id="tenant",
        entra_audience="api://test",
        session_store="cosmos",
        cosmos_endpoint="https://cosmos.example/",
        memory_store="disabled",
        model_gateway_url="https://proxy.test/openai",
        model_gateway_auth_mode="api_key",
        model_gateway_api_key="proxy-secret",
        model_gateway_api_key_header="S7P-KEY",
        model_gateway_allowed_hosts="proxy.test",
        document_understanding_enabled=True,
        cu_base_url="https://cu.example/",
        document_blob_account_url="https://acct.blob.core.windows.net",
        search_endpoint="https://example.search.windows.net",
    )
    values.update(overrides)
    return make_settings(**values)


def _ingestor(settings, *, catalog=None):
    return build_document_ingestor(
        settings,
        library=InMemoryDocumentLibraryRepository(),
        gateway=object(),
        catalog=catalog or load_catalog(None, settings.data_residency),
        usage=FakeUsage(),
    )


@pytest.mark.parametrize("env", ["dev", "prod"])
@pytest.mark.parametrize("endpoint", [None, "", "   "])
def test_enabled_nonlocal_library_requires_search_before_startup(env, endpoint):
    settings = _settings(env=env, search_endpoint=endpoint)
    with pytest.raises(RuntimeError, match="AI4IA_SEARCH_ENDPOINT"):
        settings.validate_runtime()
    with pytest.raises(RuntimeError, match="AI4IA_SEARCH_ENDPOINT"):
        create_app(settings)
    with pytest.raises(RuntimeError, match="AI4IA_SEARCH_ENDPOINT"):
        _ingestor(settings)


@pytest.mark.parametrize("env", ["dev", "prod"])
@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.search.windows.net",
        "not-a-url",
        "https://user:password@example.search.windows.net",
        "https://example.search.windows.net?secret=value",
        "https://example.search.windows.net#fragment",
    ],
)
def test_nonlocal_search_endpoint_rejects_unsafe_shapes(env, endpoint):
    with pytest.raises(RuntimeError, match="AI4IA_SEARCH_ENDPOINT") as error:
        _settings(env=env, search_endpoint=endpoint).validate_runtime()
    assert endpoint not in str(error.value)


@pytest.mark.parametrize("env", ["dev", "prod"])
@pytest.mark.parametrize("model", ["", "missing-embedding-model", "gpt-5.2"])
def test_enabled_nonlocal_library_requires_an_embedding_model(env, model):
    settings = _settings(env=env, memory_embedding_model=model)
    with pytest.raises(RuntimeError, match="AI4IA_MEMORY_EMBEDDING_MODEL"):
        settings.validate_runtime()
    with pytest.raises(RuntimeError, match="AI4IA_MEMORY_EMBEDDING_MODEL"):
        _ingestor(settings)


def test_embedding_must_resolve_under_the_catalog_policy(monkeypatch):
    settings = _settings()
    catalog = load_catalog(None).model_copy(deep=True)
    embedding = catalog.get(settings.memory_embedding_model)
    assert embedding is not None
    assert catalog.resolve_deployment(embedding.id) is not None
    monkeypatch.setattr("ai4ia_api.catalog.load_catalog", lambda *args: catalog)
    settings.validate_runtime()
    assert _ingestor(settings, catalog=catalog).embedder is not None

    catalog.residencyPolicy = "unsupported-zone"
    assert catalog.resolve_deployment(embedding.id) is None
    with pytest.raises(RuntimeError, match="AI4IA_MEMORY_EMBEDDING_MODEL"):
        settings.validate_runtime()
    with pytest.raises(RuntimeError, match="AI4IA_MEMORY_EMBEDDING_MODEL"):
        _ingestor(settings, catalog=catalog)


@pytest.mark.parametrize("env", ["local", "dev", "prod"])
@pytest.mark.parametrize("per_user", [False, True])
def test_valid_config_constructs_search_without_querying_it(env, per_user, monkeypatch):
    async def no_network(*args, **kwargs):
        pytest.fail("configuration validation must not probe Search health")

    monkeypatch.setattr(AzureSearchDocChunkStore, "ensure_ready", no_network)
    settings = _settings(env=env, search_index_per_user=per_user)
    settings.validate_runtime()
    create_app(settings)
    ingestor = _ingestor(settings)
    assert isinstance(ingestor.chunks, AzureSearchDocChunkStore)
    assert ingestor.embedder is not None
    assert (
        ingestor.chunks.index_name_for_user("owner") == settings.search_index_name
    ) is (not per_user)


@pytest.mark.parametrize("env", ["local", "dev", "prod"])
def test_disabled_library_does_not_require_search_or_embeddings(env):
    settings = _settings(
        env=env,
        document_understanding_enabled=False,
        search_endpoint=None,
        memory_embedding_model="missing-embedding-model",
    )
    settings.validate_runtime()
    assert _ingestor(settings) is None


async def test_identical_local_fixture_keeps_usable_in_memory_chunks():
    settings = _settings(
        env="local", search_endpoint=None, memory_embedding_dimensions=3,
    )
    settings.validate_runtime()
    ingestor = _ingestor(settings)
    assert isinstance(ingestor.chunks, InMemoryDocChunkStore)
    record = DocChunkRecord(
        user_id="owner", document_id="document", chunk_index=0, content="Local text",
    )
    await ingestor.chunks.add_many([record], [[1.0, 0.0, 0.0]])
    assert await ingestor.chunks.search(
        "owner", [1.0, 0.0, 0.0], 1, document_ids=["document"], query_text="text",
    )
    assert not await ingestor.chunks.search(
        "another-owner", [1.0, 0.0, 0.0], 1, document_ids=["document"], query_text="text",
    )
