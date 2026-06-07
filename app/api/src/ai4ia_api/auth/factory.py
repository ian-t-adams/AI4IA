"""Selects and constructs the configured auth provider."""
from __future__ import annotations

from ..config import AuthProviderKind, Settings
from .base import AuthProvider
from .dev import DevAuthProvider
from .entra import EntraAuthProvider


def build_auth_provider(settings: Settings) -> AuthProvider:
    if settings.auth_provider == AuthProviderKind.dev:
        if not settings.dev_auth_permitted:
            raise RuntimeError("Dev auth requested but not permitted in this environment.")
        return DevAuthProvider(
            default_sub=settings.dev_user_sub,
            default_name=settings.dev_user_name,
            default_email=settings.dev_user_email,
            allow_header_override=True,
        )

    if settings.auth_provider == AuthProviderKind.entra:
        if not settings.entra_audience:
            raise RuntimeError("Entra auth requires AI4IA_ENTRA_AUDIENCE.")
        return EntraAuthProvider(
            audience=settings.entra_audience,
            allowed_tenants=settings.allowed_tenants,
        )

    raise RuntimeError(f"Unsupported auth provider: {settings.auth_provider}")
