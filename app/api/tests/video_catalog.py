"""Paired catalog fixtures for runtime-available and runtime-disabled video models.

The shipped catalog has no video model. Retained generic video and artifact
controls use synthetic deployment metadata; no removed deployment is re-enabled.
Each copy gets its own path so the catalog cache cannot leak mutations.
"""
from __future__ import annotations

import json
from pathlib import Path

import ai4ia_api

PACKAGED_CATALOG = Path(ai4ia_api.__file__).resolve().parent / "data" / "model_catalog.json"


def video_catalog_path(directory: Path, *, runtime_enabled: bool) -> str:
    raw = json.loads(PACKAGED_CATALOG.read_text(encoding="utf-8"))
    videos = [model for model in raw["models"] if model["category"] == "video"]
    if not videos:
        videos = [{
            "id": "sora-2", "displayName": "Synthetic video fixture",
            "format": "OpenAI", "category": "video", "api": "chat",
            "options": [{
                "deploymentName": "synthetic-video-eastus2", "region": "eastus2",
                "dataZone": "US", "sku": "GlobalStandard", "modelVersion": "2025-12-08",
            }],
        }]
        raw["models"].extend(videos)
    for model in videos:
        model["runtimeEnabled"] = runtime_enabled
    state = "enabled" if runtime_enabled else "disabled"
    path = directory / f"catalog-video-runtime-{state}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return str(path)
