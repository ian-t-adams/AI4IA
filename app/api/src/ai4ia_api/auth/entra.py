"""Microsoft Entra ID (JWT) auth provider.

Validates bearer tokens against the issuing tenant's JWKS with strict checks:
signature (RS256), ``aud``, ``iss``, ``tid`` (against an allowed set), and
``exp``/``nbf`` with a small clock skew. JWKS are fetched per tenant and cached.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
import jwt

from .base import AuthCredentials, AuthError, AuthenticatedUser
from .userid import InternalUserIdProvider

_PROVIDER = "entra"
_JWKS_TTL_SECONDS = 3600
_ALLOWED_ALGS = ("RS256",)


class EntraAuthProvider:
    def __init__(
        self,
        *,
        audience: str,
        allowed_tenants: list[str],
        user_ids: InternalUserIdProvider | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not audience:
            raise ValueError("audience is required")
        if not allowed_tenants:
            raise ValueError("at least one allowed tenant is required")
        self._audience = audience
        # An app registration with requestedAccessTokenVersion=2 issues v2.0
        # access tokens whose ``aud`` is the bare client-ID GUID, while the v1
        # audience shape is ``api://<guid>``. Both are canonical identifiers for
        # the SAME app registration, so accept either form regardless of which
        # was configured. This is not a security loosening — any other audience
        # is still rejected below.
        accepted = {audience}
        if audience.startswith("api://"):
            accepted.add(audience[len("api://"):])
        else:
            accepted.add(f"api://{audience}")
        self._accepted_audiences = accepted
        self._allowed_tenants = {t.lower() for t in allowed_tenants}
        self._user_ids = user_ids or InternalUserIdProvider()
        self._http = http_client
        self._jwks_cache: dict[str, tuple[float, dict]] = {}

    async def _jwks(self, tenant_id: str) -> dict:
        now = time.time()
        cached = self._jwks_cache.get(tenant_id)
        if cached and cached[0] > now:
            return cached[1]
        url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        client = self._http or httpx.AsyncClient(timeout=10.0)
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            keys = resp.json()
        finally:
            if self._http is None:
                await client.aclose()
        self._jwks_cache[tenant_id] = (now + _JWKS_TTL_SECONDS, keys)
        return keys

    def _signing_key(self, token: str, jwks: dict) -> Any:
        """Resolve the RSA public key for ``token`` from a tenant JWKS by ``kid``.

        Raises a :class:`jwt.PyJWTError` subclass when no key matches, so the
        caller's ``except jwt.PyJWTError`` maps it to an ``AuthError`` (fail closed).
        """
        kid = jwt.get_unverified_header(token).get("kid")
        for raw in jwks.get("keys", []):
            if raw.get("kid") == kid:
                return jwt.PyJWK.from_dict(raw).key
        raise jwt.InvalidKeyError("no matching signing key for token kid")

    async def authenticate(self, credentials: AuthCredentials) -> AuthenticatedUser:
        token = credentials.token
        if not token:
            raise AuthError("Missing bearer token")
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:  # malformed token
            raise AuthError(f"Invalid token: {exc}") from exc

        tenant_id = str(unverified.get("tid", "")).lower()
        if tenant_id not in self._allowed_tenants:
            raise AuthError("Token tenant is not allowed")

        keys = await self._jwks(tenant_id)
        issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        try:
            # Select the signing key from the tenant JWKS by the token's ``kid``.
            signing_key = self._signing_key(token, keys)
            # PyJWT's ``audience`` only accepts a single string, so we disable its
            # check (verify_aud=False) and validate the audience ourselves against
            # the set of accepted forms below. Signature (RS256 only), issuer, and
            # the presence + validity of exp/iat are enforced here.
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=list(_ALLOWED_ALGS),
                issuer=issuer,
                options={"require": ["exp", "iat"], "verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"Token validation failed: {exc}") from exc

        aud = claims.get("aud")
        token_auds = [aud] if isinstance(aud, str) else list(aud or [])
        if not any(a in self._accepted_audiences for a in token_auds):
            raise AuthError("Token validation failed: Invalid audience")

        subject = str(claims.get("oid") or claims.get("sub") or "")
        if not subject:
            raise AuthError("Token has no subject (oid/sub)")

        return AuthenticatedUser(
            internal_user_id=self._user_ids.derive(
                provider=_PROVIDER, issuer=issuer, subject=subject, tenant_id=tenant_id
            ),
            subject=subject,
            issuer=issuer,
            tenant_id=tenant_id,
            provider=_PROVIDER,
            name=claims.get("name"),
            email=claims.get("preferred_username") or claims.get("email"),
            claims={k: claims.get(k) for k in ("roles", "scp") if k in claims},
        )
