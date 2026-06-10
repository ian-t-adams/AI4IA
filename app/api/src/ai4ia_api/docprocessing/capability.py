"""The ``process_document`` synthetic capability (Phase 11H).

Mirrors the ``generate_image`` / ``generate_video`` capabilities
(:mod:`ai4ia_api.images.capability`, :mod:`ai4ia_api.videos.capability`): a
function schema + an async handler injected into
:func:`~ai4ia_api.agents.runtime.run_agent_turn` as ``extra_tools`` /
``extra_handlers``. This makes document processing a tool **any agent can attach** —
decoupled from any single agent — completing the "videos, images, document
processing" triad so users compose any combination into their own personas.

Why synthetic (closure-bound) rather than a registry tool: processing needs real
services (the document retrieval consumer, the model gateway, the entitlement gate,
the usage meter, durable blob storage) plus the turn's resolved deployment, but a
registry tool handler only receives ``(args, ToolContext)`` and an agent turn runs
with an empty context. So the capability is built per turn with its services + the
authenticated ``user_id`` + the turn's deployment captured in a closure — a tool
argument can only ever carry the ``document_id`` / ``instruction`` / ``mode``, never
spoof the user or the document owner.

Most results return **inline** through the tool result. When a result exceeds the
inline cap (well under the runtime's 8 KB tool-result limit) the handler instead
**persists** the full text to per-user blob storage and returns only a small
reference (the ``artifact_id``) plus a bounded preview. The reference is appended to
a per-turn ``sink`` that the chat router drains onto the assistant message as a
:class:`~ai4ia_api.sessions.models.MessageAttachment`, which the browser renders by
fetching the text from the authenticated serve endpoint.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from ..agents.tool_exec import ToolContext
from ..catalog import DeploymentOption
from ..config import Settings
from ..entitlements.service import EntitlementService
from ..sessions.models import MessageAttachment
from ..usage.service import UsageService
from .artifacts import ANALYSIS_CONTENT_TYPE, DocumentArtifactStore
from .service import (
    ALLOWED_MODES,
    PROCESS_DOCUMENT_TOOL_NAME,
    DocumentProcessingError,
    DocumentProcessingService,
)

logger = logging.getLogger(__name__)

# Per-turn budget (on top of the runtime's global tool-call budget). Each call is
# a metered model round-trip over a document, so a turn may run only a few.
MAX_PROCESSES_PER_TURN = 4

# Length bounds for sanitized scalar fields returned to / stored from the model.
_FIELD_LIMIT = 200
_INSTRUCTION_KEEP = 400
# How much of an over-cap result is echoed back to the model alongside the
# artifact reference, so it has context without re-flooding the tool channel.
_PREVIEW_CHARS = 800

Handler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


def _one_line(text: str, limit: int = _FIELD_LIMIT) -> str:
    return (text or "").replace("\n", " ").replace("\r", " ").strip()[:limit]


def build_document_processing_capability(
    *,
    processing_service: DocumentProcessingService,
    artifact_store: DocumentArtifactStore,
    entitlements: EntitlementService,
    metering: UsageService,
    deployment: DeploymentOption,
    model_id: str,
    user_id: str,
    session_id: str,
    settings: Settings,
    sink: list[MessageAttachment],
) -> tuple[list[dict[str, Any]], dict[str, Handler]]:
    """Build the ``process_document`` tool bound to ``user_id`` + the turn's
    deployment.

    Returns ``(extra_tools, extra_handlers)`` ready to merge into
    :func:`run_agent_turn`. Small results return inline; an over-cap result is
    persisted and a reference appended to ``sink`` (the chat router attaches it to
    the assistant message). The model round-trip is metered to ``session_id``.
    Governance — instruction/mode validation, the ready-library read gate, and
    upstream-error sanitization — is shared with any HTTP caller via
    :class:`DocumentProcessingService`; the per-turn entitlement check + cap live
    here.
    """
    budget = {"used": 0}
    inline_cap = max(1, settings.document_processing_inline_max_chars)

    schema: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": PROCESS_DOCUMENT_TOOL_NAME,
            "description": (
                "Process one of the user's library documents: run an analysis, "
                "summary, or structured extraction over its already-parsed content "
                "and return the result. Use the document ids shown in the LIBRARY "
                "reference block. Only documents that have finished processing can "
                "be used. A large result is shown to the user as a downloadable "
                "document and only previewed back to you."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "string",
                        "description": "Id of the library document to process.",
                    },
                    "instruction": {
                        "type": "string",
                        "description": (
                            "What to do with the document, e.g. 'summarize the key "
                            "risks', 'extract every invoice line item as a table', "
                            "'list the action items and owners'."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": list(ALLOWED_MODES),
                        "description": (
                            "Optional processing mode: 'analyze' (default) for "
                            "general analysis, 'summarize' to condense, or "
                            "'extract' to pull structured data out."
                        ),
                    },
                },
                "required": ["document_id", "instruction"],
                "additionalProperties": False,
            },
        },
    }

    async def _handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if budget["used"] >= MAX_PROCESSES_PER_TURN:
            return {"error": "document processing budget exhausted for this turn."}
        document_id = str(args.get("document_id") or "").strip()
        if not document_id:
            return {"error": "document_id must be a non-empty string."}
        instruction = str(args.get("instruction") or "").strip()
        if not instruction:
            return {"error": "instruction must be a non-empty string."}
        mode = args.get("mode") if isinstance(args.get("mode"), str) else None
        budget["used"] += 1

        # Entitlement gate (mirrors the image/video tools): a disabled user is
        # blocked and any admin-set rate/budget limit applies before we spend on a
        # model call.
        decision = await entitlements.check(user_id)
        if not decision.allowed:
            return {"error": _one_line(decision.reason or "document processing is not permitted.")}

        try:
            result = await processing_service.process(
                user_id=user_id,
                document_id=document_id,
                instruction=instruction,
                deployment=deployment,
                model_id=model_id,
                mode=mode,
                correlation_id=ctx.correlation_id,
            )
        except DocumentProcessingError as exc:
            return {"error": _one_line(exc.detail)}
        except Exception:  # noqa: BLE001 - a tool must never crash the turn
            logger.warning("process_document unexpected error user=%s", user_id, exc_info=True)
            return {"error": "Document processing failed."}

        # Meter the model round-trip so rolling rate/token windows include
        # tool-driven processing. Best-effort: record_completion never raises.
        await metering.record_completion(
            user_id=user_id,
            session_id=session_id,
            model_id=result.model_id,
            deployment=result.deployment,
            usage=result.usage,
            status="complete",
            correlation_id=ctx.correlation_id,
        )

        # Small result: hand the full text back to the model inline so it can use
        # it directly in its reply.
        if len(result.text) <= inline_cap:
            return {
                "status": "processed",
                "document_id": result.document_id,
                "filename": _one_line(result.filename),
                "mode": result.mode,
                "model": _one_line(result.model_id),
                "source_truncated": result.source_truncated,
                "result": result.text,
            }

        # Over-cap result: persist it and return a reference + bounded preview. The
        # user sees the full result as a downloadable document attachment.
        artifact_id = uuid4().hex
        try:
            await artifact_store.put(
                user_id, artifact_id, result.text.encode("utf-8")
            )
        except Exception:  # noqa: BLE001
            logger.warning("process_document store error user=%s", user_id, exc_info=True)
            return {"error": "Processed result could not be stored."}

        sink.append(
            MessageAttachment(
                id=artifact_id,
                kind="document",
                mimeType=ANALYSIS_CONTENT_TYPE,
                prompt=instruction[:_INSTRUCTION_KEEP],
                model=result.model_id,
                filename=result.filename,
            )
        )
        return {
            "status": "processed",
            "artifact_id": artifact_id,
            "document_id": result.document_id,
            "filename": _one_line(result.filename),
            "mode": result.mode,
            "model": _one_line(result.model_id),
            "source_truncated": result.source_truncated,
            "preview": result.text[:_PREVIEW_CHARS],
            "note": (
                "The full result is large and is shown to the user as a "
                "downloadable document; only a preview is included here."
            ),
        }

    return [schema], {PROCESS_DOCUMENT_TOOL_NAME: _handler}
