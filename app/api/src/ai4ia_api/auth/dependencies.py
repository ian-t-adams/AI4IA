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
        user = await provider.authenticate(credentials)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    # Best-effort: capture the token's display name/email into the admin-only
    # user directory so the hashed userId can be resolved to a name later. Guarded
    # (the service may be absent in tests), deduped + non-blocking inside capture(),
    # and must NEVER raise into the request path.
    directory = getattr(request.app.state, "user_directory", None)
    if directory is not None:
        try:
            directory.capture(user)
        except Exception:  # noqa: BLE001 - capture is strictly best-effort
            pass
    return user

