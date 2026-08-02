"""Admin usage + resource-metrics API.

Org-level aggregation over the existing per-user usage ledger, plus best-effort
Azure Monitor resource panels — for the app admin's "how many users, tokens,
models, agents, AI Search/Postgres usage" dashboard.

Gating (the real security boundary):
- Every ``/api/admin/usage/*`` and ``/api/admin/metrics/*`` route is behind
  :func:`require_admin` (admin 200 / non-admin 403 / anon 401).
- ``GET /api/admin/whoami`` is the one exception: it only requires authentication
  and returns an ``isAdmin`` boolean so the web client can *hide* an admin nav
  entry. It never gates anything — the server-side ``require_admin`` does.

Everything here is read-only: aggregation is a bounded, cross-partition ledger
scan; the resource panels are read from Azure Monitor. The only mutations in the
admin surface remain the pre-existing entitlement PUT/DELETE (routers/entitlements).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..auth.admin import evaluate_admin, require_admin
from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..metrics.models import OperationalMetricsReport, ResourceMetricsReport
from ..metrics.operations import OperationsMetricsService
from ..metrics.service import ResourceMetricsService
from ..websearch.health import WebSearchHealth, WebSearchHealthReport
from ..usage.aggregate import (
    MAX_ADMIN_DAYS,
    AdminByDayReport,
    AdminByModelReport,
    AdminDistributionsReport,
    AdminUsageService,
    AdminUsageSummary,
    AdminUsageWindow,
    UserAgentBucket,
    UserUsageBucket,
)
from ..directory.model import UserDirectoryEntry
from .entitlements import EntitlementView

whoami_router = APIRouter(prefix="/api/admin", tags=["admin"])
router = APIRouter(prefix="/api/admin", tags=["admin-usage"])

_DAYS = Query(default=30, ge=1, le=MAX_ADMIN_DAYS, description="Window in days (1-90).")
_IDENTIFY = Query(
    default=False,
    description=(
        "When true, enrich admin-only usage rows with displayName/email from the "
        "user directory. Defaults false so dashboard responses are demo-safe."
    ),
)
_MCP_REFRESH = Query(
    default=False,
    description=(
        "When true, drop the official-MCP discovery cache before probing so the "
        "report reflects the upstream right now rather than a cached verdict."
    ),
)


class OfficialMcpServerHealth(BaseModel):
    """Per-server outcome of a full MCP discovery (``initialize`` + ``tools/list``).

    ``toolCount == 0`` alongside a non-null ``lastError`` means the server was
    reachable through APIM but returned no usable tool list — the signature of a
    missing/misprovisioned upstream. ``lastError`` is the connector's already
    bounded, redacted summary; it never carries a credential.
    """

    name: str
    displayName: str
    toolCount: int
    lastConnectedAt: datetime | None = None
    lastError: str | None = None


class OfficialMcpHealthReport(BaseModel):
    """Admin-plane view of the curated official MCP plane for this replica.

    ``enabled`` and ``gatewayConfigured`` are config posture, so an empty
    ``servers`` list is never ambiguous: enabled+configured with no servers means
    the catalog is empty, while ``enabled`` false means the plane is simply off.
    """

    enabled: bool = False
    gatewayConfigured: bool = False
    servers: list[OfficialMcpServerHealth] = []
    generatedAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WhoAmI(BaseModel):
    subject: str
    isAdmin: bool
    email: str | None = None
    name: str | None = None


class AdminUserRow(UserUsageBucket):
    """A top-spender row joined to the user's entitlement override (if any).

    ``entitlement`` is ``None`` when the user has no override — i.e. the shipped
    unlimited default — so the UI shows "Unlimited" without an extra call.

    ``displayName``/``email`` come from the admin-only user directory (PII, admin
    plane only) only when the request opts into identified mode. They are ``None``
    in de-identified mode or until the user has signed in at least once since the
    directory shipped — the hashed ``userId`` is irreversible, so there is no
    historical backfill and the UI degrades to the short hash.
    """

    entitlement: EntitlementView | None = None
    displayName: str | None = None
    email: str | None = None


class AdminByUserResponse(BaseModel):
    sinceDays: int
    fromTime: datetime
    toTime: datetime
    truncated: bool
    scannedRecords: int
    totalUsers: int
    limit: int
    offset: int
    byUser: list[AdminUserRow]


class AdminUserAgentRow(UserAgentBucket):
    """A (user, agent) cross-tab cell enriched with the directory display name.

    Same PII posture as :class:`AdminUserRow`: ``displayName``/``email`` are best-
    effort, populated only in identified mode, and ``None`` otherwise.
    """

    displayName: str | None = None
    email: str | None = None


class AdminUserAgentsResponse(AdminUsageWindow):
    userAgents: list[AdminUserAgentRow] = []


async def _resolve_directory(
    request: Request, user_ids: list[str]
) -> dict[str, UserDirectoryEntry]:
    """Best-effort batch-resolve userIds to directory entries for enrichment.

    Bounded to the ids already in the response and never raises: a missing service
    or any store error degrades to an empty mapping (UI falls back to the hash)."""
    directory = getattr(request.app.state, "user_directory", None)
    if directory is None:
        return {}
    try:
        return await directory.resolve(user_ids)
    except Exception:  # noqa: BLE001 - enrichment is advisory; never fail the read
        return {}


def _directory_fields(
    entry: UserDirectoryEntry | None,
) -> tuple[str | None, str | None]:
    """Optional (displayName, email) for a row, defaulting to None (-> hash in UI)."""
    if entry is None:
        return None, None
    return entry.displayName, entry.email


def _service(request: Request) -> AdminUsageService:
    return request.app.state.admin_usage


# ---- whoami (auth-only; powers cosmetic UI hide) ----


@whoami_router.get("/whoami", response_model=WhoAmI)
async def whoami(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> WhoAmI:
    settings = request.app.state.settings
    is_admin = evaluate_admin(user, settings, request.headers.get("X-Admin-Secret"))
    return WhoAmI(subject=user.subject, isAdmin=is_admin, email=user.email, name=user.name)


# ---- usage aggregation (all require_admin) ----


@router.get("/usage/summary", response_model=AdminUsageSummary)
async def usage_summary(
    request: Request,
    days: int = _DAYS,
    _admin: AuthenticatedUser = Depends(require_admin),
) -> AdminUsageSummary:
    return await _service(request).summary(days=days)


@router.get("/usage/by-model", response_model=AdminByModelReport)
async def usage_by_model(
    request: Request,
    days: int = _DAYS,
    _admin: AuthenticatedUser = Depends(require_admin),
) -> AdminByModelReport:
    return await _service(request).by_model(days=days)


@router.get("/usage/by-day", response_model=AdminByDayReport)
async def usage_by_day(
    request: Request,
    days: int = _DAYS,
    _admin: AuthenticatedUser = Depends(require_admin),
) -> AdminByDayReport:
    return await _service(request).by_day(days=days)


@router.get("/usage/agents")
async def usage_agents(
    request: Request,
    days: int = _DAYS,
    _admin: AuthenticatedUser = Depends(require_admin),
):
    return await _service(request).agents(days=days)


@router.get("/usage/user-agents", response_model=AdminUserAgentsResponse)
async def usage_user_agents(
    request: Request,
    days: int = _DAYS,
    identify: bool = _IDENTIFY,
    _admin: AuthenticatedUser = Depends(require_admin),
) -> AdminUserAgentsResponse:
    report = await _service(request).user_agents(days=days)
    # Best-effort enrich each (user, agent) cell only when explicitly requested.
    # Aggregation math is untouched; the stable hashed userId remains the row key.
    directory = (
        await _resolve_directory(request, [cell.userId for cell in report.userAgents])
        if identify
        else {}
    )
    rows = []
    for cell in report.userAgents:
        name, email = _directory_fields(directory.get(cell.userId))
        rows.append(AdminUserAgentRow(**cell.model_dump(), displayName=name, email=email))
    return AdminUserAgentsResponse(
        sinceDays=report.sinceDays,
        fromTime=report.fromTime,
        toTime=report.toTime,
        truncated=report.truncated,
        scannedRecords=report.scannedRecords,
        userAgents=rows,
    )


@router.get("/usage/distributions", response_model=AdminDistributionsReport)
async def usage_distributions(
    request: Request,
    days: int = _DAYS,
    _admin: AuthenticatedUser = Depends(require_admin),
) -> AdminDistributionsReport:
    return await _service(request).distributions(days=days)


@router.get("/usage/by-user", response_model=AdminByUserResponse)
async def usage_by_user(
    request: Request,
    days: int = _DAYS,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identify: bool = _IDENTIFY,
    _admin: AuthenticatedUser = Depends(require_admin),
) -> AdminByUserResponse:
    report = await _service(request).by_user(days=days, limit=limit, offset=offset)
    # Join the page to entitlement overrides in a single store read (users with
    # no override are the unlimited default -> entitlement stays None).
    overrides_by_user: dict[str, EntitlementView] = {}
    try:
        overrides = await request.app.state.entitlements.list_overrides()
        for ent in overrides:
            overrides_by_user[ent.userId] = EntitlementView.of(ent.userId, ent)
    except Exception:  # noqa: BLE001 - the join is advisory; never fail the report
        overrides_by_user = {}

    # Best-effort enrich the page with directory display names only in identified
    # mode. De-identified mode leaves PII null so the UI shows only the short hash.
    directory = (
        await _resolve_directory(request, [b.userId for b in report.byUser]) if identify else {}
    )

    rows = []
    for bucket in report.byUser:
        name, email = _directory_fields(directory.get(bucket.userId))
        rows.append(
            AdminUserRow(
                **bucket.model_dump(),
                entitlement=overrides_by_user.get(bucket.userId),
                displayName=name,
                email=email,
            )
        )
    return AdminByUserResponse(
        sinceDays=report.sinceDays,
        fromTime=report.fromTime,
        toTime=report.toTime,
        truncated=report.truncated,
        scannedRecords=report.scannedRecords,
        totalUsers=report.totalUsers,
        limit=report.limit,
        offset=report.offset,
        byUser=rows,
    )


# ---- resource metrics (require_admin; Part B, degrades gracefully) ----


@router.get("/metrics/resources", response_model=ResourceMetricsReport)
async def metrics_resources(
    request: Request,
    _admin: AuthenticatedUser = Depends(require_admin),
) -> ResourceMetricsReport:
    service: ResourceMetricsService = request.app.state.resource_metrics
    return await service.resources()


@router.get("/metrics/operations", response_model=OperationalMetricsReport)
async def metrics_operations(
    request: Request,
    minutes: int = Query(default=60, ge=15, le=1440),
    _admin: AuthenticatedUser = Depends(require_admin),
) -> OperationalMetricsReport:
    service: OperationsMetricsService = request.app.state.operations_metrics
    return await service.operations(window_minutes=minutes)


@router.get("/metrics/security", response_model=OperationalMetricsReport)
async def metrics_security(
    request: Request,
    minutes: int = Query(default=60, ge=15, le=1440),
    _admin: AuthenticatedUser = Depends(require_admin),
) -> OperationalMetricsReport:
    service: OperationsMetricsService = request.app.state.operations_metrics
    return await service.security(window_minutes=minutes)


@router.get("/metrics/web-search", response_model=WebSearchHealthReport)
async def metrics_web_search(
    request: Request,
    _admin: AuthenticatedUser = Depends(require_admin),
) -> WebSearchHealthReport:
    """Process-local Web IQ call health for this replica (diagnostics only).

    The Web IQ capability fails *soft* — a categorized auth/permission/rate_limit/
    connection failure becomes a clean ``{"error": ...}`` and the turn continues —
    so a misconfiguration is otherwise invisible. The recorder is built at startup
    unconditionally (even when the feature is off), so this endpoint always answers.

    ``enabled`` + ``authMode`` are the config posture that explains the failures:
    ``enabled`` with ``authMode == "managed_identity"`` and a run of ``auth``
    failures means the api's managed identity is not entitled to Web IQ;
    ``authMode == "unconfigured"`` means no API key and the Entra fallback is off.
    The report is de-identified (no userId) and process-local (lost on restart, not
    aggregated across replicas) — the durable, cross-replica view is App Insights.
    """
    settings = request.app.state.settings
    health: WebSearchHealth = request.app.state.web_search_health
    report = health.snapshot()
    report.enabled = bool(settings.web_search_enabled)
    if settings.webiq_api_key:
        report.authMode = "api_key"
    elif settings.webiq_use_entra:
        report.authMode = "managed_identity"
    else:
        report.authMode = "unconfigured"
    return report


@router.get("/metrics/official-mcp", response_model=OfficialMcpHealthReport)
async def metrics_official_mcp(
    request: Request,
    refresh: bool = _MCP_REFRESH,
    _admin: AuthenticatedUser = Depends(require_admin),
) -> OfficialMcpHealthReport:
    """Live reachability of each curated official MCP server (diagnostics only).

    The official-MCP plane fails *soft* by design — a server that cannot be
    discovered is simply absent from the tool list so MCP never breaks a turn.
    That softness hides a whole class of provisioning gaps, and one of them
    shipped: the APIM MCP API, product, subscription and managed-identity policy
    were all correct, but the upstream Foundry toolbox itself had never been
    created, so every ``tools/list`` returned "Toolbox not found". Nothing
    surfaced it, because the MCP ``initialize`` handshake still answered 200 —
    only the follow-up ``tools/list`` fails. This endpoint therefore reports the
    result of full discovery (``initialize`` + ``tools/list``), not a ping.

    ``toolCount == 0`` with a non-null ``lastError`` is the signature of exactly
    that failure. Pass ``refresh=true`` to drop the discovery cache first, so an
    operator re-checks after fixing the upstream instead of reading a cached
    verdict.
    """
    settings = request.app.state.settings
    service = getattr(request.app.state, "official_mcp_service", None)
    if service is None:
        # Disabled (or enabled-but-unbuildable): report posture, never 500.
        return OfficialMcpHealthReport(
            enabled=bool(settings.official_mcp_enabled),
            gatewayConfigured=bool(settings.official_mcp_gateway_url),
            servers=[],
        )
    if refresh:
        service.refresh()
    servers = await service.list_all()
    return OfficialMcpHealthReport(
        enabled=bool(settings.official_mcp_enabled),
        gatewayConfigured=bool(settings.official_mcp_gateway_url),
        servers=[
            OfficialMcpServerHealth(
                name=s.name,
                displayName=s.displayName,
                toolCount=len(s.discoveredTools),
                lastConnectedAt=s.lastConnectedAt,
                lastError=s.lastError,
            )
            for s in servers
        ],
    )
