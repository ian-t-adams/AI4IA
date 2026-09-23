"""Paired catalog fixtures for runtime-available and runtime-disabled video models.

The shipped catalog runtime-disables ``sora-2``, its only video model. Tests of the
working ``generate_video`` path therefore run against a copy of that SAME catalog
with only ``runtimeEnabled`` flipped back on, and their controls flip it off. Each
copy gets its own file path, so ``load_catalog``'s cache never shares (or leaks a
mutation of) one catalog object between tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import ai4ia_api

PACKAGED_CATALOG = Path(ai4ia_api.__file__).resolve().parent / "data" / "model_catalog.json"


def video_catalog_path(directory: Path, *, runtime_enabled: bool) -> str:
    raw = json.loads(PACKAGED_CATALOG.read_text(encoding="utf-8"))
    videos = [model for model in raw["models"] if model["category"] == "video"]
    assert videos, "the packaged catalog must still carry its video inventory"
    for model in videos:
        model["runtimeEnabled"] = runtime_enabled
    state = "enabled" if runtime_enabled else "disabled"
    path = directory / f"catalog-video-runtime-{state}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return str(path)
