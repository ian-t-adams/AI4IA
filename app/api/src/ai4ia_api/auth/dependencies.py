"""FastAPI dependency that resolves the current user via the wired provider."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .base import AuthCredentials, AuthError, AuthenticatedUser

# auto_error=False so dev auth (header-based) works without an Authorization header.
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthenticatedUser:
    provider = request.app.state.auth_provider
    credentials = AuthCredentials(
        token=creds.credentials if creds else None,
        headers=dict(request.headers),
    )
    try:
        return await provider.authenticate(credentials)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

