"""Shutdown drains background consumers before closing their shared IO.

Every resource is attempted independently, including when another close fails
or an optional service was never assigned.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from ai4ia_api.content_understanding.models import CUResult
from ai4ia_api.library.blob_store import InMemoryBlobStore
from ai4ia_api.library.ingest import DocumentIngestor, EnrichScheduleOutcome
from ai4ia_api.library.memory_repo import InMemoryDocumentLibraryRepository
from ai4ia_api.main import create_app
from tests.conftest import FakeUsage, make_settings


_CLEANUP_METHODS = {
    "durable_workflows": "stop",
    "document_ingestor": "close",
    "memory": "close",
    "usage": "close",
    "resource_metrics": "close",
    "operations_metrics": "close",
    "entitlements": "close",
    "user_directory": "close",
    "agent_service": "close",
    "workflow_service": "close",
    "mcp_service": "close",
    "official_mcp_service": "close",
    "session_repo": "close",
    "document_library": "close",
    "document_compute": "close",
    "image_artifacts": "close",
    "video_artifacts": "close",
    "document_artifacts": "close",
    "inline_attachment_analysis": "close",
    "inline_attachment_store": "close",
    "web_search": "close",
}


@pytest.mark.parametrize("failing_resource", [None, "http", *_CLEANUP_METHODS])
async def test_shutdown_attempts_every_resource_after_any_close_failure(
    monkeypatch, failing_resource,
):
    closed: list[str] = []
    app = create_app(make_settings())
    http_close = httpx.AsyncClient.aclose

    async def close_http(client):
        closed.append("http")
        await http_close(client)
        if failing_resource == "http":
            raise RuntimeError("close failed")

    def resource(name):
        async def close():
            closed.append(name)
            if name == failing_resource:
                raise RuntimeError("close failed")

        return SimpleNamespace(**{_CLEANUP_METHODS[name]: close})

    async with app.router.lifespan_context(app):
        for name in _CLEANUP_METHODS:
            setattr(app.state, name, resource(name))
        monkeypatch.setattr(httpx.AsyncClient, "aclose", close_http)

    assert len(closed) == len(_CLEANUP_METHODS) + 1
    assert set(closed) == {"http", *_CLEANUP_METHODS}
    assert closed[0] == "durable_workflows"
    assert closed[-1] == "http"
    for dependency in ("usage", "entitlements", "document_library", "session_repo"):
        assert closed.index("document_ingestor") < closed.index(dependency)


async def test_shutdown_survives_all_services_missing(monkeypatch):
    app = create_app(make_settings())
    http_close = httpx.AsyncClient.aclose
    closed = []

    async def close_http(client):
        closed.append(client)
        await http_close(client)

    async with app.router.lifespan_context(app):
        for name in _CLEANUP_METHODS:
            delattr(app.state, name)
        monkeypatch.setattr(httpx.AsyncClient, "aclose", close_http)

    assert len(closed) == 1
    assert closed[0].is_closed


@pytest.mark.parametrize("finish_before_close", [False, True])
async def test_shutdown_drains_background_ingest_before_closing_usage(finish_before_close):
    class Usage(FakeUsage):
        closed = False

        async def record_completion(self, **kwargs):
            self.calls.append({**kwargs, "store_closed": self.closed})

        async def close(self):
            self.closed = True

    class BlockingCU:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def analyze(self, *args, **kwargs):
            self.started.set()
            await self.release.wait()
            return CUResult(status="Succeeded", analyzer_id="a", markdown="content")

    settings = make_settings()
    app = create_app(settings)
    usage = Usage()
    cu = BlockingCU()
    async with app.router.lifespan_context(app):
        library = InMemoryDocumentLibraryRepository()
        ingestor = DocumentIngestor(
            library=library,
            blob_store=InMemoryBlobStore(),
            settings=settings,
            usage=usage,
            cu_client=cu,
        )
        app.state.usage = usage
        app.state.document_library = library
        app.state.document_ingestor = ingestor
        stored = await ingestor.ingest(
            user_id="u1", filename="document.txt", content_type="text/plain", data=b"content",
        )
        outcome = ingestor.schedule_enrich(
            user_id="u1", document_id=stored.document.id, content_type="text/plain",
        )
        assert outcome is EnrichScheduleOutcome.scheduled
        await asyncio.wait_for(cu.started.wait(), timeout=2)
        assert not usage.calls
        if finish_before_close:
            cu.release.set()
            await asyncio.wait_for(asyncio.gather(*ingestor._tasks.values()), timeout=2)

    assert len(usage.calls) == 1
    assert usage.calls[0]["status"] == ("complete" if finish_before_close else "error")
    assert usage.calls[0]["store_closed"] is False
    assert not ingestor._tasks
    assert usage.closed
