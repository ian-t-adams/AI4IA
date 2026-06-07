"""Core auth types shared by every provider."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class AuthError(Exception):
    """Raised when a request cannot be authenticated."""


@dataclass(frozen=True)
class AuthCredentials:
    """Decoupled view of the inbound request used by auth providers."""

    token: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        # Case-insensitive lookup without importing Starlette types.
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


class AuthenticatedUser(BaseModel):
    """Canonical identity. ``internal_user_id`` is decoupled from the IdP OID
    so the data layer never partitions on a provider-specific subject."""

    internal_user_id: str
    subject: str
    issuer: str
    tenant_id: str | None = None
    provider: str
    name: str | None = None
    email: str | None = None
    claims: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class AuthProvider(Protocol):
    """Validates credentials and returns a canonical user (or raises AuthError)."""

    async def authenticate(self, credentials: AuthCredentials) -> AuthenticatedUser: ...
