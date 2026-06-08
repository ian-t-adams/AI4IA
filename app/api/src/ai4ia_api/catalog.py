"""Loads the curated model catalog and resolves model -> deployment routing.

Precedence: explicit path (settings/env) -> packaged ``data/model_catalog.json``
-> repo ``infra/models.json`` fallback (dev only, transformed on the fly).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_PACKAGED = Path(__file__).resolve().parent / "data" / "model_catalog.json"


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
    options: list[DeploymentOption]


class ModelCatalog(BaseModel):
    models: list[ModelEntry]

    def get(self, model_id: str) -> ModelEntry | None:
        return next((m for m in self.models if m.id == model_id), None)

    def resolve_deployment(
        self, model_id: str, *, region: str | None = None, data_zone: str | None = None
    ) -> DeploymentOption | None:
        """Pick a deployment for a model, optionally honoring region/data-zone."""
        entry = self.get(model_id)
        if entry is None or not entry.options:
            return None
        if region:
            match = next((o for o in entry.options if o.region == region), None)
            if match:
                return match
        if data_zone:
            match = next((o for o in entry.options if o.dataZone == data_zone), None)
            if match:
                return match
        return entry.options[0]


def _transform_infra_models(raw: dict[str, Any]) -> dict[str, Any]:
    naming = raw["naming"]
    sku_short = naming["skuShort"]
    regions = raw.get("regions", {})
    token = "slurmfactory"
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
