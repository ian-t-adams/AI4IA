"""Shared image-generation core.

Both the HTTP endpoint (:mod:`ai4ia_api.routers.images`) and the agent-callable
``generate_image`` tool (:mod:`ai4ia_api.images.capability`) go through
:class:`ImageGenerationService` so the model/size validation, the gateway call,
and the upstream-error sanitization live in exactly one place and can never
drift. Entitlement enforcement and usage metering deliberately stay at the two
call sites (the request-scoped ``EntitlementService`` / ``UsageService`` differ
in shape between a one-shot HTTP request and a tool turn), but this module owns
the single mapping from a raw gateway ``usage`` object to :class:`TokenUsage`
(:func:`image_token_usage`) so both meter identically.

The service is transport-agnostic: it raises :class:`ImageGenerationError`
(carrying a sanitized ``status_code`` + ``detail``) instead of an
``HTTPException`` so the HTTP router can map it to a response and the tool can
map it to a structured tool result.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..catalog import DeploymentOption, ModelCatalog
from ..gateway.client import ModelGatewayClient, ModelGatewayError
from ..usage.models import ProviderCompletion, TokenUsage

logger = logging.getLogger(__name__)

# Hard caps (cost + payload protection). One image per request keeps payloads
# small and provider cost predictable; sizes are a closed allowlist.
MAX_IMAGES = 1
ALLOWED_SIZES = {"1024x1024", "1024x1536", "1536x1024", "auto"}
DEFAULT_SIZE = "1024x1024"
# Image quality is a closed allowlist mirroring the provider's accepted values.
# "auto" lets the provider pick (and is omitted from the request body); the rest
# are forwarded as an explicit trusted parameter.
ALLOWED_QUALITIES = {"auto", "low", "medium", "high"}
DEFAULT_QUALITY = "auto"
MAX_PROMPT_CHARS = 4000
# Reject an upstream response whose combined base64 exceeds this (defense against
# a misbehaving/oversized provider response blowing up the browser + proxy).
MAX_TOTAL_B64_CHARS = 12_000_000  # ~9 MB decoded


class ImageGenerationError(Exception):
    """A sanitized, transport-agnostic image-generation failure.

    ``status_code`` mirrors the HTTP status the router should surface; ``detail``
    is already sanitized (never contains the prompt or a base64 payload). The
    optional ``retry_after`` seconds is set for rate-limit (429) cases.
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        retry_after: int | None = None,
        provider_completion: ProviderCompletion | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after
        self.provider_completion = provider_completion


@dataclass(frozen=True)
class ImageGenerationResult:
    """A successful generation: the resolved model + the base64 image(s)."""

    model_id: str
    deployment: DeploymentOption
    size: str
    quality: str
    images_b64: list[str]
    usage: TokenUsage


def image_token_usage(raw: object) -> TokenUsage:
    """Map an image-generation ``usage`` object to :class:`TokenUsage`.

    Image models report ``input_tokens``/``output_tokens``/``total_tokens`` (not
    prompt/completion), so this is a small dedicated mapping. A missing or
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


def _trim(detail: str | None, limit: int = 300) -> str:
    if not detail:
        return ""
    text = detail.strip()
    return text if len(text) <= limit else text[:limit] + "…"


class ImageGenerationService:
    """Governed image generation shared by the HTTP endpoint and the tool.

    Validates the model (image-category only) and size against the closed
    allowlists, resolves a deployment, calls the model gateway, and sanitizes
    upstream errors. Does NOT enforce entitlements or meter usage — those stay at
    the call site, which owns the request-scoped services.
    """

    def __init__(self, *, catalog: ModelCatalog, gateway: ModelGatewayClient) -> None:
        self._catalog = catalog
        self._gateway = gateway

    async def generate(
        self,
        *,
        prompt: str,
        model: str | None,
        size: str | None,
        quality: str | None = None,
        n: int = 1,
        region: str | None = None,
        data_zone: str | None = None,
        correlation_id: str | None = None,
    ) -> ImageGenerationResult:
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise ImageGenerationError(422, "Prompt must not be empty.")
        if len(clean_prompt) > MAX_PROMPT_CHARS:
            raise ImageGenerationError(
                422, f"Prompt must be at most {MAX_PROMPT_CHARS} characters."
            )

        count = 1 if n is None else int(n)
        if count < 1 or count > MAX_IMAGES:
            raise ImageGenerationError(422, f"n must be between 1 and {MAX_IMAGES}.")

        model_id = model
        if not model_id:
            first = next((m for m in self._catalog.models if m.category == "image"), None)
            if first is None:
                raise ImageGenerationError(400, "No image models are available.")
            model_id = first.id

        entry = self._catalog.get(model_id)
        if entry is None:
            raise ImageGenerationError(400, f"Unknown model: {model_id}")
        if entry.category != "image":
            raise ImageGenerationError(400, f"Model '{model_id}' is not an image model.")

        allowed_sizes = set(entry.imageSizes or ALLOWED_SIZES)
        resolved_size = size or DEFAULT_SIZE
        if resolved_size not in allowed_sizes:
            raise ImageGenerationError(
                422,
                f"Unsupported size for {model_id}. Allowed: "
                f"{', '.join(sorted(allowed_sizes))}.",
            )

        allowed_qualities = set(entry.imageQualities or ALLOWED_QUALITIES)
        resolved_quality = quality or DEFAULT_QUALITY
        if resolved_quality not in allowed_qualities:
            raise ImageGenerationError(
                422,
                f"Unsupported quality for {model_id}. Allowed: "
                f"{', '.join(sorted(allowed_qualities))}.",
            )

        deployment = self._catalog.resolve_deployment(
            model_id, region=region, data_zone=data_zone
        )
        if deployment is None:
            raise ImageGenerationError(
                400, f"Unknown or unavailable model: {model_id}"
            )

        try:
            result = await self._gateway.generate_image(
                deployment=deployment.deploymentName,
                prompt=clean_prompt,
                size=None if resolved_size == "auto" else resolved_size,
                n=count,
                extra=None if resolved_quality == "auto" else {"quality": resolved_quality},
                api=entry.api,
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
                raise ImageGenerationError(
                    400, _trim(exc.detail) or "Image request was rejected."
                ) from exc
            if exc.status_code in (401, 403):
                raise ImageGenerationError(
                    502, "Image provider rejected the request."
                ) from exc
            if exc.status_code == 429:
                raise ImageGenerationError(
                    429,
                    "Image provider is rate limited. Try again shortly.",
                    retry_after=30,
                ) from exc
            raise ImageGenerationError(502, "Image generation failed.") from exc

        data = result.get("data") or []
        completion = ProviderCompletion(
            model_id=model_id,
            deployment=deployment,
            usage=image_token_usage(result.get("usage")),
        )
        images = [
            d["b64_json"]
            for d in data
            if isinstance(d, dict) and d.get("b64_json")
        ]
        if not images:
            raise ImageGenerationError(
                502,
                "Image generation returned no image.",
                provider_completion=completion,
            )

        total_b64 = sum(len(b) for b in images)
        if total_b64 > MAX_TOTAL_B64_CHARS:
            raise ImageGenerationError(
                502,
                "Generated image was unexpectedly large.",
                provider_completion=completion,
            )

        return ImageGenerationResult(
            model_id=model_id,
            deployment=deployment,
            size=resolved_size,
            quality=resolved_quality,
            images_b64=images,
            usage=completion.usage,
        )
