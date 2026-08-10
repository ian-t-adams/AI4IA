"""Shared video-generation core.

The agent-callable ``generate_video`` tool (:mod:`ai4ia_api.videos.capability`)
goes through :class:`VideoGenerationService` so the model/size/duration
validation, the async job orchestration, and the upstream-error sanitization live
in exactly one place — mirroring :class:`ai4ia_api.images.service.ImageGenerationService`.

Unlike images (a single round-trip), Sora is an **async job**: the service
submits a job, polls it to completion (bounded by a hard wall-clock budget so a
turn can never hang), then downloads the MP4 bytes. It is transport-agnostic: it
raises :class:`VideoGenerationError` (carrying a sanitized ``status_code`` +
``detail``) instead of an ``HTTPException`` so a caller can map it to either a
structured tool result or an HTTP response.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..catalog import DeploymentOption, ModelCatalog
from ..gateway.client import ModelGatewayClient, ModelGatewayError
from ..usage.models import ProviderCompletion, TokenUsage

logger = logging.getLogger(__name__)

# Closed allowlists (cost + payload protection). Sizes are "WIDTHxHEIGHT" and map
# directly to the Sora job's width/height. Duration is a bounded integer.
ALLOWED_SIZES = {"1280x720", "720x1280", "1024x1024", "1920x1080", "1080x1920"}
DEFAULT_SIZE = "1280x720"
MIN_SECONDS = 1
MAX_SECONDS = 20
DEFAULT_SECONDS = 5
MAX_PROMPT_CHARS = 4000
# Reject an upstream MP4 larger than this (defense against an oversized provider
# response exhausting memory / the serve path). A few-second clip is a few MB.
MAX_VIDEO_BYTES = 200_000_000  # ~200 MB

# Terminal job statuses.
_SUCCEEDED = "succeeded"
_FAILURE_STATUSES = {"failed", "cancelled"}


class VideoGenerationError(Exception):
    """A sanitized, transport-agnostic video-generation failure.

    ``status_code`` mirrors the HTTP status a router should surface; ``detail``
    is already sanitized (never contains a raw upstream payload). The optional
    ``retry_after`` seconds is set for rate-limit (429) cases.
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


class VideoProviderCompletedCancellation(asyncio.CancelledError):
    """Cancellation after the provider reached a successful terminal state."""

    def __init__(self, provider_completion: ProviderCompletion) -> None:
        super().__init__("video download cancelled after provider completion")
        self.provider_completion = provider_completion


@dataclass(frozen=True)
class VideoGenerationResult:
    """A successful generation: the resolved model + the MP4 bytes."""

    model_id: str
    deployment: DeploymentOption
    size: str
    seconds: int
    video_bytes: bytes
    usage: TokenUsage


def _trim(detail: str | None, limit: int = 300) -> str:
    if not detail:
        return ""
    text = detail.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _parse_size(size: str) -> tuple[int, int]:
    w, h = size.split("x", 1)
    return int(w), int(h)


class VideoGenerationService:
    """Governed Sora video generation shared by the tool (and any future HTTP
    endpoint).

    Validates the model (video-category only), the size, and the duration
    against closed allowlists, resolves a deployment, then submits + polls +
    downloads through the model gateway, sanitizing upstream errors. Does NOT
    enforce entitlements or meter usage — those stay at the call site.

    ``sleep`` is injectable so tests can drive the poll loop without real delay.
    """

    def __init__(
        self,
        *,
        catalog: ModelCatalog,
        gateway: ModelGatewayClient,
        poll_interval_seconds: float = 5.0,
        max_wait_seconds: float = 240.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._catalog = catalog
        self._gateway = gateway
        self._poll_interval = poll_interval_seconds
        self._max_wait = max_wait_seconds
        self._sleep = sleep

    async def generate(
        self,
        *,
        prompt: str,
        model: str | None,
        size: str | None,
        seconds: int | None = None,
        region: str | None = None,
        data_zone: str | None = None,
        correlation_id: str | None = None,
    ) -> VideoGenerationResult:
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise VideoGenerationError(422, "Prompt must not be empty.")
        if len(clean_prompt) > MAX_PROMPT_CHARS:
            raise VideoGenerationError(
                422, f"Prompt must be at most {MAX_PROMPT_CHARS} characters."
            )

        resolved_size = size or DEFAULT_SIZE
        if resolved_size not in ALLOWED_SIZES:
            raise VideoGenerationError(
                422,
                f"Unsupported size. Allowed: {', '.join(sorted(ALLOWED_SIZES))}.",
            )
        width, height = _parse_size(resolved_size)

        resolved_seconds = DEFAULT_SECONDS if seconds is None else int(seconds)
        if resolved_seconds < MIN_SECONDS or resolved_seconds > MAX_SECONDS:
            raise VideoGenerationError(
                422,
                f"seconds must be between {MIN_SECONDS} and {MAX_SECONDS}.",
            )

        model_id = model
        if not model_id:
            first = next((m for m in self._catalog.models if m.category == "video"), None)
            if first is None:
                raise VideoGenerationError(400, "No video models are available.")
            model_id = first.id

        entry = self._catalog.get(model_id)
        if entry is None:
            raise VideoGenerationError(400, f"Unknown model: {model_id}")
        if entry.category != "video":
            raise VideoGenerationError(400, f"Model '{model_id}' is not a video model.")

        deployment = self._catalog.resolve_deployment(
            model_id, region=region, data_zone=data_zone
        )
        if deployment is None:
            raise VideoGenerationError(400, f"Unknown or unavailable model: {model_id}")

        try:
            job = await self._gateway.create_video_job(
                deployment=deployment.deploymentName,
                prompt=clean_prompt,
                width=width,
                height=height,
                n_seconds=resolved_seconds,
                correlation_id=correlation_id,
            )
            job_id = job.get("id")
            if not job_id:
                raise VideoGenerationError(502, "Video provider did not return a job.")

            status = await self._poll(job_id, job, correlation_id)
        except ModelGatewayError as exc:
            raise self._sanitize(exc, model_id, correlation_id) from exc

        completion = ProviderCompletion(
            model_id=model_id,
            deployment=deployment,
            usage=TokenUsage(known=False, complete=False, calls=1),
        )
        try:
            generation_id = self._extract_generation_id(status)
            video_bytes = await self._gateway.get_video_content(
                generation_id=generation_id, correlation_id=correlation_id
            )
        except asyncio.CancelledError as exc:
            raise VideoProviderCompletedCancellation(completion) from exc
        except ModelGatewayError as exc:
            sanitized = self._sanitize(exc, model_id, correlation_id)
            raise VideoGenerationError(
                sanitized.status_code,
                sanitized.detail,
                retry_after=sanitized.retry_after,
                provider_completion=completion,
            ) from exc
        except VideoGenerationError as exc:
            raise VideoGenerationError(
                exc.status_code,
                exc.detail,
                retry_after=exc.retry_after,
                provider_completion=completion,
            ) from exc

        if not video_bytes:
            raise VideoGenerationError(
                502,
                "Video generation returned no content.",
                provider_completion=completion,
            )
        if len(video_bytes) > MAX_VIDEO_BYTES:
            raise VideoGenerationError(
                502,
                "Generated video was unexpectedly large.",
                provider_completion=completion,
            )

        return VideoGenerationResult(
            model_id=model_id,
            deployment=deployment,
            size=resolved_size,
            seconds=resolved_seconds,
            video_bytes=video_bytes,
            # Sora jobs do not report token usage; count one request so rolling
            # rate windows include the call without inventing token counts.
            usage=completion.usage,
        )

    async def _poll(
        self, job_id: str, initial: dict, correlation_id: str | None
    ) -> dict:
        """Poll a submitted job to a terminal status within the wall-clock budget."""
        status_obj = initial
        waited = 0.0
        step = self._poll_interval if self._poll_interval > 0 else 0.001
        while True:
            status = (status_obj.get("status") or "").lower()
            if status == _SUCCEEDED:
                return status_obj
            if status in _FAILURE_STATUSES:
                raise VideoGenerationError(
                    502, f"Video generation {status}."
                )
            if waited >= self._max_wait:
                raise VideoGenerationError(
                    504, "Video generation timed out. Try a shorter clip."
                )
            await self._sleep(self._poll_interval)
            waited += step
            status_obj = await self._gateway.get_video_job(
                job_id=job_id, correlation_id=correlation_id
            )

    def _extract_generation_id(self, status_obj: dict) -> str:
        generations = status_obj.get("generations") or []
        if generations and isinstance(generations[0], dict):
            gen_id = generations[0].get("id")
            if gen_id:
                return str(gen_id)
        raise VideoGenerationError(502, "Video generation returned no content.")

    def _sanitize(
        self, exc: ModelGatewayError, model_id: str, correlation_id: str | None
    ) -> VideoGenerationError:
        # Surface user-actionable 400s (content policy / invalid request) with a
        # trimmed detail; map everything else to a generic message. Never log the
        # prompt or any payload.
        logger.warning(
            "video generation upstream error (status=%s, model=%s, correlation_id=%s)",
            exc.status_code,
            model_id,
            correlation_id,
        )
        if exc.status_code == 400:
            return VideoGenerationError(
                400, _trim(exc.detail) or "Video request was rejected."
            )
        if exc.status_code in (401, 403):
            return VideoGenerationError(502, "Video provider rejected the request.")
        if exc.status_code == 429:
            return VideoGenerationError(
                429,
                "Video provider is rate limited. Try again shortly.",
                retry_after=30,
            )
        return VideoGenerationError(502, "Video generation failed.")
