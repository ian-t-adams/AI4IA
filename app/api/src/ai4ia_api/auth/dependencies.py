"""FastAPI dependency that resolves the current user via the wired provider."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..logging_setup import emit_security_block
from ..gateway.priority import resolve_priority, set_request_priority
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
        emit_security_block("http_auth", "authentication_failed", "auth_dependency")
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
    # Resolve the SimpleL7Proxy priority band here, at the one point where a
    # request's principal is known to be authentic, and stash it for the gateway
    # client. Doing it anywhere downstream would risk deriving it from something
    # the caller controls. See ai4ia_api.gateway.priority.
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.proxy_priorities_enabled:
        set_request_priority(resolve_priority(user, settings))
    else:
        set_request_priority(None)
    return user
