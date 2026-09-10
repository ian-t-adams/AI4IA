"""Read-only compatibility, not permission to create a session or dispatch a model."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..catalog import DeploymentOption, ModelCatalog
from ..request_constraints import CANARY_MAX_OUTPUT_TOKENS

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


@router.get("/capabilities", response_model=CanaryCapabilities)
async def capabilities(
    request: Request,
    response: Response,
    model: str = Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    user: AuthenticatedUser = Depends(get_current_user),
) -> CanaryCapabilities:
    response.headers["Cache-Control"] = "no-store"
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
    entry = catalog.get(model)
    if (
        entry is None or entry.api not in ("chat", "responses")
        or entry.category not in ("chat", "chat-fast")
        or not entry.maxOutputTokens or entry.maxOutputTokens < CANARY_MAX_OUTPUT_TOKENS
        or not (
            "none" in entry.reasoningEffortOptions
            or (not entry.reasoningEffortOptions and entry.supportsSampling)
        )
    ):
        return CanaryCapabilities(reason="model_unavailable")
    for option in catalog.eligible_options(entry):
        # The production PolicyService owns this hook, actor matching, current
        # strict individual limits and model/tool/document domain checks. A
        # response is a compatibility observation, never a cached grant.
        decision = await probe(user, entry.id, option)
        if decision.outcome == "allow":
            return CanaryCapabilities(
                ready=True, reason="ready", model=entry.id, api=entry.api, region=option.region,
            )
    return CanaryCapabilities(reason="not_ready")
