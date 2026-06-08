"""Auth abstractions: pluggable providers yield a canonical AuthenticatedUser."""

from .base import AuthCredentials, AuthenticatedUser, AuthError, AuthProvider
from .factory import build_auth_provider
from .userid import internal_user_id

__all__ = [
    "AuthCredentials",
    "AuthenticatedUser",
    "AuthError",
    "AuthProvider",
    "build_auth_provider",
    "internal_user_id",
]
