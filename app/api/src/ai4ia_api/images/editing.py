"""Shared image-edit core for the ``edit_image`` tool and the HTTP endpoint.

Mirrors :class:`~ai4ia_api.images.service.ImageGenerationService`: model/size/
quality validation, deployment resolution, the governed gateway call and upstream
error sanitization live here once, so the two entry points can never drift.
Entitlements, metering, persistence and receipts stay at the call sites, which own
the request-scoped services.

Only catalog rows declaring ``imageEditing`` are accepted. An omitted model uses
the catalog's preferred editing model (Sunburst) when it is routable, otherwise
the first routable editing model; an explicitly requested model is validated as
requested and never substituted. The source has already been resolved and
inspected (:mod:`ai4ia_api.images.sources`); an optional mask is re-checked
against the provider contract immediately before dispatch.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from ..catalog import DeploymentOption, ModelCatalog, ModelEntry
from ..config import Settings
from ..gateway.client import ModelGatewayClient, ModelGatewayError
from ..usage.models import ProviderCompletion, TokenUsage
from .availability import (
    ImageEditAvailability,
    default_image_edit_model_id,
    image_editing_availability,
)
from .service import (
    ALLOWED_QUALITIES,
    ALLOWED_SIZES,
    MAX_PROMPT_CHARS,
    MAX_TOTAL_B64_CHARS,
    ImageGenerationError,
    image_provider_id,
    image_token_usage,
)
from .source import ImageSourceError, validate_mask
from .sources import EditSource

logger = logging.getLogger(__name__)

DEFAULT_EDIT_SIZE = "auto"
DEFAULT_EDIT_QUALITY = "auto"
_SIZE_ORDER = ("auto", "1024x1024", "1024x1536", "1536x1024")
_QUALITY_ORDER = ("auto", "low", "medium", "high")
CONTENT_FILTER_DETAIL = (
    "The edit was blocked by the content safety system. Try a different image or prompt."
)
# Documented Azure codes (``contentFilter``, ``content_policy_violation``, inner
# ``ResponsibleAIPolicyViolation``) plus the OpenAI-surface ``moderation_blocked``.
_CONTENT_FILTER_CODES = frozenset({
    "contentfilter", "content_filter", "content_policy_violation", "moderation_blocked",
    "responsibleaipolicyviolation",
})


@dataclass(frozen=True)
class ImageEditResult:
    model_id: str
    display_name: str
    provider: str
    deployment: DeploymentOption
    size: str
    quality: str
    image_b64: str
    usage: TokenUsage


def _ordered(values: Iterable[str], order: tuple[str, ...]) -> list[str]:
    rank = {value: index for index, value in enumerate(order)}
    return sorted(set(values), key=lambda value: (rank.get(value, len(order)), value))


def edit_sizes(entry: ModelEntry) -> list[str]:
    """Every output size an edit on ``entry`` accepts, ``auto`` first.

    The single source for both validation and the options endpoint. Unlike the
    generation picker, an unset catalog list is never narrowed to one square size.
    """
    return _ordered(entry.imageSizes or ALLOWED_SIZES, _SIZE_ORDER)


def edit_qualities(entry: ModelEntry) -> list[str]:
    """Every quality an edit on ``entry`` accepts, ``auto`` first."""
    return _ordered(entry.imageQualities or ALLOWED_QUALITIES, _QUALITY_ORDER)


def _default_control(values: list[str], preferred: str) -> str:
    return preferred if preferred in values else values[0]


def _provider_error(detail: str | None) -> tuple[set[str], str | None]:
    """Error codes and message from a provider/APIM JSON error body, if any."""
    try:
        body = json.loads(detail or "")
    except ValueError:
        return set(), None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return set(), None
    codes = {str(error.get("code") or "").lower()}
    for key in ("innererror", "inner_error"):
        inner = error.get(key)
        if isinstance(inner, dict):
            codes.add(str(inner.get("code") or "").lower())
    message = error.get("message")
    return codes - {""}, message if isinstance(message, str) else None


def _trim(text: str | None, limit: int = 300) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


def sanitize_edit_error(exc: ModelGatewayError) -> ImageGenerationError:
    """Map an upstream failure to a safe, user-actionable error."""
    codes, message = _provider_error(exc.detail)
    if codes & _CONTENT_FILTER_CODES:
        return ImageGenerationError(400, CONTENT_FILTER_DETAIL)
    if exc.status_code == 400:
        return ImageGenerationError(400, _trim(message) or "Image edit request was rejected.")
    if exc.status_code in (401, 403):
        return ImageGenerationError(502, "Image provider rejected the request.")
    if exc.status_code == 429:
        return ImageGenerationError(
            429, "Image provider is rate limited. Try again shortly.", retry_after=30,
        )
    return ImageGenerationError(502, "Image edit failed.")


class ImageEditService:
    """Governed image editing shared by the HTTP endpoint and the tool."""

    def __init__(
        self, *, settings: Settings, catalog: ModelCatalog, gateway: ModelGatewayClient,
    ) -> None:
        self._settings = settings
        self._catalog = catalog
        self._gateway = gateway

    @property
    def enabled(self) -> bool:
        return bool(
            self._settings.image_editing_enabled and self._settings.image_generation_enabled
        )

    def availability(
        self, artifact_store: object | None, *, policy_filter: bool = True,
    ) -> ImageEditAvailability:
        """The shared predicate over this service's settings and catalog."""
        return image_editing_availability(
            editing_enabled=self._settings.image_editing_enabled,
            generation_enabled=self._settings.image_generation_enabled,
            artifact_store=artifact_store,
            catalog=self._catalog,
            policy_filter=policy_filter,
        )

    def resolve_model(self, model: str | None) -> ModelEntry:
        """The requested editing model, or the default; refuse anything else."""
        model_id = (model or "").strip() or default_image_edit_model_id(self._catalog)
        if not model_id:
            raise ImageGenerationError(400, "No image editing models are available.")
        entry = self._catalog.get(model_id)
        if entry is None:
            raise ImageGenerationError(400, f"Unknown model: {model_id}")
        if entry.category != "image" or not entry.imageEditing:
            raise ImageGenerationError(400, f"Model '{model_id}' does not support image editing.")
        if not self._catalog.available(entry):
            raise ImageGenerationError(400, f"Unknown or unavailable model: {model_id}")
        return entry

    def resolve_controls(
        self, entry: ModelEntry, size: str | None, quality: str | None,
    ) -> tuple[str, str]:
        sizes = edit_sizes(entry)
        resolved_size = size or _default_control(sizes, DEFAULT_EDIT_SIZE)
        if resolved_size not in sizes:
            raise ImageGenerationError(
                422, f"Unsupported size for {entry.id}. Allowed: {', '.join(sorted(sizes))}.",
            )
        qualities = edit_qualities(entry)
        resolved_quality = quality or _default_control(qualities, DEFAULT_EDIT_QUALITY)
        if resolved_quality not in qualities:
            raise ImageGenerationError(
                422,
                f"Unsupported quality for {entry.id}. Allowed: {', '.join(sorted(qualities))}.",
            )
        return resolved_size, resolved_quality

    async def edit(
        self,
        *,
        prompt: str,
        model: str | None,
        source: EditSource,
        size: str | None = None,
        quality: str | None = None,
        mask: bytes | None = None,
        correlation_id: str | None = None,
    ) -> ImageEditResult:
        if not self.enabled:
            raise ImageGenerationError(404, "Image editing is disabled.")
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise ImageGenerationError(422, "Prompt must not be empty.")
        if len(clean_prompt) > MAX_PROMPT_CHARS:
            raise ImageGenerationError(
                422, f"Prompt must be at most {MAX_PROMPT_CHARS} characters."
            )
        entry = self.resolve_model(model)
        resolved_size, resolved_quality = self.resolve_controls(entry, size, quality)
        deployment = self._catalog.resolve_deployment(entry.id)
        if deployment is None:
            raise ImageGenerationError(400, f"Unknown or unavailable model: {entry.id}")
        if mask is not None:
            try:
                validate_mask(mask, source.info.width, source.info.height)
            except ImageSourceError as exc:
                raise ImageGenerationError(exc.status_code, exc.detail) from exc

        provider = image_provider_id(entry.format)
        completion = ProviderCompletion(
            model_id=entry.id,
            deployment=deployment,
            usage=TokenUsage(known=False, complete=False, calls=1),
            provider=provider,
            image_size=resolved_size,
            image_quality=resolved_quality,
            billable_units=1,
            billing_unit="image",
        )
        try:
            result = await self._gateway.edit_image(
                deployment=deployment.deploymentName,
                prompt=clean_prompt,
                image=source.data,
                image_content_type=source.info.content_type,
                mask=mask,
                size=None if resolved_size == "auto" else resolved_size,
                quality=None if resolved_quality == "auto" else resolved_quality,
                n=1,
                api=entry.api,
                correlation_id=correlation_id,
            )
        except ModelGatewayError as exc:
            # Never log the prompt or any image payload.
            logger.warning(
                "image edit upstream error (status=%s, model=%s, correlation_id=%s)",
                exc.status_code, entry.id, correlation_id,
            )
            raise sanitize_edit_error(exc) from exc
        except ValueError as exc:
            # The request was sent and answered with an unreadable body.
            raise ImageGenerationError(
                502, "Image edit returned an unreadable response.",
                provider_completion=completion,
            ) from exc

        completion = ProviderCompletion(
            model_id=entry.id,
            deployment=deployment,
            usage=image_token_usage(result.get("usage") if isinstance(result, dict) else None),
            provider=provider,
            image_size=resolved_size,
            image_quality=resolved_quality,
            billable_units=1,
            billing_unit="image",
        )
        data = result.get("data") if isinstance(result, dict) else None
        images = [
            item["b64_json"] for item in (data if isinstance(data, list) else [])
            if isinstance(item, dict) and isinstance(item.get("b64_json"), str) and item["b64_json"]
        ]
        if not images:
            raise ImageGenerationError(
                502, "Image edit returned no image.", provider_completion=completion,
            )
        if len(images[0]) > MAX_TOTAL_B64_CHARS:
            raise ImageGenerationError(
                502, "Edited image was unexpectedly large.", provider_completion=completion,
            )
        return ImageEditResult(
            model_id=entry.id,
            display_name=entry.displayName,
            provider=provider,
            deployment=deployment,
            size=resolved_size,
            quality=resolved_quality,
            image_b64=images[0],
            usage=completion.usage,
        )
