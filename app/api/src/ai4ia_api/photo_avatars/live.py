"""Server-only live avatar grants for the real-time relay. Never an HTTP surface.

When a Voice Live session names one of the caller's avatar records, the relay
calls :func:`resolve_live_avatar`. The returned :class:`LiveAvatarGrant` is the
only way the provider's avatar id leaves this package, and it must never be
serialized to a client: the relay places it in the upstream session
configuration it builds itself. The resolver re-checks ownership, readiness and
the same availability predicate as creation, with the ``avatar.use`` policy
operation enforced (not a display snapshot).

Every refusal is a :class:`LiveAvatarError` whose ``code`` is one of
:data:`LIVE_AVATAR_ERROR_CODES`:

* ``not_found`` -- a malformed id, an unknown id and another user's id are
  deliberately indistinguishable;
* ``avatar_not_ready`` -- not ``ready`` (or no stored preview or provider id);
* ``avatar_needs_reverification`` -- a live session reported that the avatar
  failed verification; ``retry_after`` says when a re-check may run;
* ``avatar_home_changed`` -- the record lives in a different home account from
  the one this deployment routes to;
* ``photo_avatars_unavailable`` -- the availability predicate refused; ``reason``
  carries the :data:`~.models.AvailabilityReason` (including ``disabled``);
* ``policy_denied`` -- application policy denies ``avatar.use`` for the caller.

When Voice Live reports that an avatar failed verification, the relay calls
:func:`mark_live_avatar_verification_failed`. Live use is then refused until a
fresh provider read, at most once per cooldown, still finds the avatar
``Succeeded``; the record, its preview and deletion stay available throughout.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

from .models import PhotoAvatarError

if TYPE_CHECKING:
    from ..auth.base import AuthenticatedUser

LiveAvatarErrorCode = Literal[
    "not_found",
    "avatar_not_ready",
    "avatar_needs_reverification",
    "avatar_home_changed",
    "photo_avatars_unavailable",
    "policy_denied",
]
LIVE_AVATAR_ERROR_CODES: frozenset[str] = frozenset(get_args(LiveAvatarErrorCode))


@dataclass(frozen=True, slots=True)
class LiveAvatarGrant:
    """Authority to name one ready avatar in one live session. Server-side only."""

    record_id: str
    provider_avatar_id: str = field(repr=False)
    base_model: str
    home_region: str


class LiveAvatarError(PhotoAvatarError):
    """A refused live grant. ``code`` is one of :data:`LIVE_AVATAR_ERROR_CODES`."""


def _disabled() -> LiveAvatarError:
    return LiveAvatarError(
        503, "photo_avatars_unavailable", "Photo avatars are unavailable.", reason="disabled",
    )


async def resolve_live_avatar(state: Any, user: AuthenticatedUser, record_id: str) -> LiveAvatarGrant:
    """Resolve ``record_id`` for ``user`` from app state, or raise :class:`LiveAvatarError`.

    Call it after the relay has authenticated the caller and bound application
    policy for them (``bind_authenticated``). While photo avatars are disabled
    the service does not exist, which this reports as unavailable/disabled.
    """
    service = getattr(state, "photo_avatars", None)
    settings = getattr(state, "settings", None)
    if service is None or not getattr(settings, "photo_avatars_enabled", False):
        raise _disabled()
    return await service.resolve_live_avatar(user, record_id)


async def mark_live_avatar_verification_failed(
    state: Any, user: AuthenticatedUser, record_id: str, *, provider_code: str | None = None,
) -> bool:
    """Record that a live session reported the avatar failed verification.

    Returns whether one of the caller's ``ready`` records was marked. Unknown,
    foreign, malformed and not-ready ids are ignored, as is a disabled feature.
    """
    service = getattr(state, "photo_avatars", None)
    if service is None:
        return False
    return await service.mark_live_avatar_verification_failed(
        user, record_id, provider_code=provider_code,
    )
