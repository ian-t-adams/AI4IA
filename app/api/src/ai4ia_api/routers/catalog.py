"""Model catalog endpoint (curated, data-driven)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..catalog import ModelCatalog
from ..policy.context import current_binding

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
    binding = current_binding()
    if binding is not None:
        binding.require_configuration()
    catalog: ModelCatalog = request.app.state.catalog
    settings = request.app.state.settings
    gateway = request.app.state.gateway
    models = []
    for entry in catalog.models:
        if not catalog.available(entry) or not entry.supports_realtime_protocol(
            settings.realtime_protocol
        ):
            continue
        options = catalog.eligible_options(entry)
        if settings.chat_pilot_isolation_enabled:
            options = [
                option for option in options
                if gateway.chat_pilot_model_key(
                    entry.api, option.deploymentName, option.sku,
                    model_id=entry.id,
                ) is not None
            ]
        if options:
            models.append(entry.model_copy(update={"options": options}))
    return ModelCatalog(residencyPolicy=catalog.residencyPolicy, models=models)
