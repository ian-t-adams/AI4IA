"""Local development auth provider.

Returns a fixed identity (optionally overridden by the ``X-Dev-User`` header so
multiple users can be simulated locally). This provider is fail-closed: the
factory only wires it when settings permit dev auth, and it ignores
``X-Dev-User`` if that override is disabled.
"""
from __future__ import annotations

from .base import AuthCredentials, AuthenticatedUser
from .userid import InternalUserIdProvider

_ISSUER = "ai4ia-dev"
_PROVIDER = "dev"
_TENANT = "dev"


class DevAuthProvider:
    def __init__(
        self,
        *,
        default_sub: str,
        default_name: str,
        default_email: str,
        allow_header_override: bool = True,
        user_ids: InternalUserIdProvider | None = None,
    ) -> None:
        self._default_sub = default_sub
        self._default_name = default_name
        self._default_email = default_email
        self._allow_header_override = allow_header_override
        self._user_ids = user_ids or InternalUserIdProvider()

    async def authenticate(self, credentials: AuthCredentials) -> AuthenticatedUser:
        sub = self._default_sub
        name = self._default_name
        email = self._default_email
        if self._allow_header_override:
            override = credentials.header("X-Dev-User")
            if override:
                sub = override
                name = override
                email = f"{override}@example.com"
        return AuthenticatedUser(
            internal_user_id=self._user_ids.derive(
                provider=_PROVIDER, issuer=_ISSUER, subject=sub, tenant_id=_TENANT
            ),
            subject=sub,
            issuer=_ISSUER,
            tenant_id=_TENANT,
            provider=_PROVIDER,
            name=name,
            email=email,
        )
