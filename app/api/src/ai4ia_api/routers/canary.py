"""Read-only compatibility, not permission to create a session or dispatch a model."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..catalog import DeploymentOption, ModelCatalog, ModelEntry
from ..request_constraints import CANARY_MAX_OUTPUT_TOKENS
from ..usage.pricing import load_pricing

router = APIRouter(prefix="/api/canary", tags=["canary"])


class CanaryConstraints(BaseModel):
    allowTools: Literal[False] = False
    allowAutomaticMemory: Literal[False] = False
    requireFreshSession: Literal[True] = True
    maxOutputTokens: Literal[64] = CANARY_MAX_OUTPUT_TOKENS
    libraryDocumentIds: list[str] = Field(default_factory=list, max_length=0)


class CanaryCapabilities(BaseModel):
    version: Literal[1] = 1
    ready: bool = False
    reason: Literal["not_ready", "policy_unavailable", "model_unavailable", "lifecycle_unavailable", "ready"] = "not_ready"
    model: str | None = None
    api: str | None = None
    region: str | None = None
    constraints: CanaryConstraints = Field(default_factory=CanaryConstraints)


class RealtimeCanaryConstraints(BaseModel):
    provider: Literal["azure_openai"] = "azure_openai"
    protocol: Literal["ga"] = "ga"
    setupOnly: Literal[True] = True
    allowAudio: Literal[False] = False
    allowResponses: Literal[False] = False
    allowTools: Literal[False] = False
    maxSeconds: Literal[15] = 15


class RealtimeCanaryCapabilities(BaseModel):
    version: Literal[1] = 1
    ready: bool = False
    model: str | None = None
    region: str | None = None
    constraints: RealtimeCanaryConstraints = Field(default_factory=RealtimeCanaryConstraints)


@router.get("/realtime-capabilities", response_model=RealtimeCanaryCapabilities)
async def realtime_capabilities(
    request: Request, response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RealtimeCanaryCapabilities:
    response.headers["Cache-Control"] = "no-store"
    state = request.app.state
    if (
        not state.settings.realtime_enabled or state.settings.realtime_protocol.value != "ga"
        or state.settings.hard_quota_enabled
        or "azure_openai" not in state.settings.voice_provider_allowlist_list
    ):
        return RealtimeCanaryCapabilities()
    probe: Callable[[AuthenticatedUser, str, DeploymentOption], Awaitable[Any]] | None = getattr(
        state, "realtime_canary_policy_probe", None,
    )
    if not callable(probe) or not callable(getattr(state, "realtime_canary_dispatch_guard", None)):
        return RealtimeCanaryCapabilities()
    catalog: ModelCatalog = state.catalog
    if len(catalog.models) > 128:
        return RealtimeCanaryCapabilities()
    try:
        async with asyncio.timeout(10):
            # Catalog order preserves the application's default; a new name is
            # never interpreted as a request to select the latest model.
            for entry in catalog.models:
                if entry.category != "realtime":
                    continue
                options = catalog.eligible_options(entry)
                if len(options) > 16:
                    return RealtimeCanaryCapabilities()
                for option in options:
                    decision = await probe(user, entry.id, option)
                    if decision.outcome == "allow":
                        return RealtimeCanaryCapabilities(ready=True, model=entry.id, region=option.region)
    except TimeoutError:
        return RealtimeCanaryCapabilities()
    return RealtimeCanaryCapabilities()


def _compatible(entry: ModelEntry) -> bool:
    return (
        entry.api in ("chat", "responses")
        and entry.category in ("chat", "chat-fast")
        and entry.maxOutputTokens is not None
        and entry.maxOutputTokens >= CANARY_MAX_OUTPUT_TOKENS
        and (
            "none" in entry.reasoningEffortOptions
            or (not entry.reasoningEffortOptions and entry.supportsSampling)
        )
    )


@router.get("/capabilities", response_model=CanaryCapabilities)
async def capabilities(
    request: Request,
    response: Response,
    model: str | None = Query(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    selection: Literal["least_estimated_cost"] | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CanaryCapabilities:
    response.headers["Cache-Control"] = "no-store"
    if (model is None) == (selection is None):
        raise HTTPException(422, detail="Specify one catalog model or the least-estimated-cost operation.")
    state = request.app.state
    if not state.settings.session_deletion_enabled or state.settings.hard_quota_enabled:
        return CanaryCapabilities(reason="lifecycle_unavailable")
    probe: Callable[[AuthenticatedUser, str, DeploymentOption], Awaitable[Any]] | None = getattr(
        state, "canary_policy_probe", None,
    )
    guard = getattr(state, "canary_dispatch_guard", None)
    if not callable(probe) or not callable(guard):
        return CanaryCapabilities(reason="policy_unavailable")
    catalog: ModelCatalog = state.catalog
    if len(catalog.models) > 128:
        return CanaryCapabilities(reason="model_unavailable")
    prices = load_pricing()
    ranked = []
    for entry in catalog.models:
        if not _compatible(entry) or (model is not None and entry.id != model):
            continue
        estimate = prices.estimate_token_bound(
            entry.id, prompt_tokens=1024, completion_tokens=CANARY_MAX_OUTPUT_TOKENS,
        )
        if estimate.known and estimate.micro_usd is not None:
            ranked.append((estimate.micro_usd, entry.id, entry))
    if not ranked or len(ranked) > 32:
        return CanaryCapabilities(reason="model_unavailable")
    try:
        async with asyncio.timeout(10):
            for _, _, entry in sorted(ranked, key=lambda row: (row[0], row[1])):
                options = catalog.eligible_options(entry)
                if len(options) > 16:
                    return CanaryCapabilities(reason="model_unavailable")
                for option in options:
                    # Current policy selects the allowed intersection, not the
                    # cheapest global model followed by an avoidable denial.
                    decision = await probe(user, entry.id, option)
                    if decision.outcome == "allow":
                        return CanaryCapabilities(
                            ready=True, reason="ready", model=entry.id, api=entry.api, region=option.region,
                        )
    except TimeoutError:
        return CanaryCapabilities(
            reason="policy_unavailable",
        )
    return CanaryCapabilities(reason="not_ready")
