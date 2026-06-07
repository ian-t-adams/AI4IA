"""Deterministic canonical internal user IDs, decoupled from the IdP subject.

Wrapped behind a small provider so the derivation (today: a UUIDv5 over a
canonicalized identity tuple) can later be swapped for a lookup table without
touching call sites. The tuple includes the provider + issuer + tenant so the
same subject across different IdPs/tenants never collides.
"""
from __future__ import annotations

import uuid

# Fixed namespace for AI4IA internal user IDs (do not change — IDs are stable).
_NAMESPACE = uuid.UUID("6b3c4f9a-2d1e-5a7b-9c8d-0e1f2a3b4c5d")


def internal_user_id(
    *, provider: str, issuer: str, subject: str, tenant_id: str | None = None
) -> str:
    canonical = "|".join([provider, issuer or "", tenant_id or "", subject])
    return str(uuid.uuid5(_NAMESPACE, canonical))


class InternalUserIdProvider:
    """Indirection point for the internal-user-id strategy."""

    def derive(
        self, *, provider: str, issuer: str, subject: str, tenant_id: str | None = None
    ) -> str:
        return internal_user_id(
            provider=provider, issuer=issuer, subject=subject, tenant_id=tenant_id
        )
