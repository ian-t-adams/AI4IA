"""FastAPI application factory for the AI4IA backend."""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth.factory import build_auth_provider
from .agents.agent_catalog import load_agent_catalog
from .agents.tool_exec import build_tools
from .catalog import load_catalog
from .config import Settings, get_settings
from .gateway.client import ModelGatewayClient
from .memory.factory import build_memory_service
from .logging_setup import (
    configure_logging,
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)
from .routers import agents as agents_router
from .routers import catalog as catalog_router
from .routers import chat as chat_router
from .routers import health as health_router
from .routers import sessions as sessions_router
from .sessions.factory import build_session_repository
from .sessions.repository import SessionNotFoundError

_CORRELATION_HEADER = "x-correlation-id"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    settings.validate_runtime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = httpx.AsyncClient(timeout=settings.gateway_timeout_seconds)
        app.state.settings = settings
        app.state.auth_provider = build_auth_provider(settings)
        app.state.session_repo = build_session_repository(settings)
        app.state.gateway = ModelGatewayClient(settings, http_client=http)
        app.state.catalog = load_catalog(settings.model_catalog_path)
        app.state.agents = load_agent_catalog(settings.agent_catalog_path)
        # Tool-safety registry (governs) + executor (runs). Separate objects,
        # seeded together so a tool's safety contract and handler never drift.
        registry, executor = build_tools()
        app.state.tool_registry = registry
        app.state.tool_executor = executor
        # Per-user memory (Phase 5). Disabled by default -> NoopMemoryService, so
        # the chat path can call it unconditionally with no behavior change.
        app.state.memory = build_memory_service(
            settings, gateway=app.state.gateway, catalog=app.state.catalog
        )
        try:
            yield
        finally:
            await http.aclose()
            repo = app.state.session_repo
            close = getattr(repo, "close", None)
            if close is not None:
                await close()

    app = FastAPI(title="AI4IA API", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        incoming = request.headers.get(_CORRELATION_HEADER) or new_correlation_id()
        set_correlation_id(incoming)
        response = await call_next(request)
        response.headers[_CORRELATION_HEADER] = get_correlation_id()
        return response

    @app.exception_handler(SessionNotFoundError)
    async def _session_not_found(_request: Request, _exc: SessionNotFoundError):
        return JSONResponse(status_code=404, content={"detail": "Session not found"})

    app.include_router(health_router.router)
    app.include_router(catalog_router.router)
    app.include_router(agents_router.router)
    app.include_router(sessions_router.router)
    app.include_router(chat_router.router)
    return app


app = create_app()
