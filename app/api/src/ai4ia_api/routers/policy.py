"""Readiness for separately bound model-only actors; responses never grant access."""
from __future__ import annotations

from typing import Literal

from azure.core.exceptions import AzureError
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..policy.models import ChatRestrictedProfile, PolicyError
from ..sessions.deletion_models import DeletionIntegrityError, DeletionUnavailableError

router = APIRouter(prefix="/api", tags=["policy"])


class ReductionEnvelope(BaseModel):
    allowTools: Literal[False] = False
    allowAutomaticMemory: Literal[False] = False
    requireFreshSession: Literal[True] = True
    maxOutputTokens: Literal[64, 256]
    libraryDocumentIds: list[str] = Field(default_factory=list, max_length=0)


class ExecutionCapabilities(BaseModel):
    version: Literal[1] = 1
    ready: bool = False
    ownerBound: bool = False
    profile: ChatRestrictedProfile
    model: str
    api: str | None = None
    region: str | None = None
    reductionControlsVersion: int | None = None
    constraints: ReductionEnvelope | None = None
    reason: str | None = None


@router.get("/execution-capabilities", response_model=ExecutionCapabilities, response_model_exclude_none=True)
async def execution_capabilities(
    profile: ChatRestrictedProfile, model: str, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ExecutionCapabilities:
    state = request.app.state
    not_ready = ExecutionCapabilities(profile=profile, model=model)
    guard = state.policy.dispatch_guard(profile)
    if (
        not callable(guard)
        or getattr(guard, "__module__", None) != "ai4ia_api.request_constraints"
        or not state.settings.session_deletion_enabled
    ):
        return not_ready.model_copy(update={"reason": "reduction_controls_unavailable"})
    entry = state.catalog.get(model)
    if entry is None or not entry.conversational or entry.api not in {"chat", "responses"}:
        return not_ready.model_copy(update={"reason": "model_unavailable"})
    try:
        await state.session_repo.check_deletion_ready()
        for option in state.catalog.eligible_options(entry):
            decision = await state.policy.restricted_probe(user, model, option, profile)
            if not decision.allowed:
                continue
            return ExecutionCapabilities(
                ready=True, ownerBound=True, profile=profile, model=model, api=entry.api,
                region=option.region, reductionControlsVersion=1,
                constraints=ReductionEnvelope(
                    maxOutputTokens=64 if profile == "monitor-canary" else 256,
                ),
            )
    except (PolicyError, DeletionIntegrityError, DeletionUnavailableError, AzureError, OSError):
        return not_ready.model_copy(update={"reason": "policy_unavailable"})
    return not_ready.model_copy(update={"reason": "policy_not_ready"})
