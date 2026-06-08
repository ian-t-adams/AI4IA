"""Model catalog endpoint (curated, data-driven)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..catalog import ModelCatalog

router = APIRouter(prefix="/api", tags=["catalog"])


@router.get("/models", response_model=ModelCatalog)
async def list_models(
    request: Request,
    _user: AuthenticatedUser = Depends(get_current_user),
) -> ModelCatalog:
    return request.app.state.catalog
