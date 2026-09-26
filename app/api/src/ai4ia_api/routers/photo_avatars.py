"""Custom photo avatars: owner-scoped create, status, preview, list, delete and report.

The router is deliberately thin. Governance -- the availability predicate,
entitlement and cost checks, limits, the single-attempt provider create and its
reconciliation -- lives in :class:`~ai4ia_api.photo_avatars.service.PhotoAvatarService`.
The service is built only when ``AI4IA_PHOTO_AVATARS_ENABLED`` is on; while it is
off every route except ``/config`` answers 404 and nothing reaches the provider.

Route identity (the function names below) is also the group-policy operation
key: see ``PHOTO_AVATAR_OPERATIONS`` in :mod:`ai4ia_api.policy.routes`.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request, Response, status

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..photo_avatars.models import (
    RECORD_ID_PATTERN,
    CreatePhotoAvatarRequest,
    PhotoAvatar,
    PhotoAvatarApi,
    PhotoAvatarConfig,
    PhotoAvatarError,
    PhotoAvatarList,
    PhotoAvatarReportReceipt,
    PhotoAvatarReportRequest,
)

router = APIRouter(prefix="/api/photo-avatars", tags=["photo-avatars"])

_RECORD_ID = re.compile(RECORD_ID_PATTERN)
PREVIEW_HEADERS = {
    "Cache-Control": "private, max-age=86400",
    "X-Content-Type-Options": "nosniff",
    "Content-Disposition": 'inline; filename="ai-generated-avatar.png"',
    # Machine-readable disclosure alongside the visible label the client renders.
    "X-AI4IA-Synthetic-Media": "ai-generated",
}


def _service(request: Request) -> PhotoAvatarApi | None:
    settings = request.app.state.settings
    service = getattr(request.app.state, "photo_avatars", None)
    if not getattr(settings, "photo_avatars_enabled", False) or service is None:
        return None
    return service


def _enabled_service(request: Request) -> PhotoAvatarApi:
    service = _service(request)
    if service is None:
        raise PhotoAvatarError(
            status.HTTP_404_NOT_FOUND, "photo_avatars_disabled", "Photo avatars are disabled.",
        )
    return service


def _record_id(avatar_id: str) -> str:
    # Constrain the path segment to the exact opaque shape so it can never carry
    # a separator, a traversal or a provider-side identifier.
    if not _RECORD_ID.fullmatch(avatar_id or ""):
        raise PhotoAvatarError(status.HTTP_404_NOT_FOUND, "not_found", "Not found.")
    return avatar_id


@router.get("/config", response_model=PhotoAvatarConfig)
async def get_photo_avatar_config(
    request: Request, user: AuthenticatedUser = Depends(get_current_user),
) -> PhotoAvatarConfig:
    service = _service(request)
    if service is None:
        return PhotoAvatarConfig.disabled()
    return await service.config(user)


@router.get("", response_model=PhotoAvatarList)
async def list_photo_avatars(
    request: Request, user: AuthenticatedUser = Depends(get_current_user),
) -> PhotoAvatarList:
    return await _enabled_service(request).list(user)


@router.post("", response_model=PhotoAvatar, status_code=status.HTTP_202_ACCEPTED)
async def create_photo_avatar(
    body: CreatePhotoAvatarRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PhotoAvatar:
    return await _enabled_service(request).create(user, body)


@router.get("/{avatar_id}", response_model=PhotoAvatar)
async def get_photo_avatar(
    avatar_id: str, request: Request, user: AuthenticatedUser = Depends(get_current_user),
) -> PhotoAvatar:
    service = _enabled_service(request)
    return await service.get(user, _record_id(avatar_id))


@router.get("/{avatar_id}/preview")
async def get_photo_avatar_preview(
    avatar_id: str, request: Request, user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    service = _enabled_service(request)
    data = await service.preview(user, _record_id(avatar_id))
    return Response(content=data, media_type="image/png", headers=PREVIEW_HEADERS)


@router.delete("/{avatar_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_photo_avatar(
    avatar_id: str, request: Request, user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    service = _enabled_service(request)
    await service.delete(user, _record_id(avatar_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{avatar_id}/reports",
    response_model=PhotoAvatarReportReceipt,
    status_code=status.HTTP_202_ACCEPTED,
)
async def report_photo_avatar(
    avatar_id: str,
    body: PhotoAvatarReportRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PhotoAvatarReportReceipt:
    service = _enabled_service(request)
    return await service.report(user, _record_id(avatar_id), body)
