"""Admin authorization for the entitlement-management API.

Threat model: Entra auth is the live deployed posture, but the code still
supports an explicitly opted-in dev provider for local and break-glass
environments. Dev auth derives identity from the client-supplied
``X-Dev-User`` header — i.e. it is spoofable. Gating admin solely on
``subject in admin_subjects`` would let any caller impersonate an admin and
disable users or lift limits. So:

- Under **non-spoofable** auth (entra) or **local** dev, an admin is anyone in
  the configured subject/email allowlist or carrying an ``admin`` role claim.
- Under **dev auth in a deployed env** the identity is untrusted, so admin
  requires a matching server-side ``X-Admin-Secret`` (constant-time compared).
  With no secret configured there, admin is **fail-closed** (nobody is admin).
- When a secret IS configured, it is always required (a second factor) on top of
  identity, in every environment.

This keeps the management API usable in an explicitly configured deployed
dev-auth environment (set a secret) without opening a privilege-escalation hole.
"""
from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status

from ..config import Settings
from ..logging_setup import emit_security_block
from ..policy.models import ADMIN_OPERATIONS, PolicyError, PolicyRequest
from ..policy.routes import admin_operations
from .base import AuthenticatedUser
from .dependencies import get_current_user
from .identity import identity_is_admin


def _secret_ok(provided: str | None, expected: str) -> bool:
    return bool(provided) and hmac.compare_digest(provided, expected)


def evaluate_admin(
    user: AuthenticatedUser, settings: Settings, provided_secret: str | None
) -> bool:
    """Return whether this request is authorized as admin (never raises).

    Single source of truth for the dev-auth threat model (see module docstring),
    shared by :func:`require_admin` (which raises on False) and the read-only
    ``/api/admin/whoami`` probe (which reports the boolean so the UI can hide an
    admin entry without ever being the security boundary itself).
    """
    secret = settings.admin_api_secret
    spoofable = settings.auth_provider_is_spoofable

    if spoofable:
        # Identity can't be trusted here; only the shared secret authorizes.
        return bool(secret) and _secret_ok(provided_secret, secret)

    # Trustworthy identity (entra) or local dev: allowlist/role governs...
    if not identity_is_admin(user, settings):
        return False
    # ...and the secret, when configured, is required as a second factor.
    if secret and not _secret_ok(provided_secret, secret):
        return False
    return True


async def require_admin(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    settings: Settings = request.app.state.settings
    provided_secret = request.headers.get("X-Admin-Secret")
    legacy = evaluate_admin(user, settings, provided_secret)
    policy = getattr(request.app.state, "policy", None)
    allowed = legacy
    if policy is not None and policy.enabled:
        actor = await policy.resolve(user)
        allowed = not settings.auth_provider_is_spoofable and (
            not settings.admin_api_secret or _secret_ok(provided_secret, settings.admin_api_secret)
        )
        for operation in admin_operations(request):
            decision = await policy.authorize(
                actor, PolicyRequest(operation, legacy_admin=legacy),
            )
            if decision.outcome == "unavailable":
                raise PolicyError(decision)
            allowed = allowed and decision.allowed
    if not allowed:
        emit_security_block("admin_auth", "privileges_required", "admin_dependency")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required."
        )
    return user


async def authorized_admin_operations(request: Request, user: AuthenticatedUser) -> list[str]:
    settings: Settings = request.app.state.settings
    provided = request.headers.get("X-Admin-Secret")
    legacy = evaluate_admin(user, settings, provided)
    policy = getattr(request.app.state, "policy", None)
    if policy is None or not policy.enabled:
        return sorted(ADMIN_OPERATIONS) if legacy else []
    if settings.auth_provider_is_spoofable or (
        settings.admin_api_secret and not _secret_ok(provided, settings.admin_api_secret)
    ):
        return []
    actor = await policy.resolve(user)
    return sorted(
        operation for operation in ADMIN_OPERATIONS
        if policy.decide(actor, PolicyRequest(operation, legacy_admin=legacy)).allowed
    )
