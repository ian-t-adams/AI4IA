"""The ``generate_video`` synthetic capability.

Mirrors the ``generate_image`` capability (:mod:`ai4ia_api.images.capability`): a
function schema + an async handler injected into
:func:`~ai4ia_api.agents.runtime.run_agent_turn` as ``extra_tools`` /
``extra_handlers``. This makes video generation a tool **any agent can attach** —
decoupled from any single agent — so users compose it into their own personas
(the same pattern as image generation and, later, document processing).

Why synthetic (closure-bound) rather than a registry tool: generation needs real
services (the model gateway, the catalog, the entitlement gate, the usage meter,
and durable blob storage), but a registry tool handler only receives
``(args, ToolContext)``. So the capability is built per turn with its services +
the authenticated ``user_id`` captured in a closure — a tool argument can only
ever carry the prompt/model/size/seconds, never spoof the user.

The generated MP4 is far larger than the runtime's 8 KB tool-result cap, so the
handler **persists** the video to per-user blob storage and returns only a small
reference (the ``artifact_id``). The reference is also appended to a per-turn
``sink`` that the chat router drains onto the assistant message as a
:class:`~ai4ia_api.sessions.models.MessageAttachment`, which the browser renders
by fetching the bytes from the authenticated serve endpoint.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from ..agents.tool_exec import ToolContext
from ..catalog import ModelCatalog
from ..entitlements.service import EntitlementService
from ..sessions.models import MessageAttachment
from ..usage.models import UsageStatus
from ..usage.service import UsageService
from .artifacts import VideoArtifactStore
from .service import (
    VideoGenerationError,
    VideoGenerationService,
    VideoProviderCompletedCancellation,
)

logger = logging.getLogger(__name__)

GENERATE_VIDEO_TOOL_NAME = "generate_video"

# Per-turn budget (on top of the runtime's global tool-call budget). Video
# generation is a slow, metered, paid call, so a turn may produce at most one.
MAX_VIDEOS_PER_TURN = 1

# Length bounds for sanitized scalar fields returned to / stored from the model.
_FIELD_LIMIT = 200
_PROMPT_KEEP = 400

Handler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


def _one_line(text: str, limit: int = _FIELD_LIMIT) -> str:
    return (text or "").replace("\n", " ").replace("\r", " ").strip()[:limit]


def _video_model_ids(catalog: ModelCatalog) -> list[str]:
    return [m.id for m in catalog.models if m.category == "video"]


def build_video_capability(
    *,
    video_service: VideoGenerationService,
    artifact_store: VideoArtifactStore,
    entitlements: EntitlementService,
    metering: UsageService,
    catalog: ModelCatalog,
    user_id: str,
    session_id: str,
    sink: list[MessageAttachment],
) -> tuple[list[dict[str, Any]], dict[str, Handler]]:
    """Build the ``generate_video`` tool bound to ``user_id``.

    Returns ``(extra_tools, extra_handlers)`` ready to merge into
    :func:`run_agent_turn`. Successful generations are appended to ``sink`` (the
    chat router attaches them to the assistant message) and metered to
    ``session_id``. Governance — entitlement gate, video-category-only model,
    size/duration allowlists, payload cap — is shared with any HTTP caller via
    :class:`VideoGenerationService` and the per-turn entitlement check here.
    """
    budget = {"used": 0}
    video_ids = _video_model_ids(catalog)
    models_hint = (
        f" Available video models: {', '.join(video_ids)}." if video_ids else ""
    )

    schema: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": GENERATE_VIDEO_TOOL_NAME,
            "description": (
                "Generate a short video clip from a text prompt and show it to the "
                "user. Use this whenever the user asks to create, animate, or render a "
                "video or movie. Generation can take a couple of minutes. The video is "
                "displayed to the user automatically — you do NOT receive the frames, "
                "only a confirmation, so never try to describe the generated video "
                "contents." + models_hint
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "A detailed description of the video to generate."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional video model id to use. Omit to use the default "
                            "video model."
                        ),
                    },
                    "size": {
                        "type": "string",
                        "enum": [
                            "480x480",
                            "480x854",
                            "854x480",
                            "720x720",
                            "1280x720",
                            "720x1280",
                            "1080x1080",
                            "1920x1080",
                            "1080x1920",
                        ],
                        "description": (
                            "Optional output resolution (WIDTHxHEIGHT). Landscape "
                            "1280x720 by default; use 720x1280 for portrait."
                        ),
                    },
                    "seconds": {
                        "type": "integer",
                        "enum": [4, 8, 12],
                        "description": (
                            "Optional clip length in seconds (4, 8, or 12). Longer "
                            "clips take longer to generate; defaults to 4."
                        ),
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
    }

    async def _handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if budget["used"] >= MAX_VIDEOS_PER_TURN:
            return {"error": "video generation budget exhausted for this turn."}
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return {"error": "prompt must be a non-empty string."}
        model = args.get("model") if isinstance(args.get("model"), str) else None
        size = args.get("size") if isinstance(args.get("size"), str) else None
        seconds = args.get("seconds") if isinstance(args.get("seconds"), int) else None
        budget["used"] += 1

        # Entitlement gate (mirrors the image tool): a disabled user is blocked
        # and any admin-set rate/budget limit applies before we spend on a call.
        decision = await entitlements.check(user_id)
        if not decision.allowed:
            return {"error": _one_line(decision.reason or "video generation is not permitted.")}

        try:
            result = await video_service.generate(
                prompt=prompt,
                model=model,
                size=size,
                seconds=seconds,
                correlation_id=ctx.correlation_id,
            )
        except VideoProviderCompletedCancellation as exc:
            completion = exc.provider_completion
            await metering.record_completion(
                user_id=user_id,
                session_id=session_id,
                model_id=completion.model_id,
                deployment=completion.deployment,
                usage=completion.usage,
                status="cancelled",
                provider_completed=True,
                correlation_id=ctx.correlation_id,
            )
            raise
        except VideoGenerationError as exc:
            if exc.provider_completion is not None:
                completion = exc.provider_completion
                await metering.record_completion(
                    user_id=user_id,
                    session_id=session_id,
                    model_id=completion.model_id,
                    deployment=completion.deployment,
                    usage=completion.usage,
                    status="error",
                    provider_completed=True,
                    correlation_id=ctx.correlation_id,
                )
            return {"error": _one_line(exc.detail)}
        except Exception:  # noqa: BLE001 - a tool must never crash the turn
            logger.warning("generate_video unexpected error user=%s", user_id, exc_info=True)
            return {"error": "Video generation failed."}

        meter_status: UsageStatus = "error"
        try:
            artifact_id = uuid4().hex
            try:
                await artifact_store.put(user_id, artifact_id, result.video_bytes)
            except asyncio.CancelledError:
                meter_status = "cancelled"
                raise
            except Exception:  # noqa: BLE001
                logger.warning("generate_video store error user=%s", user_id, exc_info=True)
                return {"error": "Generated video could not be stored."}
            sink.append(
                MessageAttachment(
                    id=artifact_id,
                    kind="video",
                    mimeType="video/mp4",
                    prompt=prompt[:_PROMPT_KEEP],
                    model=result.model_id,
                    size=result.size,
                    durationSeconds=result.seconds,
                )
            )
            meter_status = "complete"
        finally:
            # Provider work remains chargeable when local persistence fails.
            await metering.record_completion(
                user_id=user_id,
                session_id=session_id,
                model_id=result.model_id,
                deployment=result.deployment,
                usage=result.usage,
                status=meter_status,
                provider_completed=True,
                correlation_id=ctx.correlation_id,
            )

        return {
            "status": "generated",
            "artifact_id": artifact_id,
            "model": _one_line(result.model_id),
            "size": _one_line(result.size),
            "seconds": result.seconds,
            "note": (
                "The video was generated and is shown to the user. You do not have "
                "the frames; do not describe the video contents."
            ),
        }

    return [schema], {GENERATE_VIDEO_TOOL_NAME: _handler}
