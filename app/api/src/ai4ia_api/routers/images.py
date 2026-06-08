"""Image generation endpoint (Phase 7A): custom imagery & backgrounds.

Generates images through the same model gateway used for chat (gpt-image-2 etc.).
Governance mirrors the chat path: the caller is authenticated, the entitlement
gate runs (a disabled user is blocked; an admin-set rate/budget limit applies),
and every successful generation is metered into the per-user usage ledger so the
rolling rate/token windows actually account for image requests — not just chat.

Hardening (per security review):
- Only ``image``-category catalog models are accepted (no chat/audio smuggling).
- ``n`` and ``size`` are hard-capped; provider params are an explicit allowlist
  (no arbitrary client passthrough).
- The total returned base64 payload is size-guarded.
- Upstream gateway errors are sanitized: user-actionable 400s (content policy /
  invalid request) surface a trimmed detail; everything else maps to a generic
  message. Prompts and base64 payloads are never logged.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..catalog import ModelCatalog
from ..entitlements.service import EntitlementService
from ..gateway.client import ModelGatewayClient, ModelGatewayError
from ..logging_setup import get_correlation_id
from ..usage.models import TokenUsage
from ..usage.service import UsageService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/images", tags=["images"])

# Hard caps (cost + payload protection). One image per request keeps payloads
# small and provider cost predictable for v1; sizes are a closed allowlist.
MAX_IMAGES = 1
ALLOWED_SIZES = {"1024x1024", "1024x1536", "1536x1024", "auto"}
DEFAULT_SIZE = "1024x1024"
MAX_PROMPT_CHARS = 4000
# Reject an upstream response whose combined base64 exceeds this (defense against
# a misbehaving/oversized provider response blowing up the browser + proxy).
MAX_TOTAL_B64_CHARS = 12_000_000  # ~9 MB decoded


class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    model: str | None = None
    size: str | None = None
    n: int = Field(default=1, ge=1, le=MAX_IMAGES)
    region: str | None = None
    dataZone: str | None = None


class GeneratedImage(BaseModel):
    b64: str


class ImageResponse(BaseModel):
    model: str
    deployment: str
    size: str
    images: list[GeneratedImage]


def _image_token_usage(raw: dict | None) -> TokenUsage:
    """Map an image-generation ``usage`` object to TokenUsage.

    Image models report ``input_tokens``/``output_tokens``/``total_tokens``
    (not prompt/completion), so this is a small dedicated mapping. A missing or
    malformed object yields an *unknown* single call (counted as one request but
    not summed as zero tokens), keeping rate enforcement correct.
    """
    if not isinstance(raw, dict):
        return TokenUsage(known=False, complete=False, calls=1)
    inp = raw.get("input_tokens")
    out = raw.get("output_tokens")
    tot = raw.get("total_tokens")
    if inp is None and out is None and tot is None:
        return TokenUsage(known=False, complete=False, calls=1)
    try:
        p = int(inp or 0)
        c = int(out or 0)
        t = int(tot if tot is not None else p + c)
    except (TypeError, ValueError, OverflowError):
        return TokenUsage(known=False, complete=False, calls=1)
    return TokenUsage(prompt=p, completion=c, total=t, known=True, complete=True, calls=1)


@router.post("/generations", response_model=ImageResponse)
async def generate_images(
    body: ImageRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ImageResponse:
    catalog: ModelCatalog = request.app.state.catalog
    gateway: ModelGatewayClient = request.app.state.gateway
    entitlements: EntitlementService = request.app.state.entitlements
    metering: UsageService = request.app.state.usage

    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="Prompt must not be empty.")

    size = body.size or DEFAULT_SIZE
    if size not in ALLOWED_SIZES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported size. Allowed: {', '.join(sorted(ALLOWED_SIZES))}.",
        )

    model_id = body.model
    if not model_id:
        # Default to the first image-category model in the catalog.
        first = next((m for m in catalog.models if m.category == "image"), None)
        if first is None:
            raise HTTPException(status_code=400, detail="No image models are available.")
        model_id = first.id

    entry = catalog.get(model_id)
    if entry is None:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model_id}")
    if entry.category != "image":
        raise HTTPException(
            status_code=400, detail=f"Model '{model_id}' is not an image model."
        )

    deployment = catalog.resolve_deployment(
        model_id, region=body.region, data_zone=body.dataZone
    )
    if deployment is None:
        raise HTTPException(
            status_code=400, detail=f"Unknown or unavailable model: {model_id}"
        )

    # Entitlement gate (Phase 6B): blocks disabled users and applies any admin-set
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
        result = await gateway.generate_image(
            deployment=deployment.deploymentName,
            prompt=prompt,
            size=None if size == "auto" else size,
            n=body.n,
            correlation_id=correlation_id,
        )
    except ModelGatewayError as exc:
        # Sanitize: surface user-actionable 400s (content policy / invalid
        # request) with a trimmed detail; map everything else to a generic
        # message. Never log the prompt or any base64 payload.
        logger.warning(
            "image generation upstream error (status=%s, model=%s, correlation_id=%s)",
            exc.status_code,
            model_id,
            correlation_id,
        )
        if exc.status_code == 400:
            raise HTTPException(
                status_code=400,
                detail=_trim(exc.detail) or "Image request was rejected.",
            ) from exc
        if exc.status_code in (401, 403):
            raise HTTPException(
                status_code=502, detail="Image provider rejected the request."
            ) from exc
        if exc.status_code == 429:
            raise HTTPException(
                status_code=429,
                detail="Image provider is rate limited. Try again shortly.",
                headers={"Retry-After": "30"},
            ) from exc
        raise HTTPException(
            status_code=502, detail="Image generation failed."
        ) from exc

    data = result.get("data") or []
    images = [GeneratedImage(b64=d["b64_json"]) for d in data if d.get("b64_json")]
    if not images:
        raise HTTPException(status_code=502, detail="Image generation returned no image.")

    total_b64 = sum(len(img.b64) for img in images)
    if total_b64 > MAX_TOTAL_B64_CHARS:
        raise HTTPException(
            status_code=502, detail="Generated image was unexpectedly large."
        )

    # Meter the request so the rolling rate/token windows include image usage.
    # Best-effort: record_completion never raises.
    await metering.record_completion(
        user_id=user.internal_user_id,
        session_id="image-generation",
        model_id=model_id,
        deployment=deployment,
        usage=_image_token_usage(result.get("usage")),
        status="complete",
        correlation_id=correlation_id,
    )

    return ImageResponse(
        model=model_id,
        deployment=deployment.deploymentName,
        size=size,
        images=images,
    )


def _trim(detail: str | None, limit: int = 300) -> str:
    if not detail:
        return ""
    text = detail.strip()
    return text if len(text) <= limit else text[:limit] + "…"
