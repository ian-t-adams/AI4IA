"""Image generation endpoint: custom imagery & backgrounds, plus the
authenticated serve endpoint for tool-generated images.

Generation governance lives in :class:`~ai4ia_api.images.service.ImageGenerationService`
(shared with the ``generate_image`` agent tool): only ``image``-category catalog
models are accepted, ``n``/``size`` are hard-capped, the returned base64 payload
is size-guarded, and upstream gateway errors are sanitized (user-actionable 400s
surface a trimmed detail; everything else maps to a generic message; prompts and
base64 payloads are never logged). This router adds the request-scoped concerns:
the entitlement gate, usage metering, and (for the serve endpoint) per-user
artifact ownership.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..entitlements.service import EntitlementService
from ..images.artifacts import IMAGE_CONTENT_TYPE, BlobNotFoundError, ImageArtifactStore
from ..images.service import (
    MAX_IMAGES,
    MAX_PROMPT_CHARS,
    ImageGenerationError,
    ImageGenerationService,
)
from ..logging_setup import get_correlation_id
from ..usage.service import UsageService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/images", tags=["images"])

# An artifact id is a uuid4 hex token (32 lowercase hex chars). Constrain the
# path param to that shape so it can never carry a separator or traversal.
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")


class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    model: str | None = None
    size: str | None = None
    quality: str | None = None
    n: int = Field(default=1, ge=1, le=MAX_IMAGES)
    region: str | None = None
    dataZone: str | None = None


class GeneratedImage(BaseModel):
    b64: str


class ImageResponse(BaseModel):
    model: str
    deployment: str
    size: str
    quality: str
    images: list[GeneratedImage]


@router.post("/generations", response_model=ImageResponse)
async def generate_images(
    body: ImageRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ImageResponse:
    # The generation core is stateless; build it per request so tests that swap
    # app.state.gateway after startup are honored.
    service = ImageGenerationService(
        catalog=request.app.state.catalog, gateway=request.app.state.gateway
    )
    entitlements: EntitlementService = request.app.state.entitlements
    metering: UsageService = request.app.state.usage

    # Entitlement gate: blocks disabled users and applies any admin-set
    # rate/budget limit. Ships unlimited; short-circuits with no ledger IO.
    decision = await entitlements.check(user.internal_user_id)
    if not decision.allowed:
        headers = (
            {"Retry-After": str(decision.retry_after_seconds)}
            if decision.retry_after_seconds is not None
            else None
        )
        raise HTTPException(
            status_code=decision.code, detail=decision.reason, headers=headers
        )

    correlation_id = get_correlation_id()
    try:
        result = await service.generate(
            prompt=body.prompt,
            model=body.model,
            size=body.size,
            quality=body.quality,
            n=body.n,
            region=body.region,
            data_zone=body.dataZone,
            correlation_id=correlation_id,
        )
    except ImageGenerationError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers=headers
        ) from exc

    # Meter the request so the rolling rate/token windows include image usage.
    # Best-effort: record_completion never raises.
    await metering.record_completion(
        user_id=user.internal_user_id,
        session_id="image-generation",
        model_id=result.model_id,
        deployment=result.deployment,
        usage=result.usage,
        status="complete",
        correlation_id=correlation_id,
    )

    return ImageResponse(
        model=result.model_id,
        deployment=result.deployment.deploymentName,
        size=result.size,
        quality=result.quality,
        images=[GeneratedImage(b64=b) for b in result.images_b64],
    )


@router.get("/artifacts/{artifact_id}")
async def get_image_artifact(
    artifact_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Serve a tool-generated image's bytes to its owner.

    The blob path is composed from the *authenticated* user's id, so a user can
    only ever read their own artifacts — an id belonging to another user resolves
    to a path that does not exist for the caller (404), never a cross-user read.
    """
    if not _ARTIFACT_ID_RE.match(artifact_id or ""):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    store: ImageArtifactStore = request.app.state.image_artifacts
    try:
        data = await store.get(user.internal_user_id, artifact_id)
    except BlobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.") from exc
    return Response(
        content=data,
        media_type=IMAGE_CONTENT_TYPE,
        headers={"Cache-Control": "private, max-age=86400"},
    )
