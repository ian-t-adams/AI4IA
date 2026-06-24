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

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from ..auth.admin import evaluate_admin, require_admin
from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..metrics.models import ResourceMetricsReport
from ..metrics.service import ResourceMetricsService
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
    plane only). They are ``None`` until the user has signed in at least once since
    the directory shipped — the hashed ``userId`` is irreversible, so there is no
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
    effort and ``None`` until the user signs in post-deploy, degrading to the hash.
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
    _admin: AuthenticatedUser = Depends(require_admin),
) -> AdminUserAgentsResponse:
    report = await _service(request).user_agents(days=days)
    # Best-effort enrich each (user, agent) cell with the directory display name,
    # resolving only the userIds already present in the cross-tab. Aggregation math
    # is untouched; we only attach optional displayName/email (None -> hash in UI).
    directory = await _resolve_directory(
        request, [cell.userId for cell in report.userAgents]
    )
    rows = []
    for cell in report.userAgents:
        name, email = _directory_fields(directory.get(cell.userId))
        rows.append(
            AdminUserAgentRow(**cell.model_dump(), displayName=name, email=email)
        )
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

    # Best-effort enrich the page with directory display names (only the userIds
    # on this page; None -> the UI shows the short hash).
    directory = await _resolve_directory(request, [b.userId for b in report.byUser])

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
