"""The one server-side answer to "can image editing run here, now?".

Mirrors :mod:`ai4ia_api.videos.availability`. Every seam that advertises, lists,
attaches, authorizes, snapshots or executes ``edit_image`` / the image-edit
endpoint asks this module: the tool catalog, conversation policy (and therefore
the inspector), consent and publication snapshots, the ``/edit_image`` command,
chat capability injection, ``/api/images/options``, the HTTP endpoints and the
tool handler itself.

Editing is available only when all of these hold:

* ``AI4IA_IMAGE_EDITING_ENABLED`` **and** ``AI4IA_IMAGE_GENERATION_ENABLED`` are
  on (edits read and write the generated-image artifact store);
* that durable artifact store exists; and
* at least one catalog row declaring ``imageEditing`` is routable:
  runtime-enabled, residency-compliant and, unless a publication-metadata caller
  opts out with ``policy_filter=False``, allowed for the current caller.

This decides what is offered. Execution still re-checks at call time, and the
edit service refuses a model it cannot resolve, so a model disappearing mid-turn
fails cleanly before any provider call. Serving previously produced images is
deliberately NOT gated here: the authenticated artifact endpoint keeps serving
edited images after editing is withdrawn.
"""
from __future__ import annotations

from typing import Any, Literal

from ..catalog import ModelCatalog, ModelEntry

ImageEditAvailability = Literal["available", "disabled", "storage_unavailable", "no_model"]

NO_IMAGE_EDIT_MODEL_DETAIL = "No runtime-enabled image editing model is available."


def _routable(catalog: ModelCatalog, entry: ModelEntry, *, policy_filter: bool) -> bool:
    if entry.category != "image" or not entry.imageEditing:
        return False
    if policy_filter:
        return catalog.available(entry)
    return bool(catalog.eligible_options(entry, policy_filter=False))


def available_image_edit_model_ids(
    catalog: ModelCatalog | None, *, policy_filter: bool = True,
) -> list[str]:
    """Catalog ids of the editing-capable image models that can be routed now."""
    if catalog is None:
        return []
    return [
        entry.id for entry in catalog.models
        if _routable(catalog, entry, policy_filter=policy_filter)
    ]


def default_image_edit_model_id(
    catalog: ModelCatalog | None, *, policy_filter: bool = True,
) -> str | None:
    """The catalog's preferred editing model when routable, else the first routable one.

    Only an omitted model uses this default. An explicitly requested model is
    validated as requested and never substituted.
    """
    available = available_image_edit_model_ids(catalog, policy_filter=policy_filter)
    if catalog is None or not available:
        return None
    preferred = next(
        (entry.id for entry in catalog.models if entry.imageEditingDefault and entry.id in available),
        None,
    )
    return preferred or available[0]


def image_editing_availability(
    *,
    editing_enabled: bool,
    generation_enabled: bool,
    artifact_store: object | None,
    catalog: ModelCatalog | None,
    policy_filter: bool = True,
) -> ImageEditAvailability:
    """Classify availability; only ``"available"`` may offer or run an edit."""
    if not (editing_enabled and generation_enabled):
        return "disabled"
    if artifact_store is None:
        return "storage_unavailable"
    if not available_image_edit_model_ids(catalog, policy_filter=policy_filter):
        return "no_model"
    return "available"


def state_image_edit_availability(state: Any, *, policy_filter: bool = True) -> ImageEditAvailability:
    """:func:`image_editing_availability` over the application state."""
    settings = getattr(state, "settings", None)
    return image_editing_availability(
        editing_enabled=bool(getattr(settings, "image_editing_enabled", False)),
        generation_enabled=bool(getattr(settings, "image_generation_enabled", False)),
        artifact_store=getattr(state, "image_artifacts", None),
        catalog=getattr(state, "catalog", None),
        policy_filter=policy_filter,
    )


def image_editing_available_for_state(state: Any, *, policy_filter: bool = True) -> bool:
    return state_image_edit_availability(state, policy_filter=policy_filter) == "available"
