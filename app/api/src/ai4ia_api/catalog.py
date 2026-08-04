"""Loads the curated model catalog and resolves model -> deployment routing.

Precedence: explicit path (settings/env) -> packaged ``data/model_catalog.json``
-> repo ``infra/models.json`` fallback (dev only, transformed on the fly).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, computed_field

from .model_traits import reasoning_effort_options, supports_sampling

_PACKAGED = Path(__file__).resolve().parent / "data" / "model_catalog.json"

# Categories whose models are driven through a normal text chat turn and so
# belong in the chat / agent model pickers. Everything else — image, video, tts,
# transcription, audio, realtime, embedding, rerank — is a capability model
# invoked through its own surface or tool, not selected as a raw chat target.
# This is an allowlist on purpose: a new capability category added to the
# catalog stays out of the chat picker until it's explicitly opted in here.
CONVERSATIONAL_CATEGORIES = frozenset(
    {"chat", "chat-fast", "reasoning", "reasoning-oss", "router", "research"}
)


class DeploymentOption(BaseModel):
    region: str
    dataZone: str | None = None
    sku: str
    deploymentName: str


class ModelEntry(BaseModel):
    id: str
    displayName: str
    category: str
    format: str
    # Which Azure surface serves this model: "chat" (Chat Completions, the
    # default) or "responses" (the Responses API — required by gpt-5-pro,
    # gpt-5-codex, o3-pro, which 400 on chat/completions). The gateway routes by
    # this flag; the field is informational to the UI.
    api: str = "chat"
    # Per-model context window (total prompt+completion tokens the deployment
    # accepts) and the maximum tokens it will emit in one completion. Both are
    # OPTIONAL: when absent (``None``) the backend falls back to its fixed
    # constants, so a model lacking metadata behaves exactly as before. Populated
    # for conversational models from ``infra/models.json``; serialized so the web
    # app can show the cap and clamp the max-tokens input from one source of truth.
    contextWindow: int | None = None
    maxOutputTokens: int | None = None
    # ``reasoning_effort`` values this model accepts, from ``infra/models.json``.
    # ``None`` means "not recorded" and falls back to the family heuristic;
    # an empty list means "recorded, and this model takes no effort value".
    # The distinction matters: a reasoning model with no recorded data should
    # still get the safe low/medium/high floor rather than losing the control.
    reasoningEffort: list[str] | None = None
    options: list[DeploymentOption]

    @computed_field
    @property
    def conversational(self) -> bool:
        """Whether this model is offered in the chat/agent model pickers.

        True for text-chat categories (chat, reasoning, router, …); False for
        capability models (image, video, tts, transcription, embedding, rerank)
        and voice models (realtime, audio), which are reached through their own
        surfaces/tools rather than selected as a raw chat target. Serialized so
        the web app can filter the dropdowns from the same source of truth.
        """
        return self.category in CONVERSATIONAL_CATEGORIES


    @computed_field
    @property
    def supportsSampling(self) -> bool:
        """Whether ``temperature``/``top_p`` actually reach the provider.

        Reasoning models 400 on non-default sampling values, so the gateway
        strips them from the outgoing body. Serializing that here lets the web
        app hide the sliders instead of presenting controls that are silently
        discarded -- which is what it did for 11 of the 15 conversational models,
        including the whole GPT-5.6 family.
        """
        return supports_sampling(self.id)

    @computed_field
    @property
    def reasoningEffortOptions(self) -> list[str]:
        """Allowed ``reasoning_effort`` values, empty when unsupported.

        The one knob reasoning models *do* honour, and the UI offered no way to
        set it. The values must come from the server: they vary per model in ways
        no naming convention predicts (``gpt-5.6`` rejects ``minimal`` that
        ``gpt-5.4`` accepts; ``gpt-5-pro`` accepts only ``high``), so a hardcoded
        list in the web app would offer values that 400.

        Precedence is catalog first, heuristic second. ``infra/models.json``
        carries per-model values established by probing the live deployment;
        ``model_traits`` supplies only the conservative floor every reasoning
        model honours, so a newly added model degrades to fewer options rather
        than to options that fail.
        """
        if self.reasoningEffort is not None:
            return list(self.reasoningEffort)
        return reasoning_effort_options(self.id)


class ModelCatalog(BaseModel):
    models: list[ModelEntry]

    def get(self, model_id: str) -> ModelEntry | None:
        return next((m for m in self.models if m.id == model_id), None)

    def conversational_models(self) -> list[ModelEntry]:
        """Models the chat/agent pickers should offer (excludes capability models)."""
        return [m for m in self.models if m.conversational]

    def resolve_deployment(
        self, model_id: str, *, region: str | None = None, data_zone: str | None = None
    ) -> DeploymentOption | None:
        """Pick a deployment for a model, honoring an explicit region/data zone.

        An explicit constraint is treated as a REQUIREMENT, not a hint. This
        previously fell through to ``entry.options[0]`` when the requested region
        or data zone had no deployment, so a caller asking for EU processing
        silently got a US one and nothing in the response, the usage record or
        the logs said the request had been relocated. For a residency constraint
        that failure mode is worse than an error, so an unsatisfiable constraint
        now returns ``None``; every caller already maps that to a 400-class
        "unknown or unavailable model" response.

        Constraints combine with AND when both are supplied: asking for a region
        *and* a data zone that disagree is contradictory, and answering from
        whichever one happened to match first is exactly the silent relocation
        this avoids.

        NOTE: ``DeploymentOption.dataZone`` is derived from the endpoint's
        geography (``infra/models.json`` ``regions``), not from the deployment's
        SKU. A ``GlobalStandard`` deployment can process outside that geography,
        so satisfying a ``data_zone`` constraint here is not by itself a
        residency guarantee. Tracked in the repository audit as an open item.
        """
        entry = self.get(model_id)
        if entry is None or not entry.options:
            return None
        options = list(entry.options)
        if region:
            options = [o for o in options if o.region == region]
        if data_zone:
            options = [o for o in options if o.dataZone == data_zone]
        return options[0] if options else None


def _transform_infra_models(raw: dict[str, Any]) -> dict[str, Any]:
    naming = raw["naming"]
    sku_short = naming["skuShort"]
    regions = raw.get("regions", {})
    # Single source of truth in infra/models.json `naming` (matches main.bicep + the catalog
    # generator). This dev fallback only runs from a source checkout without the packaged catalog.
    token = naming["subscriptionToken"]
    models = []
    for model in raw["catalog"]:
        options = []
        for dep in model["deployments"]:
            region = dep["region"]
            sku = dep["sku"]
            options.append(
                {
                    "region": region,
                    "dataZone": regions.get(region, {}).get("dataZone"),
                    "sku": sku,
                    "deploymentName": f"{model['name']}-{token}-{region}-{sku_short[sku]}",
                }
            )
        models.append(
            {
                "id": model["name"],
                "displayName": model.get("displayName", model["name"]),
                "category": model.get("category", "chat"),
                "format": model["format"],
                "api": model.get("api", "chat"),
                "contextWindow": model.get("contextWindow"),
                "maxOutputTokens": model.get("maxOutputTokens"),
                "reasoningEffort": model.get("reasoningEffort"),
                "options": options,
            }
        )
    return {"models": models}


def _load_raw(explicit_path: str | None) -> dict[str, Any]:
    if explicit_path:
        return json.loads(Path(explicit_path).read_text(encoding="utf-8"))
    if _PACKAGED.exists():
        return json.loads(_PACKAGED.read_text(encoding="utf-8"))
    # Dev fallback: transform infra/models.json if running from a source checkout.
    infra = Path(__file__).resolve().parents[4] / "infra" / "models.json"
    if infra.exists():
        return _transform_infra_models(json.loads(infra.read_text(encoding="utf-8")))
    raise FileNotFoundError("No model catalog found (packaged or infra).")


@lru_cache
def load_catalog(explicit_path: str | None = None) -> ModelCatalog:
    raw = _load_raw(explicit_path)
    return ModelCatalog(models=raw["models"])
