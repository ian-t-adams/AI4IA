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

import asyncio
import logging
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..entitlements.service import EntitlementService
from ..images.artifacts import IMAGE_CONTENT_TYPE, BlobNotFoundError, ImageArtifactStore
from ..images.availability import (
    NO_IMAGE_EDIT_MODEL_DETAIL,
    default_image_edit_model_id,
    state_image_edit_availability,
)
from ..images.edit_capability import (
    build_edit_receipt,
    edit_request_text,
    record_provider_failure,
    store_edit_result,
)
from ..images.editing import ImageEditService
from ..images.service import (
    MAX_IMAGES,
    MAX_PROMPT_CHARS,
    ImageGenerationError,
    ImageGenerationService,
    image_provider_id,
)
from ..images.source import (
    MAX_EDIT_SOURCE_BYTES,
    EditRegion,
    ImageSourceError,
    build_region_mask,
    inspect_image,
    region_box,
)
from ..images.sources import EditSourceRef, load_edit_source
from ..logging_setup import get_correlation_id
from ..sessions.models import Message, MessageRole, MessageStatus
from ..usage.models import UsageTarget
from ..usage.pricing import load_pricing
from ..usage.service import UsageService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/images", tags=["images"])

# An artifact id is a uuid4 hex token (32 lowercase hex chars, no dashes).
# Constrain the path param to exactly that shape so it can never carry a
# separator or traversal.
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Library document ids are server-minted tokens; anything else is a 404.
_DOCUMENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


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
    provider: str
    deployment: str
    region: str
    dataZone: str | None
    residency: str
    size: str
    quality: str
    costKnown: bool
    estimatedCostUsd: float | None = None
    pricingBasis: str | None = None
    priceVersion: str | None = None
    images: list[GeneratedImage]


class ImagePriceOption(BaseModel):
    size: str
    quality: str
    costKnown: bool
    estimatedCostUsd: float | None = None
    pricingBasis: str | None = None


class ImageModelOption(BaseModel):
    id: str
    displayName: str
    provider: str
    sizes: list[str]
    qualities: list[str]
    dataZones: list[str]
    residencies: list[str]
    prices: list[ImagePriceOption]
    # The catalog declares images/edits for this model (``imageEditing``).
    editing: bool = False


class ImageOptionsResponse(BaseModel):
    enabled: bool
    maxSelectedModels: int = 3
    currency: str
    priceVersion: str | None = None
    models: list[ImageModelOption]
    # Server-authoritative editing availability (flags, store and a routable
    # editing model); the web may only hide UI from it, never enforce.
    editingEnabled: bool = False
    defaultEditModel: str | None = None


_Fraction = Annotated[float, Field(strict=True, ge=0, le=1)]
_PositiveFraction = Annotated[float, Field(strict=True, gt=0, le=1)]


class ImageEditSourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["generated", "library"]
    id: str = Field(min_length=1, max_length=128)


class ImageEditRegionBody(BaseModel):
    """A rectangle as fractions of the source: x/y from the top-left."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    x: _Fraction
    y: _Fraction
    width: _PositiveFraction
    height: _PositiveFraction


class ImageEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessionId: str = Field(min_length=1, max_length=128)
    source: ImageEditSourceBody
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    model: str | None = Field(default=None, max_length=128)
    size: str | None = Field(default=None, max_length=32)
    quality: str | None = Field(default=None, max_length=32)
    region: ImageEditRegionBody | None = None


class ImageEditResponse(BaseModel):
    messages: list[Message]


@router.get("/options", response_model=ImageOptionsResponse)
async def image_options(
    request: Request,
    _user: AuthenticatedUser = Depends(get_current_user),
) -> ImageOptionsResponse:
    catalog = request.app.state.catalog
    pricing = load_pricing()
    models: list[ImageModelOption] = []
    for entry in catalog.models:
        if (
            not request.app.state.settings.image_generation_enabled
            or entry.category != "image"
            or not catalog.available(entry)
        ):
            continue
        sizes = entry.imageSizes or ["1024x1024"]
        qualities = entry.imageQualities or ["auto"]
        prices: list[ImagePriceOption] = []
        for size in sizes:
            for quality in qualities:
                estimate = pricing.estimate_image(
                    entry.id, size=size, quality=quality
                )
                prices.append(
                    ImagePriceOption(
                        size=size,
                        quality=quality,
                        costKnown=estimate.known,
                        estimatedCostUsd=(
                            estimate.micro_usd / 1_000_000
                            if estimate.micro_usd is not None
                            else None
                        ),
                        pricingBasis=estimate.pricing_basis,
                    )
                )
        models.append(
            ImageModelOption(
                id=entry.id,
                displayName=entry.displayName,
                provider=image_provider_id(entry.format),
                sizes=sizes,
                qualities=qualities,
                dataZones=sorted(
                    {d.dataZone for d in entry.options if d.dataZone is not None}
                ),
                residencies=sorted({d.residency for d in entry.options}),
                prices=prices,
                editing=entry.imageEditing,
            )
        )
    editing_enabled = state_image_edit_availability(request.app.state) == "available"
    return ImageOptionsResponse(
        enabled=request.app.state.settings.image_generation_enabled,
        currency=pricing.currency,
        priceVersion=pricing.version,
        models=models,
        editingEnabled=editing_enabled,
        defaultEditModel=default_image_edit_model_id(catalog) if editing_enabled else None,
    )


@router.post("/generations", response_model=ImageResponse)
async def generate_images(
    body: ImageRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ImageResponse:
    if not request.app.state.settings.image_generation_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Image generation is disabled."
        )
    # The generation core is stateless; build it per request so tests that swap
    # app.state.gateway after startup are honored.
    service = ImageGenerationService(
        settings=request.app.state.settings,
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
        if exc.provider_completion is not None:
            completion = exc.provider_completion
            await metering.record_completion(
                user_id=user.internal_user_id,
                session_id="image-generation",
                model_id=completion.model_id,
                target=UsageTarget.from_deployment(
                    completion.deployment, provider=completion.provider
                ),
                usage=completion.usage,
                status="error",
                provider_completed=True,
                correlation_id=correlation_id,
                billable_units=completion.billable_units,
                billing_unit=completion.billing_unit,
                image_size=completion.image_size,
                image_quality=completion.image_quality,
            )
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
        target=UsageTarget.from_deployment(
            result.deployment, provider=result.provider
        ),
        usage=result.usage,
        status="complete",
        provider_completed=True,
        correlation_id=correlation_id,
        billable_units=len(result.images_b64),
        billing_unit="image",
        image_size=result.size,
        image_quality=result.quality,
    )

    estimate = load_pricing().estimate_image(
        result.model_id,
        size=result.size,
        quality=result.quality,
        count=len(result.images_b64),
    )
    return ImageResponse(
        model=result.model_id,
        provider=result.provider,
        deployment=result.deployment.deploymentName,
        region=result.deployment.region,
        dataZone=result.deployment.dataZone,
        residency=result.deployment.residency,
        size=result.size,
        quality=result.quality,
        costKnown=estimate.known,
        estimatedCostUsd=(
            estimate.micro_usd / 1_000_000
            if estimate.micro_usd is not None
            else None
        ),
        pricingBasis=estimate.pricing_basis,
        priceVersion=estimate.version,
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


def _require_editing(request: Request) -> None:
    """404 unless the shared predicate says editing is available right now."""
    availability = state_image_edit_availability(request.app.state)
    if availability != "available":
        detail = (
            NO_IMAGE_EDIT_MODEL_DETAIL if availability == "no_model"
            else "Image editing is disabled."
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _http_error(exc: ImageGenerationError | ImageSourceError) -> HTTPException:
    retry_after = getattr(exc, "retry_after", None)
    return HTTPException(
        status_code=exc.status_code,
        detail=exc.detail,
        headers={"Retry-After": str(retry_after)} if retry_after else None,
    )


@router.post("/edits", response_model=ImageEditResponse)
async def edit_image(
    body: ImageEditRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ImageEditResponse:
    """Edit an image the caller owns and add the result to their conversation.

    A direct user action, not a model turn. Ownership is enforced on both ends:
    the destination conversation must belong to the caller (404 otherwise), and
    the source must be an image in that conversation or an owned library image
    inside its library scope. Nothing is fetched from a URL. The optional region
    becomes a PNG mask built at the source's exact dimensions. One provider
    attempt is made; a lost response is never retried here.
    """
    _require_editing(request)
    state = request.app.state
    uid = user.internal_user_id
    service = ImageEditService(settings=state.settings, catalog=state.catalog, gateway=state.gateway)
    try:
        # Cheap validation first: nothing below reads a source for a refused model.
        entry = service.resolve_model(body.model)
        service.resolve_controls(entry, body.size, body.quality)
    except ImageGenerationError as exc:
        raise _http_error(exc) from exc
    repo = state.session_repo
    session = await repo.get_session(uid, body.sessionId)

    entitlements: EntitlementService = state.entitlements
    decision = await entitlements.check(uid)
    if not decision.allowed:
        headers = (
            {"Retry-After": str(decision.retry_after_seconds)}
            if decision.retry_after_seconds is not None
            else None
        )
        raise HTTPException(status_code=decision.code, detail=decision.reason, headers=headers)

    try:
        source = await load_edit_source(
            ref=EditSourceRef(body.source.kind, body.source.id),
            user_id=uid, session=session, repo=repo,
            image_artifacts=state.image_artifacts,
            retrieval=getattr(state, "document_retrieval", None),
        )
        mask = None
        region = body.region.model_dump() if body.region is not None else None
        if region is not None:
            if source.info.rotated:
                raise ImageSourceError(
                    422,
                    "Region edits are unavailable for this rotated photo. "
                    "Edit the whole image instead.",
                )
            box = region_box(EditRegion(**region), source.info.width, source.info.height)
            mask = await asyncio.to_thread(
                build_region_mask, source.info.width, source.info.height, box,
            )
    except ImageSourceError as exc:
        raise _http_error(exc) from exc

    metering: UsageService = state.usage
    correlation_id = get_correlation_id()
    try:
        result = await service.edit(
            prompt=body.prompt, model=entry.id, source=source, size=body.size,
            quality=body.quality, mask=mask, correlation_id=correlation_id,
        )
    except ImageGenerationError as exc:
        await record_provider_failure(
            exc, metering=metering, user_id=uid, session_id=session.id,
            correlation_id=correlation_id,
        )
        raise _http_error(exc) from exc
    try:
        attachment = await store_edit_result(
            result=result, source=source, prompt=body.prompt, masked=mask is not None,
            artifact_store=state.image_artifacts, metering=metering, user_id=uid,
            session_id=session.id, correlation_id=correlation_id,
        )
    except ImageGenerationError as exc:
        raise _http_error(exc) from exc

    user_message = Message(
        sessionId=session.id,
        userId=uid,
        role=MessageRole.user,
        content=edit_request_text(body.prompt, source, masked=mask is not None),
        status=MessageStatus.complete,
    )
    assistant = Message(
        sessionId=session.id,
        userId=uid,
        role=MessageRole.assistant,
        content=f"Edited the image with {result.display_name}.",
        status=MessageStatus.complete,
        model=result.model_id,
        attachments=[attachment],
        executionReceipt=build_edit_receipt(
            result=result, source=source, prompt=body.prompt, attachment=attachment,
            region=region, correlation_id=correlation_id,
        ),
    )
    # The same fenced repository writes every other transcript path uses.
    await repo.add_message(uid, user_message)
    await repo.add_message(uid, assistant)
    await repo.touch_session(uid, session.id)
    return ImageEditResponse(messages=[user_message, assistant])


@router.get("/sources/library/{document_id}")
async def get_library_image_source(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Preview an owned, editable library image for the edit dialog.

    Owner-only (shared and tenant-public documents never resolve), ready, image
    modality, PNG/JPEG by content, and bounded; unavailable while editing is off.
    """
    _require_editing(request)
    retrieval = getattr(request.app.state, "document_retrieval", None)
    if retrieval is None or not _DOCUMENT_ID_RE.match(document_id or ""):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    result = await retrieval.read_owned_image(
        user.internal_user_id, document_id, max_bytes=MAX_EDIT_SOURCE_BYTES,
    )
    if "error" in result:
        raise HTTPException(status_code=int(result.get("status", 404)), detail=result["error"])
    try:
        info = inspect_image(result["data"])
    except ImageSourceError as exc:
        raise _http_error(exc) from exc
    return Response(
        content=result["data"],
        media_type=info.content_type,
        headers={"Cache-Control": "private, no-store"},
    )
