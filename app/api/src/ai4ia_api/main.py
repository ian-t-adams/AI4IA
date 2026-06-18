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
from .agents.mcp_client import HttpxMcpConnector
from .agents.mcp_secrets import build_mcp_secret_store
from .agents.mcp_servers import MAX_MCP_SERVERS_PER_USER
from .agents.mcp_service import McpServerService
from .agents.mcp_store import build_user_mcp_server_store
from .agents.service import AgentService
from .agents.tool_exec import attachable_tool_names, build_tools
from .catalog import load_catalog
from .config import Settings, get_settings
from .entitlements.factory import build_default_entitlement, build_entitlement_store
from .entitlements.service import EntitlementService
from .gateway.client import ModelGatewayClient
from .images.artifacts import ImageArtifactStore, build_image_blob_store
from .videos.artifacts import VideoArtifactStore, build_video_blob_store
from .docprocessing.artifacts import (
    DocumentArtifactStore,
    build_document_artifact_blob_store,
)
from .documents.analyze_factory import build_inline_attachment_analysis
from .documents.ephemeral_store import (
    EphemeralAttachmentStore,
    build_inline_attachment_blob_store,
)
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
from .routers import docprocessing as docprocessing_router
from .routers import documents as documents_router
from .routers import entitlements as entitlements_router
from .routers import health as health_router
from .routers import images as images_router
from .routers import library as library_router
from .routers import mcp_servers as mcp_servers_router
from .routers import realtime as realtime_router
from .routers import sessions as sessions_router
from .routers import usage as usage_router
from .routers import videos as videos_router
from .routers import voice as voice_router
from .sessions.factory import build_session_repository
from .sessions.repository import SessionNotFoundError
from .usage.factory import build_usage_repository
from .usage.pricing import load_pricing
from .usage.service import UsageService
from .websearch.factory import build_web_search_service
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
        # User-registered MCP servers / BYO custom tools (Phase 12A). Feature-
        # flagged + default-OFF: when settings.custom_tools_enabled is false the
        # service is None, so the ``/api/agents/mcp-servers`` API refuses (404) and
        # nothing is constructed — zero regression by default. When enabled, the
        # service owns the per-user registry (durable in Cosmos), the strict SSRF
        # egress guard on every endpoint, and tool discovery via the MCP client.
        if settings.custom_tools_enabled:
            connector = HttpxMcpConnector(
                timeout_s=settings.custom_tools_discovery_timeout_seconds
            )
            max_servers = (
                settings.custom_tools_max_servers_per_user
                if settings.custom_tools_max_servers_per_user > 0
                else MAX_MCP_SERVERS_PER_USER
            )
            app.state.mcp_service = McpServerService(
                build_user_mcp_server_store(settings),
                connector=connector,
                secret_store=build_mcp_secret_store(settings),
                max_servers=max_servers,
            )
        else:
            app.state.mcp_service = None
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
        # Durable store for tool-generated images (Phase 11F). Shared (single
        # instance) so the byte written during a tool turn is readable by the
        # later authenticated serve request. Durable AzureBlobStore when
        # image_blob_account_url is set; an in-memory store locally/in tests.
        # Independent of the document library (image generation ships live).
        app.state.image_artifacts = ImageArtifactStore(build_image_blob_store(settings))
        # Durable store for tool-generated videos (Phase 11G, Sora 2). Same
        # shared-instance rationale as images; durable AzureBlobStore when
        # video_blob_account_url is set, else an in-memory store.
        app.state.video_artifacts = VideoArtifactStore(build_video_blob_store(settings))
        # Durable store for over-cap ``process_document`` results (Phase 11H).
        # Same shared-instance rationale as images/video; reuses the document
        # library's blob account (document_blob_account_url) when configured, else
        # an in-memory store. The backing capability only runs when document
        # understanding is enabled, but the store is always built so the serve
        # endpoint can answer (404) regardless.
        app.state.document_artifacts = DocumentArtifactStore(
            build_document_artifact_blob_store(settings)
        )
        # Ephemeral retained-bytes store for inline composer attachments (default-OFF
        # inline code-interpreter feature). Always built so the cleanup paths
        # (document/session delete) can call it unconditionally; it reuses the
        # document blob account on a dedicated ephemeral container, else an
        # in-memory store. Retention into it only ever happens when the feature flag
        # is on (routers/documents.py), so when off NO bytes are ever written.
        app.state.inline_attachment_store = EphemeralAttachmentStore(
            build_inline_attachment_blob_store(settings)
        )
        # Inline-attachment analysis service (default-OFF). None when the flag is
        # off, so the chat hot path never advertises the analyze_attachment tool and
        # no Code Interpreter client is constructed — zero regression by default.
        # When on, reuses the ephemeral store + a Responses API CI client (owned;
        # closed in finally), the entitlement gate, and the usage meter.
        app.state.inline_attachment_analysis = build_inline_attachment_analysis(
            settings,
            store=app.state.inline_attachment_store,
            entitlements=app.state.entitlements,
            metering=app.state.usage,
        )
        # Web IQ search service (default-OFF). None when the flag is off, so the
        # chat hot path never advertises any web tool and no SDK client is
        # constructed — zero regression by default. When on, owns a lazily-built
        # WebIQAsyncClient (closed in finally), the entitlement gate, and the usage
        # meter.
        app.state.web_search = build_web_search_service(
            settings,
            entitlements=app.state.entitlements,
            metering=app.state.usage,
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
            mcp_service = getattr(app.state, "mcp_service", None)
            if mcp_service is not None:
                try:
                    await mcp_service.close()
                except Exception:  # noqa: BLE001
                    logger.warning("mcp service close failed", exc_info=True)
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
            image_artifacts = getattr(app.state, "image_artifacts", None)
            if image_artifacts is not None:
                try:
                    await image_artifacts.close()
                except Exception:  # noqa: BLE001
                    logger.warning("image artifact store close failed", exc_info=True)
            video_artifacts = getattr(app.state, "video_artifacts", None)
            if video_artifacts is not None:
                try:
                    await video_artifacts.close()
                except Exception:  # noqa: BLE001
                    logger.warning("video artifact store close failed", exc_info=True)
            document_artifacts = getattr(app.state, "document_artifacts", None)
            if document_artifacts is not None:
                try:
                    await document_artifacts.close()
                except Exception:  # noqa: BLE001
                    logger.warning("document artifact store close failed", exc_info=True)
            inline_attachment_analysis = getattr(
                app.state, "inline_attachment_analysis", None
            )
            if inline_attachment_analysis is not None:
                try:
                    await inline_attachment_analysis.close()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "inline attachment analysis close failed", exc_info=True
                    )
            inline_attachment_store = getattr(app.state, "inline_attachment_store", None)
            if inline_attachment_store is not None:
                try:
                    await inline_attachment_store.close()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "inline attachment store close failed", exc_info=True
                    )
            web_search = getattr(app.state, "web_search", None)
            if web_search is not None:
                try:
                    await web_search.close()
                except Exception:  # noqa: BLE001
                    logger.warning("web search service close failed", exc_info=True)

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

        # Data-plane 4xx codes that signal an operational/availability problem rather
        # than a malformed-request (code) bug, so they degrade to 503 rather than 500:
        #   401 token/identity not yet propagated, 403 firewall/network/RBAC drift
        #   (e.g. the documented Cosmos publicNetworkAccess flip to Disabled by a tenant
        #   policy remediation), 408 request timeout, 429 throttling.
        _TRANSIENT_DATA_PLANE_CODES = frozenset({401, 403, 408, 429})

        @app.exception_handler(AzureError)
        async def _azure_unavailable(_request: Request, exc: AzureError):
            # Preserve 500 semantics for an unexpected non-transient 4xx (a client/code
            # bug); treat connectivity, auth, firewall, throttling and 5xx as transient.
            status_code = getattr(exc, "status_code", None)
            if (
                HttpResponseError is not None
                and isinstance(exc, HttpResponseError)
                and isinstance(status_code, int)
                and 400 <= status_code < 500
                and status_code not in _TRANSIENT_DATA_PLANE_CODES
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
    app.include_router(mcp_servers_router.router)
    app.include_router(workflows_router.router)
    app.include_router(sessions_router.router)
    app.include_router(chat_router.router)
    app.include_router(documents_router.router)
    app.include_router(images_router.router)
    app.include_router(videos_router.router)
    app.include_router(docprocessing_router.router)
    app.include_router(library_router.router)
    app.include_router(voice_router.router)
    app.include_router(realtime_router.router)
    app.include_router(usage_router.router)
    app.include_router(entitlements_router.self_router)
    app.include_router(entitlements_router.admin_router)
    return app


app = create_app()
