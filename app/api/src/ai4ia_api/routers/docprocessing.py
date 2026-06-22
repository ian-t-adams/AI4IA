"""Authenticated serve endpoint for ``process_document`` results.

The ``process_document`` agent tool persists each over-cap result to per-user blob
storage and returns only a small reference; the browser fetches the text here.
Mirrors the image/video serve endpoints (:mod:`ai4ia_api.routers.images`,
:mod:`ai4ia_api.routers.videos`): the blob path is composed from the
*authenticated* user's id, so a user can only ever read their own results — an id
belonging to another user resolves to a path that does not exist for the caller
(404), never a cross-user read.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..docprocessing.artifacts import (
    ANALYSIS_CONTENT_TYPE,
    BlobNotFoundError,
    DocumentArtifactStore,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])

# An artifact id is a uuid4 hex token. Constrain the path param to that shape so
# it can never carry a separator or traversal.
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")


@router.get("/artifacts/{artifact_id}")
async def get_document_artifact(
    artifact_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Serve a tool-produced processing result's text to its owner."""
    if not _ARTIFACT_ID_RE.match(artifact_id or ""):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    store: DocumentArtifactStore = request.app.state.document_artifacts
    try:
        data = await store.get(user.internal_user_id, artifact_id)
    except BlobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.") from exc
    return Response(
        content=data,
        media_type=ANALYSIS_CONTENT_TYPE,
        headers={"Cache-Control": "private, max-age=86400"},
    )
