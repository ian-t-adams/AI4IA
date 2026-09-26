"""The ``edit_image`` synthetic capability and the shared edit bookkeeping.

Mirrors :mod:`ai4ia_api.images.capability` (``generate_image``): a function
schema + an async handler, built per turn, closure-bound to the authenticated
owner and conversation, and injected by the chat router as ``extra_tools`` /
``extra_handlers``. A tool argument can only name a source, a prompt and the
governed controls; it can never widen whose images are read.

The same store/meter/receipt helpers back ``POST /api/images/edits`` so the tool
and the direct user action persist, meter and describe an edit identically:

* the edited image becomes a NEW artifact under the owner's prefix, attached to
  the reply with its source provenance (kind, id) and whether a mask applied;
* provider work is metered with the existing image convention (one image), and
  stays chargeable when local decode or persistence fails;
* receipts and tool results carry ids, hashes, sizes and dimensions -- never
  image bytes or base64.

Availability is asked through :mod:`ai4ia_api.images.availability` when the
tool is built AND again when it runs, so a stale offer cannot reach a provider.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import uuid4

from ..agents.tool_exec import ToolContext
from ..catalog import ModelCatalog
from ..entitlements.service import EntitlementService
from ..receipts import (
    ExecutionReceipt,
    ReceiptRuntime,
    ReceiptToolCall,
    build_receipt,
    enforce_receipt_budget,
    json_payload,
)
from ..sessions.models import Message, MessageAttachment, Session
from ..sessions.repository import SessionNotFoundError
from ..usage.models import UsageStatus, UsageTarget
from ..usage.pricing import load_pricing
from ..usage.service import UsageService
from .artifacts import ImageArtifactStore
from .availability import (
    NO_IMAGE_EDIT_MODEL_DETAIL,
    available_image_edit_model_ids,
    default_image_edit_model_id,
)
from .editing import ImageEditResult, ImageEditService, edit_qualities, edit_sizes
from .service import ALLOWED_QUALITIES, ALLOWED_SIZES, ImageGenerationError
from .source import ImageSourceError
from .sources import EditSource, EditSourceRef, load_edit_source

logger = logging.getLogger(__name__)

EDIT_IMAGE_TOOL_NAME = "edit_image"
# Per-turn budget on top of the runtime's global tool-call budget: an edit is a
# slow, metered, paid call.
MAX_EDITS_PER_TURN = 2
_FIELD_LIMIT = 200
_PROMPT_KEEP = 400

Handler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


class _SessionReader(Protocol):
    async def get_session(self, user_id: str, session_id: str) -> Session: ...

    async def list_messages(self, user_id: str, session_id: str) -> list[Message]: ...


def _one_line(text: str | None, limit: int = _FIELD_LIMIT) -> str:
    return (text or "").replace("\n", " ").replace("\r", " ").strip()[:limit]


def edit_request_text(prompt: str, source: EditSource, *, masked: bool) -> str:
    """The persisted user-message text for a direct edit (bounded, one line)."""
    label = f" “{_one_line(source.filename, 80)}”" if source.filename else ""
    region = " (selected region)" if masked else ""
    return f"Edit image{label}{region}: {prompt.strip()}"


async def record_provider_failure(
    exc: ImageGenerationError, *, metering: UsageService, user_id: str,
    session_id: str, correlation_id: str | None,
) -> None:
    """Meter a provider-completed failure once; pre-provider refusals cost nothing."""
    completion = exc.provider_completion
    if completion is None:
        return
    await metering.record_completion(
        user_id=user_id,
        session_id=session_id,
        model_id=completion.model_id,
        target=UsageTarget.from_deployment(completion.deployment, provider=completion.provider),
        usage=completion.usage,
        status="error",
        provider_completed=True,
        correlation_id=correlation_id,
        billable_units=completion.billable_units,
        billing_unit=completion.billing_unit,
        image_size=completion.image_size,
        image_quality=completion.image_quality,
    )


async def store_edit_result(
    *,
    result: ImageEditResult,
    source: EditSource,
    prompt: str,
    masked: bool,
    artifact_store: ImageArtifactStore,
    metering: UsageService,
    user_id: str,
    session_id: str,
    correlation_id: str | None,
) -> MessageAttachment:
    """Persist the edited image as a new artifact, meter it, return its attachment."""
    meter_status: UsageStatus = "error"
    try:
        try:
            raw = base64.b64decode(result.image_b64)
        except Exception as exc:  # noqa: BLE001 - provider bytes are untrusted
            raise ImageGenerationError(502, "Edited image could not be decoded.") from exc
        artifact_id = uuid4().hex
        try:
            await artifact_store.put(user_id, artifact_id, raw)
        except asyncio.CancelledError:
            meter_status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "image edit store error user=%s model=%s", user_id, result.model_id,
                exc_info=True,
            )
            raise ImageGenerationError(502, "Edited image could not be stored.") from exc
        estimate = load_pricing().estimate_image(
            result.model_id, size=result.size, quality=result.quality, count=1,
        )
        meter_status = "complete"
        return MessageAttachment(
            id=artifact_id,
            kind="image",
            mimeType="image/png",
            prompt=prompt[:_PROMPT_KEEP],
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
                estimate.micro_usd / 1_000_000 if estimate.micro_usd is not None else None
            ),
            pricingBasis=estimate.pricing_basis,
            priceVersion=estimate.version,
            status="complete",
            filename=source.filename,
            sourceKind=source.ref.kind,
            sourceId=source.ref.id,
            masked=masked,
        )
    finally:
        await metering.record_completion(
            user_id=user_id,
            session_id=session_id,
            model_id=result.model_id,
            target=UsageTarget.from_deployment(result.deployment, provider=result.provider),
            usage=result.usage,
            status=meter_status,
            provider_completed=True,
            correlation_id=correlation_id,
            billable_units=1,
            billing_unit="image",
            image_size=result.size,
            image_quality=result.quality,
        )


def build_edit_receipt(
    *,
    result: ImageEditResult,
    source: EditSource,
    prompt: str,
    attachment: MessageAttachment,
    region: dict[str, float] | None,
    correlation_id: str | None,
) -> ExecutionReceipt:
    """A bounded receipt for a user-initiated edit: ids, hashes, never bytes."""
    runtime = ReceiptRuntime(
        modelId=result.model_id,
        deployment=result.deployment.deploymentName,
        region=result.deployment.region,
        sku=result.deployment.sku,
        dataZone=result.deployment.dataZone,
        residency=result.deployment.residency,
        api="images/edits",
    )
    call = ReceiptToolCall(
        tool=EDIT_IMAGE_TOOL_NAME,
        outcome="result",
        arguments=json_payload({
            "source": source.evidence(),
            "model": result.model_id,
            "size": result.size,
            "quality": result.quality,
            "region": region,
        }),
        result=json_payload({
            "artifactId": attachment.id,
            "model": result.model_id,
            "size": result.size,
            "quality": result.quality,
            "costKnown": attachment.costKnown,
        }),
    )
    receipt = build_receipt(
        correlation_id=correlation_id,
        runtime=runtime,
        prompt_messages=[{"role": "user", "content": prompt}],
        calls=[call],
        usage=result.usage,
        notes=["user_initiated_image_edit"],
    )
    return enforce_receipt_budget(receipt)


def _edit_sizes(catalog: ModelCatalog, ids: list[str]) -> list[str]:
    sizes: set[str] = set()
    for model_id in ids:
        entry = catalog.get(model_id)
        if entry is not None:
            sizes.update(edit_sizes(entry))
    return sorted(sizes or ALLOWED_SIZES)


def _edit_qualities(catalog: ModelCatalog, ids: list[str]) -> list[str]:
    qualities: set[str] = set()
    for model_id in ids:
        entry = catalog.get(model_id)
        if entry is not None:
            qualities.update(edit_qualities(entry))
    return sorted(qualities or ALLOWED_QUALITIES)


def build_image_edit_capability(
    *,
    edit_service: ImageEditService,
    artifact_store: ImageArtifactStore,
    entitlements: EntitlementService,
    metering: UsageService,
    catalog: ModelCatalog,
    user_id: str,
    session_id: str,
    sink: list[MessageAttachment],
    repo: _SessionReader,
    retrieval: Any | None = None,
    policy_filter: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Handler]]:
    """Build the ``edit_image`` tool bound to ``user_id`` and ``session_id``.

    Returns ``(extra_tools, extra_handlers)``. The schema is static for a given
    catalog (no per-conversation data), so consent and publication digests stay
    stable. Edited images are appended to ``sink``, the same per-turn list the
    image generator fills, so an edit can target an image generated earlier in
    the same turn. ``policy_filter=False`` is only for publication metadata.
    """
    if edit_service.availability(artifact_store, policy_filter=policy_filter) != "available":
        return [], {}
    budget = {"used": 0}
    # Named independent of the caller's policy so contract digests do not vary per
    # caller; execution still resolves the chosen model under that policy.
    edit_ids = available_image_edit_model_ids(catalog, policy_filter=False)
    default_id = default_image_edit_model_id(catalog, policy_filter=False)
    schema: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": EDIT_IMAGE_TOOL_NAME,
            "description": (
                "Edit an image that already exists in this conversation and show the "
                "edited result to the user. Use this when the user asks to change, "
                "retouch, restyle, extend, or remove something in an existing picture. "
                "By default it edits the most recent image in this conversation; pass "
                "image_artifact_id (returned by generate_image or edit_image) or "
                "library_document_id (an image document id listed in the library "
                "context) to choose another. The whole image is edited as the prompt "
                "describes. You do NOT receive the pixels, only a confirmation, so never "
                "describe the edited image contents."
                + (f" Editing models: {', '.join(edit_ids)}." if edit_ids else "")
                + (f" Default model: {default_id}." if default_id else "")
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What to change, described completely.",
                    },
                    "image_artifact_id": {
                        "type": "string",
                        "description": (
                            "Optional artifact_id of an image generated or edited earlier "
                            "in this conversation."
                        ),
                    },
                    "library_document_id": {
                        "type": "string",
                        "description": (
                            "Optional id of the user's own PNG or JPEG library image "
                            "selected for this conversation."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional image editing model id.",
                    },
                    "size": {
                        "type": "string",
                        "enum": _edit_sizes(catalog, edit_ids),
                        "description": "Optional output size; auto follows the model.",
                    },
                    "quality": {
                        "type": "string",
                        "enum": _edit_qualities(catalog, edit_ids),
                        "description": (
                            "Optional rendering quality. Higher costs more; omit (auto) "
                            "to let the provider choose."
                        ),
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
    }

    def failure(detail: str, *, model: str | None, source: EditSource | None) -> None:
        sink.append(MessageAttachment(
            id=uuid4().hex,
            kind="image_error",
            mimeType="application/problem+json",
            model=model,
            status="error",
            error=_one_line(detail),
            filename=source.filename if source is not None else None,
            sourceKind=source.ref.kind if source is not None else None,
            sourceId=source.ref.id if source is not None else None,
        ))

    async def _handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        state = edit_service.availability(artifact_store, policy_filter=policy_filter)
        if state != "available":
            return {"error": NO_IMAGE_EDIT_MODEL_DETAIL if state == "no_model" else (
                "Image editing is disabled."
            )}
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return {"error": "prompt must be a non-empty string."}
        artifact = args.get("image_artifact_id")
        document = args.get("library_document_id")
        artifact = artifact.strip() if isinstance(artifact, str) and artifact.strip() else None
        document = document.strip() if isinstance(document, str) and document.strip() else None
        if artifact and document:
            return {"error": "Pass at most one of image_artifact_id or library_document_id."}
        ref = (
            EditSourceRef("generated", artifact) if artifact
            else EditSourceRef("library", document) if document else None
        )
        model = args.get("model") if isinstance(args.get("model"), str) else None
        size = args.get("size") if isinstance(args.get("size"), str) else None
        quality = args.get("quality") if isinstance(args.get("quality"), str) else None
        if budget["used"] >= MAX_EDITS_PER_TURN:
            return {"error": f"at most {MAX_EDITS_PER_TURN} images may be edited in one turn."}

        decision = await entitlements.check(user_id)
        if not decision.allowed:
            return {"error": _one_line(decision.reason or "image editing is not permitted.")}
        try:
            session = await repo.get_session(user_id, session_id)
            source = await load_edit_source(
                ref=ref, user_id=user_id, session=session, repo=repo,
                image_artifacts=artifact_store, retrieval=retrieval, pending=list(sink),
            )
        except ImageSourceError as exc:
            return {"error": _one_line(exc.detail)}
        except SessionNotFoundError:
            return {"error": "This conversation is no longer available."}
        # The budget bounds paid dispatches; a refused source costs nothing.
        budget["used"] += 1
        try:
            result = await edit_service.edit(
                prompt=prompt, model=model, source=source, size=size, quality=quality,
                correlation_id=ctx.correlation_id,
            )
        except ImageGenerationError as exc:
            await record_provider_failure(
                exc, metering=metering, user_id=user_id, session_id=session_id,
                correlation_id=ctx.correlation_id,
            )
            failure(exc.detail, model=model, source=source)
            return {"error": _one_line(exc.detail)}
        except Exception:  # noqa: BLE001 - a tool must never crash the turn
            logger.warning("edit_image unexpected error user=%s", user_id, exc_info=True)
            failure("Image edit failed.", model=model, source=source)
            return {"error": "Image edit failed."}
        try:
            attachment = await store_edit_result(
                result=result, source=source, prompt=prompt, masked=False,
                artifact_store=artifact_store, metering=metering, user_id=user_id,
                session_id=session_id, correlation_id=ctx.correlation_id,
            )
        except ImageGenerationError as exc:
            failure(exc.detail, model=result.model_id, source=source)
            return {"error": _one_line(exc.detail)}
        sink.append(attachment)
        return {
            "status": "edited",
            "artifact_id": attachment.id,
            "source": {"kind": source.ref.kind, "id": source.ref.id},
            "model": _one_line(result.model_id),
            "size": _one_line(result.size),
            "quality": _one_line(result.quality),
            "cost_known": attachment.costKnown,
            "estimated_cost_usd": attachment.estimatedCostUsd,
            "note": (
                "The edited image was shown to the user. You do not have the pixels; "
                "do not describe the image contents."
            ),
        }

    return [schema], {EDIT_IMAGE_TOOL_NAME: _handler}
