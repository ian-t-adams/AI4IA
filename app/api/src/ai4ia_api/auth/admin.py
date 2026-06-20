"""Admin authorization for the entitlement-management API.

Threat model: the deployed environment currently runs the *dev* auth provider,
which derives identity from the client-supplied ``X-Dev-User`` header — i.e. it
is spoofable. Gating admin solely on ``subject in admin_subjects`` would let any
caller impersonate an admin and disable users or lift limits. So:

- Under **non-spoofable** auth (entra) or **local** dev, an admin is anyone in
  the configured subject/email allowlist or carrying an ``admin`` role claim.
- Under **dev auth in a deployed env** the identity is untrusted, so admin
  requires a matching server-side ``X-Admin-Secret`` (constant-time compared).
  With no secret configured there, admin is **fail-closed** (nobody is admin).
- When a secret IS configured, it is always required (a second factor) on top of
  identity, in every environment.

This keeps the management API usable on a personal demo (set a secret) without
opening a privilege-escalation hole.
"""
from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status

from ..config import AuthProviderKind, Environment, Settings
from .base import AuthenticatedUser
from .dependencies import get_current_user


def _has_admin_role(user: AuthenticatedUser) -> bool:
    roles = user.claims.get("roles")
    return isinstance(roles, list) and "admin" in roles


def _secret_ok(provided: str | None, expected: str) -> bool:
    return bool(provided) and hmac.compare_digest(provided, expected)


def _identity_is_admin(user: AuthenticatedUser, settings: Settings) -> bool:
    if user.subject in settings.admin_subject_set:
        return True
    if user.email and user.email.lower() in settings.admin_email_set:
        return True
    return _has_admin_role(user)


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
    spoofable = (
        settings.auth_provider == AuthProviderKind.dev
        and settings.env != Environment.local
    )

    if spoofable:
        # Identity can't be trusted here; only the shared secret authorizes.
        return bool(secret) and _secret_ok(provided_secret, secret)

    # Trustworthy identity (entra) or local dev: allowlist/role governs...
    if not _identity_is_admin(user, settings):
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
    if not evaluate_admin(user, settings, provided_secret):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required."
        )
    return user
