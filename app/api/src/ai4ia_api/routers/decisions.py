"""Explicit, transient decision playground. Only usage metadata is persisted."""
from __future__ import annotations

import asyncio
import logging
from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..catalog import ModelCatalog
from ..decisions import (
    DECISION_PROVIDER, MAX_REQUEST_BYTES, DecisionExecution, DecisionRequest, read_json,
)
from ..gateway.client import ModelGatewayError
from ..logging_setup import get_correlation_id
from ..policy.context import current_binding
from ..usage.models import UsageStatus, UsageTarget

router = APIRouter(prefix="/api/decisions", tags=["decisions"])
logger = logging.getLogger(__name__)


@router.get("/options")
async def decision_options(
    request: Request, response: Response, _user: AuthenticatedUser = Depends(get_current_user),
):
    response.headers["Cache-Control"] = "no-store"
    binding = current_binding()
    if binding is not None:
        binding.require_configuration()
    catalog: ModelCatalog = request.app.state.catalog
    return {
        "models": [
            {
                "id": entry.id, "displayName": entry.displayName,
                "residencies": sorted({o.residency for o in catalog.eligible_options(entry)}),
            }
            for entry in catalog.models
            if entry.category == "decision" and entry.api == "decision" and catalog.available(entry)
        ],
    }


@router.post("/run")
async def run_decision(
    request: Request, response: Response, user: AuthenticatedUser = Depends(get_current_user),
):
    response.headers["Cache-Control"] = "no-store"
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "Decision request exceeds 64 KiB.")
        content.extend(chunk)
    try:
        body = DecisionRequest.model_validate(read_json(bytes(content)))
    except (ValueError, ValidationError, RecursionError, UnicodeError) as exc:
        raise HTTPException(422, "Invalid Decision input or questions. Check the documented limits and JSON.") from exc
    catalog: ModelCatalog = request.app.state.catalog
    entry = catalog.get(body.model)
    deployment = catalog.resolve_deployment(body.model)
    if entry is None or entry.category != "decision" or entry.api != "decision" or deployment is None:
        raise HTTPException(400, "Decision model is unavailable under the current policy.")
    entitlement = await request.app.state.entitlements.check(user.internal_user_id)
    if not entitlement.allowed:
        raise HTTPException(
            entitlement.code, entitlement.reason,
            headers={"Retry-After": str(entitlement.retry_after_seconds)}
            if entitlement.retry_after_seconds is not None else None,
        )
    execution = DecisionExecution()
    correlation_id = get_correlation_id()
    started = perf_counter()
    status: UsageStatus = "error"

    async def watch_disconnect():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    operation = asyncio.create_task(request.app.state.gateway.decide(
        deployment=deployment.deploymentName, request=body, execution=execution,
        correlation_id=correlation_id,
    ))
    disconnected = asyncio.create_task(watch_disconnect())
    try:
        done, _ = await asyncio.wait({operation, disconnected}, return_when=asyncio.FIRST_COMPLETED)
        if operation not in done:
            raise asyncio.CancelledError()
        result = operation.result()
        status = "complete"
        return {
            **result, "model": entry.id, "region": deployment.region,
            "residency": deployment.residency, "latencyMs": round((perf_counter() - started) * 1000),
            "costKnown": False, "estimatedCostUsd": None,
            "costUnknownReason": "No verified price for this deployment's billing meter.",
        }
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except ModelGatewayError as exc:
        logger.warning("Decision request failed (status=%s).", exc.status_code)
        raise HTTPException(
            429 if exc.status_code == 429 else 502,
            "Decision provider is rate limited. Run again later." if exc.status_code == 429 else
            "Decision request failed or returned invalid data. Work may have been billed; no automatic retry.",
        ) from exc
    finally:
        operation.cancel()
        disconnected.cancel()
        await asyncio.gather(operation, disconnected, return_exceptions=True)
        if execution.dispatched:
            await asyncio.shield(request.app.state.usage.record_completion(
                user_id=user.internal_user_id, session_id="decision-playground",
                model_id=entry.id, target=UsageTarget.from_deployment(
                    deployment, provider=DECISION_PROVIDER,
                ),
                usage=execution.usage, status=status,
                provider_completed=execution.provider_completed, correlation_id=correlation_id,
            ))
