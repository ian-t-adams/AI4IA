"""Entitlement endpoints.

- ``GET /api/entitlement`` — any authenticated user reads their OWN effective
  policy (read-only). Lets the UI show "unlimited" or the active caps.
- ``/api/admin/entitlements`` — admin-only management (list / get / set / clear)
  of per-user overrides. Admin is gated by :func:`require_admin` (see its module
  docstring for the dev-auth threat model). Setting an override is a full
  REPLACE: any limit omitted from the body becomes unlimited for that dimension.

The default (no-override) policy ships unlimited, so ``GET /api/entitlement``
returns ``isUnlimited: true`` for everyone until an admin sets a limit. An
unrestricted admin (``auth.identity.admin_is_unrestricted``) reads
``source: "admin"`` and ``isUnlimited: true`` whatever the default or an override
says; the admin endpoints below still show and manage the stored overrides,
including an admin's own.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel

from ..auth.admin import require_admin
from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..entitlements.models import Entitlement, EntitlementLimits
from ..entitlements.service import EntitlementService
from ..policy.models import PolicyDecision, PolicyError

self_router = APIRouter(prefix="/api/entitlement", tags=["entitlements"])
admin_router = APIRouter(prefix="/api/admin/entitlements", tags=["entitlements-admin"])


class EntitlementView(BaseModel):
    """A user-facing projection of an effective entitlement (hides the internal
    default and admin sentinel ids; adds ``source`` and ``isUnlimited``)."""

    userId: str
    # "override" | "default" | "policy" | "admin". "admin" means an unrestricted
    # admin: per-user caps don't apply to the caller's own usage.
    source: str
    isUnlimited: bool
    disabled: bool = False
    requestsPerMinute: int | None = None
    tokensPerDay: int | None = None
    costPerDayMicroUsd: int | None = None
    tokensPerMonth: int | None = None
    costPerMonthMicroUsd: int | None = None
    computeExecutionsPerDay: int | None = None
    note: str | None = None
    updatedAt: datetime | None = None
    updatedBy: str | None = None

    @classmethod
    def of(cls, user_id: str, ent: Entitlement) -> "EntitlementView":
        # Override docs carry the real userId; the default sentinel does not.
        is_override = ent.userId == user_id
        # A group restriction composed onto an admin's base is policy, not admin.
        source = (
            "admin" if ent.is_unrestricted_admin and ent.is_unlimited
            else "override" if is_override else "default"
        )
        return cls(
            userId=user_id,
            source=source,
            isUnlimited=ent.is_unlimited,
            disabled=ent.disabled,
            requestsPerMinute=ent.requestsPerMinute,
            tokensPerDay=ent.tokensPerDay,
            costPerDayMicroUsd=ent.costPerDayMicroUsd,
            tokensPerMonth=ent.tokensPerMonth,
            costPerMonthMicroUsd=ent.costPerMonthMicroUsd,
            computeExecutionsPerDay=ent.computeExecutionsPerDay,
            note=ent.note,
            updatedAt=ent.updatedAt if is_override else None,
            updatedBy=ent.updatedBy if is_override else None,
        )


def _service(request: Request) -> EntitlementService:
    return request.app.state.entitlements


@self_router.get("", response_model=EntitlementView)
async def get_my_entitlement(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> EntitlementView:
    svc = _service(request)
    policy = getattr(request.app.state, "policy", None)
    if policy is not None and policy.enabled:
        effective = await policy.resolve(user)
        if effective.limits_unavailable or effective.spend_invalid:
            raise PolicyError(PolicyDecision("unavailable", "policy_unavailable"))
        view = EntitlementView.of(user.internal_user_id, effective.limits)
        if view.source != "admin":
            view.source = "policy"
        return view
    return EntitlementView.of(user.internal_user_id, await svc.get_effective(user.internal_user_id))


@admin_router.get("", response_model=list[Entitlement])
async def list_overrides(
    request: Request,
    _admin: AuthenticatedUser = Depends(require_admin),
) -> list[Entitlement]:
    return await _service(request).list_overrides()


@admin_router.get("/{user_id}", response_model=EntitlementView)
async def get_user_entitlement(
    user_id: str,
    request: Request,
    _admin: AuthenticatedUser = Depends(require_admin),
) -> EntitlementView:
    # The stored policy, even for the admin's own id: management shows what is set.
    ent = await _service(request).get_effective(user_id, exempt_admin=False)
    return EntitlementView.of(user_id, ent)


@admin_router.put("/{user_id}", response_model=EntitlementView)
async def set_user_entitlement(
    user_id: str,
    limits: EntitlementLimits,
    request: Request,
    admin: AuthenticatedUser = Depends(require_admin),
) -> EntitlementView:
    ent = await _service(request).set(user_id, limits, updated_by=admin.subject)
    return EntitlementView.of(user_id, ent)


@admin_router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def clear_user_entitlement(
    user_id: str,
    request: Request,
    _admin: AuthenticatedUser = Depends(require_admin),
) -> Response:
    await _service(request).clear(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
