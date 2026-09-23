"""The one server-side answer to "can ``generate_video`` run here, now?".

Every seam that advertises, lists, attaches, authorizes or snapshots the tool
asks this module: conversation policy (effective tools), the chat router's
``/generate_video`` command and capability injection, the tool catalog, consent
and publication snapshots, and the capability builder itself. Checking only the
feature flag and artifact store let a deployment whose only video model had
been runtime-disabled keep offering a tool that could only fail.

Generation is available only when all of these hold:

* ``AI4IA_VIDEO_GENERATION_ENABLED`` is on;
* the durable artifact store that delivers the clip exists; and
* at least one ``video`` catalog model is routable: runtime-enabled,
  residency-compliant and, unless a publication-metadata caller opts out with
  ``policy_filter=False``, allowed for the current caller (``catalog.available``).

This decides what is offered. Execution still re-checks at call time: the
handler asks again, and :class:`~ai4ia_api.videos.service.VideoGenerationService`
refuses a model it cannot resolve, so a model disappearing mid-request fails
cleanly before any provider call.

Serving previously generated clips is deliberately NOT gated here. The
authenticated artifact endpoint reads the owner's blob regardless of the flag or
the catalog, so retiring a video model hides the tool without hiding history.
"""
from __future__ import annotations

from typing import Any, Literal

from ..catalog import ModelCatalog

VideoAvailability = Literal["available", "disabled", "storage_unavailable", "no_model"]

NO_VIDEO_MODEL_DETAIL = "No runtime-enabled video generation model is available."


def available_video_model_ids(
    catalog: ModelCatalog | None, *, policy_filter: bool = True,
) -> list[str]:
    """Catalog ids of the video models that can currently be routed."""
    if catalog is None:
        return []
    return [
        entry.id
        for entry in catalog.models
        if entry.category == "video"
        and (
            catalog.available(entry)
            if policy_filter
            else bool(catalog.eligible_options(entry, policy_filter=False))
        )
    ]


def video_generation_availability(
    *,
    enabled: bool,
    artifact_store: object | None,
    catalog: ModelCatalog | None,
    policy_filter: bool = True,
) -> VideoAvailability:
    """Classify availability; only ``"available"`` may offer or run the tool."""
    if not enabled:
        return "disabled"
    if artifact_store is None:
        return "storage_unavailable"
    if not available_video_model_ids(catalog, policy_filter=policy_filter):
        return "no_model"
    return "available"


def state_video_availability(state: Any, *, policy_filter: bool = True) -> VideoAvailability:
    """:func:`video_generation_availability` over the application state."""
    settings = getattr(state, "settings", None)
    return video_generation_availability(
        enabled=bool(getattr(settings, "video_generation_enabled", False)),
        artifact_store=getattr(state, "video_artifacts", None),
        catalog=getattr(state, "catalog", None),
        policy_filter=policy_filter,
    )


def video_generation_available_for_state(state: Any, *, policy_filter: bool = True) -> bool:
    return state_video_availability(state, policy_filter=policy_filter) == "available"
