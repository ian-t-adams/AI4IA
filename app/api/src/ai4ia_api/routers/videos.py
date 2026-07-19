"""Authenticated serve endpoint for tool-generated videos.

The ``generate_video`` agent tool persists each MP4 to per-user blob storage and
returns only a small reference; the browser fetches the bytes here. Mirrors the
image serve endpoint (:mod:`ai4ia_api.routers.images`): the blob path is composed
from the *authenticated* user's id, so a user can only ever read their own
artifacts — an id belonging to another user resolves to a path that does not
exist for the caller (404), never a cross-user read.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..videos.artifacts import VIDEO_CONTENT_TYPE, BlobNotFoundError, VideoArtifactStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/videos", tags=["videos"])

# An artifact id is a uuid4 hex token (32 lowercase hex chars, no dashes).
# Constrain the path param to exactly that shape so it can never carry a
# separator or traversal.
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@router.get("/artifacts/{artifact_id}")
async def get_video_artifact(
    artifact_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Serve a tool-generated video's bytes to its owner."""
    if not _ARTIFACT_ID_RE.match(artifact_id or ""):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    store: VideoArtifactStore = request.app.state.video_artifacts
    try:
        data = await store.get(user.internal_user_id, artifact_id)
    except BlobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.") from exc
    return Response(
        content=data,
        media_type=VIDEO_CONTENT_TYPE,
        headers={"Cache-Control": "private, max-age=86400"},
    )
