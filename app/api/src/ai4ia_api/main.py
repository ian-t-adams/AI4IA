"""FastAPI application factory for the AI4IA backend."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth.factory import build_auth_provider
from .agents.agent_catalog import load_agent_catalog
from .agents.factory import build_user_agent_store
from .agents.service import AgentService
from .agents.tool_exec import attachable_tool_names, build_tools
from .catalog import load_catalog
from .config import Settings, get_settings
from .entitlements.factory import build_default_entitlement, build_entitlement_store
from .entitlements.service import EntitlementService
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
from .routers import documents as documents_router
from .routers import entitlements as entitlements_router
from .routers import health as health_router
from .routers import images as images_router
from .routers import sessions as sessions_router
from .routers import usage as usage_router
from .routers import voice as voice_router
from .sessions.factory import build_session_repository
from .sessions.repository import SessionNotFoundError
from .usage.factory import build_usage_repository
from .usage.pricing import load_pricing
from .usage.service import UsageService

_CORRELATION_HEADER = "x-correlation-id"

logger = logging.getLogger(__name__)

# Cap the startup memory warmup so an unreachable database can't stall the app:
# warmup is purely diagnostic (the store self-heals by retrying lazily).
_MEMORY_WARMUP_TIMEOUT_S = 10.0


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
        # User-defined agents (Phase 8). The service composes the curated catalog
        # with each user's saved personas (per-user, durable in Cosmos) and owns
        # CRUD + validation. Reads fail open to the curated catalog so a store
        # blip can never break chat. Tools a user may attach are an explicit
        # safe/no-scope/no-approval allowlist computed once from the seeded tools.
        app.state.agent_service = AgentService(
            build_user_agent_store(settings),
            catalog=app.state.catalog,
            attachable_tools=attachable_tool_names(registry, executor),
        )
        # Per-user memory (Phase 5). Disabled by default -> NoopMemoryService, so
        # the chat path can call it unconditionally with no behavior change.
        app.state.memory = build_memory_service(
            settings, gateway=app.state.gateway, catalog=app.state.catalog
        )
        # Usage metering / cost ledger (Phase 6). Observational: records each
        # completed turn to a per-user ledger and emits structured cost telemetry.
        # Best-effort by construction (record_completion never raises), and shares
        # the session store's durability (Cosmos vs in-memory) via the factory.
        app.state.usage = UsageService(
            build_usage_repository(settings),
            load_pricing(),
            enabled=settings.usage_metering_enabled,
        )
        # Entitlement enforcement (Phase 6B). Ships effectively unlimited: with
        # no per-user override and no global default cap, check() short-circuits
        # to allow with zero ledger IO. The store shares the session store's
        # durability; the usage service supplies rolling-window totals.
        app.state.entitlements = EntitlementService(
            build_entitlement_store(settings),
            app.state.usage,
            build_default_entitlement(settings),
            enabled=settings.entitlements_enabled,
            cache_ttl_seconds=settings.entitlement_cache_ttl_seconds,
        )
        # Surface store init problems (auth/network/DDL) loudly at startup, but
        # never fail startup over them: the store retries lazily on first use.
        try:
            await asyncio.wait_for(
                app.state.memory.warmup(), timeout=_MEMORY_WARMUP_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 - warmup is best-effort/diagnostic
            logger.warning("memory warmup failed; will initialize lazily", exc_info=True)
        try:
            yield
        finally:
            await http.aclose()
            # Close each resource independently so one failure can't skip others.
            try:
                await app.state.memory.close()
            except Exception:  # noqa: BLE001
                logger.warning("memory close failed", exc_info=True)
            try:
                await app.state.usage.close()
            except Exception:  # noqa: BLE001
                logger.warning("usage service close failed", exc_info=True)
            try:
                await app.state.entitlements.close()
            except Exception:  # noqa: BLE001
                logger.warning("entitlement store close failed", exc_info=True)
            try:
                await app.state.agent_service.close()
            except Exception:  # noqa: BLE001
                logger.warning("agent service close failed", exc_info=True)
            repo = app.state.session_repo
            close = getattr(repo, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    logger.warning("session repo close failed", exc_info=True)

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
    app.include_router(documents_router.router)
    app.include_router(images_router.router)
    app.include_router(voice_router.router)
    app.include_router(usage_router.router)
    app.include_router(entitlements_router.self_router)
    app.include_router(entitlements_router.admin_router)
    return app


app = create_app()
