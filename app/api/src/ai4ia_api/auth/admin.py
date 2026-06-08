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


async def require_admin(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    settings: Settings = request.app.state.settings
    secret = settings.admin_api_secret
    provided_secret = request.headers.get("X-Admin-Secret")
    forbidden = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required."
    )

    spoofable = (
        settings.auth_provider == AuthProviderKind.dev
        and settings.env != Environment.local
    )

    if spoofable:
        # Identity can't be trusted here; only the shared secret authorizes.
        if not secret or not _secret_ok(provided_secret, secret):
            raise forbidden
        return user

    # Trustworthy identity (entra) or local dev: allowlist/role governs...
    if not _identity_is_admin(user, settings):
        raise forbidden
    # ...and the secret, when configured, is required as a second factor.
    if secret and not _secret_ok(provided_secret, secret):
        raise forbidden
    return user
