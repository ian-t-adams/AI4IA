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
    """The models this deployment will actually route to.

    Filtered by the active data-residency policy, and each remaining deployment
    keeps only the options that policy permits. Advertising a model the server
    would refuse turns a governance decision into an unexplained failure at send
    time, so the exclusion happens here instead.

    ``residencyPolicy`` and each option's ``residency`` are serialized so the UI
    can state the guarantee rather than infer it from region names -- the whole
    point being that a GlobalStandard deployment in an EU region is not EU-
    resident.
    """
    catalog: ModelCatalog = request.app.state.catalog
    return ModelCatalog(
        residencyPolicy=catalog.residencyPolicy,
        models=[
            entry.model_copy(update={"options": catalog.eligible_options(entry)})
            for entry in catalog.models
            if catalog.available(entry)
        ],
    )
