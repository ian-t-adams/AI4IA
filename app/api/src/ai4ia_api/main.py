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
from .routers.realtime import AiohttpRealtimeConnector
from .library.factory import build_document_library
from .library.ingest_factory import build_document_ingestor, build_document_retrieval
from .library.compute_factory import build_document_compute
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
from .routers import library as library_router
from .routers import realtime as realtime_router
from .routers import sessions as sessions_router
from .routers import usage as usage_router
from .routers import voice as voice_router
from .sessions.factory import build_session_repository
from .sessions.repository import SessionNotFoundError
from .usage.factory import build_usage_repository
from .usage.pricing import load_pricing
from .usage.service import UsageService
from .workflows.factory import build_workflow_store
from .workflows.service import WorkflowService
from .routers import workflows as workflows_router

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
        # Per-user document library (Phase 11A). Feature-flagged + default-OFF:
        # build_document_library returns None unless
        # settings.document_understanding_enabled, so the ``/api/library`` API
        # refuses (404) and nothing is constructed by default — zero regression.
        app.state.document_library = build_document_library(settings)
        app.state.gateway = ModelGatewayClient(settings, http_client=http)
        # Voice Live (Phase 10): the upstream realtime-WS connector. Default OFF,
        # so this is unused unless settings.realtime_enabled is true. Swappable in
        # tests (a fake socket) the same way app.state.gateway is.
        app.state.realtime_connector = AiohttpRealtimeConnector()
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
        # User-defined workflows (Phase 8 inc 3): saved, ordered pipelines of agent
        # steps. Own Cosmos container ("workflows", PK /userId) + own invocation
        # surface, so a workflow and an agent may share a name. Not on the chat hot
        # path, so reads do NOT fail open — a store error surfaces to the caller.
        app.state.workflow_service = WorkflowService(build_workflow_store(settings))
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
        # Document ingest pipeline (Phase 11B). Built only when document
        # understanding is enabled (else None), so the upload endpoint refuses
        # and no blob/CU/pgvector IO is constructed by default — zero regression.
        # Owns its blob store + CU client + chunk store; closed in finally.
        app.state.document_ingestor = build_document_ingestor(
            settings,
            library=app.state.document_library,
            gateway=app.state.gateway,
            catalog=app.state.catalog,
            usage=app.state.usage,
        )
        # Recover documents left stuck at ``analyzing`` by an enrich task that was
        # cancelled on a prior shutdown (or lost to a crash): flip them to
        # ``failed`` so they aren't permanent zombies. Best-effort, startup-only.
        if app.state.document_ingestor is not None:
            try:
                await app.state.document_ingestor.recover_interrupted()
            except Exception:  # noqa: BLE001 - startup sweep must not block boot
                logger.warning("document recovery sweep failed", exc_info=True)
        # Document retrieval consumer (Phase 11B-2). Reuses the ingestor's backing
        # IO so a document indexed by the producer is visible to chat retrieval.
        # None when document understanding is off (no library context, no
        # fetch_document tool) — zero regression by default.
        app.state.document_retrieval = build_document_retrieval(
            settings, ingestor=app.state.document_ingestor
        )
        # Document compute consumer (Phase 11C): intent router + Code Interpreter
        # + "adjust & return" export. Layered on top of retrieval and reusing the
        # ingestor's IO. None when document compute is off (default), so the chat
        # hot path never classifies intent, advertises neither tool, and the
        # version-download endpoint refuses — zero regression by default. Owns the
        # Code Interpreter client; closed in finally.
        app.state.document_compute = build_document_compute(
            settings,
            ingestor=app.state.document_ingestor,
            retrieval=app.state.document_retrieval,
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
            try:
                await app.state.workflow_service.close()
            except Exception:  # noqa: BLE001
                logger.warning("workflow service close failed", exc_info=True)
            repo = app.state.session_repo
            close = getattr(repo, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    logger.warning("session repo close failed", exc_info=True)
            library = getattr(app.state, "document_library", None)
            lib_close = getattr(library, "close", None) if library else None
            if lib_close is not None:
                try:
                    await lib_close()
                except Exception:  # noqa: BLE001
                    logger.warning("document library close failed", exc_info=True)
            ingestor = getattr(app.state, "document_ingestor", None)
            if ingestor is not None:
                try:
                    await ingestor.close()
                except Exception:  # noqa: BLE001
                    logger.warning("document ingestor close failed", exc_info=True)
            compute = getattr(app.state, "document_compute", None)
            if compute is not None:
                try:
                    await compute.close()
                except Exception:  # noqa: BLE001
                    logger.warning("document compute close failed", exc_info=True)

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

    # Map unhandled Azure data-plane failures (Cosmos/Blob connectivity, throttling,
    # 5xx, or managed-identity token-acquisition errors) to 503 instead of a raw 500.
    # Repos translate expected not-found codes before they reach here, so anything that
    # bubbles is a genuine availability problem. With this, a managed-store outage (e.g.
    # the Cosmos public-network drift from a tenant policy remediation) degrades
    # gracefully: the static model catalog + chat stay usable and the web client
    # (Promise.allSettled) shows a scoped "temporarily unavailable" notice rather than
    # blanking the whole app. Imported defensively so the app still builds where the
    # Azure SDKs are absent (the repos themselves import azure lazily for the same reason).
    try:
        from azure.core.exceptions import AzureError, HttpResponseError
    except Exception:  # noqa: BLE001 - azure-core is a hard dep in the deployed image
        AzureError = HttpResponseError = None  # type: ignore[assignment,misc]

    if AzureError is not None:

        @app.exception_handler(AzureError)
        async def _azure_unavailable(_request: Request, exc: AzureError):
            # Preserve 500 semantics for an unexpected non-transient 4xx (a client/code
            # bug); treat connectivity, auth, throttling (408/429) and 5xx as transient.
            status_code = getattr(exc, "status_code", None)
            if (
                HttpResponseError is not None
                and isinstance(exc, HttpResponseError)
                and isinstance(status_code, int)
                and 400 <= status_code < 500
                and status_code not in (408, 429)
            ):
                logger.exception("Unexpected Azure client error")
                return JSONResponse(
                    status_code=500, content={"detail": "Internal server error"}
                )
            logger.warning("Azure data-plane unavailable -> 503 (%s)", type(exc).__name__)
            return JSONResponse(
                status_code=503, content={"detail": "Service temporarily unavailable"}
            )

    app.include_router(health_router.router)
    app.include_router(catalog_router.router)
    app.include_router(agents_router.router)
    app.include_router(workflows_router.router)
    app.include_router(sessions_router.router)
    app.include_router(chat_router.router)
    app.include_router(documents_router.router)
    app.include_router(images_router.router)
    app.include_router(library_router.router)
    app.include_router(voice_router.router)
    app.include_router(realtime_router.router)
    app.include_router(usage_router.router)
    app.include_router(entitlements_router.self_router)
    app.include_router(entitlements_router.admin_router)
    return app


app = create_app()
