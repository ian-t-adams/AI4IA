"""Builds the photo avatar service only while the feature flag is on."""
from __future__ import annotations

from ..config import Settings
from ..entitlements.service import EntitlementService
from ..usage.service import UsageService
from .availability import CapabilityProbe
from .catalog import load_photo_avatar_catalog
from .preview import PhotoAvatarArtifactStore, build_photo_avatar_blob_store
from .provider import PhotoAvatarGateway
from .service import PhotoAvatarService
from .store import build_photo_avatar_store


def build_photo_avatar_service(
    settings: Settings, *, entitlements: EntitlementService, usage: UsageService,
) -> PhotoAvatarService | None:
    """``None`` while ``AI4IA_PHOTO_AVATARS_ENABLED`` is off: no client, store or Blob."""
    if not settings.photo_avatars_enabled:
        return None
    catalog = load_photo_avatar_catalog()
    gateway = PhotoAvatarGateway(settings)
    return PhotoAvatarService(
        settings=settings,
        catalog=catalog,
        store=build_photo_avatar_store(settings),
        artifacts=PhotoAvatarArtifactStore(build_photo_avatar_blob_store(settings)),
        gateway=gateway,
        capability=CapabilityProbe(gateway, catalog.requiredFeature),
        entitlements=entitlements,
        usage=usage,
        pricing=usage.pricing,
    )
