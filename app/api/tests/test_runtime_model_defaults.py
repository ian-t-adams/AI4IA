"""Retained infrastructure cannot become an implicit runtime capability default."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from ai4ia_api.catalog import DeploymentOption, ModelCatalog, ModelEntry
from ai4ia_api.images.service import ImageGenerationError, ImageGenerationService
from ai4ia_api.routers.voice import _resolve_model
from ai4ia_api.sessions.models import ImageGenerationPreferences
from ai4ia_api.videos.service import VideoGenerationError
from tests.test_image_tool import _build_capability as image_capability, _client as image_client
from tests.test_video_tool import (
    _build_capability as video_capability,
    _client as video_client,
    _service as video_service,
)


def _catalog(category: str) -> ModelCatalog:
    return ModelCatalog(models=[
        ModelEntry(
            id=f"{category}-{index}", displayName=f"{category} {index}", category=category,
            format="OpenAI", runtimeEnabled=index == 2,
            imageSizes=["1024x1024"] if category == "image" else None,
            options=[DeploymentOption(
                region="eastus2", dataZone="US", sku="GlobalStandard",
                deploymentName=f"{category}-{index}-deployment",
            )],
        )
        for index in (1, 2)
    ])


@pytest.mark.parametrize("category", ["tts", "transcription"])
def test_implicit_voice_default_skips_disabled_inventory_without_replacing_explicit_picks(category):
    catalog = _catalog(category)
    first, second = catalog.models
    assert _resolve_model(catalog, None, categories={category}, kind=category)[0] == second.id
    with pytest.raises(HTTPException) as denied:
        _resolve_model(catalog, first.id, categories={category}, kind=category)
    assert denied.value.status_code == 400
    first.runtimeEnabled = True
    assert _resolve_model(catalog, None, categories={category}, kind=category)[0] == first.id


@pytest.mark.parametrize("category", ["image", "video"])
def test_media_service_defaults_and_tool_advertisements_share_runtime_availability(category):
    client = image_client() if category == "image" else video_client()
    try:
        catalog = _catalog(category)
        first, second = catalog.models
        client.app.state.catalog = catalog
        if category == "image":
            service = ImageGenerationService(
                settings=client.app.state.settings, catalog=catalog, gateway=client.app.state.gateway,
            )
            build, error = image_capability, ImageGenerationError
        else:
            service = video_service(client)
            build, error = video_capability, VideoGenerationError
        tools, _ = build(client, "owner", [])
        assert first.id not in tools[0]["function"]["description"]
        assert second.id in tools[0]["function"]["description"]

        with pytest.raises(error):
            asyncio.run(service.generate(prompt="Synthetic fixture.", model=first.id, size=None))
        assert client.app.state.gateway.calls == []
        generated = asyncio.run(service.generate(prompt="Synthetic fixture.", model=None, size=None))
        assert generated.model_id == second.id
        assert client.app.state.gateway.calls[0]["deployment"] == second.options[0].deploymentName

        first.runtimeEnabled = True
        tools, _ = build(client, "owner", [])
        assert first.id in tools[0]["function"]["description"]
        start = len(client.app.state.gateway.calls)
        generated = asyncio.run(service.generate(prompt="Synthetic fixture.", model=None, size=None))
        assert generated.model_id == first.id
        assert client.app.state.gateway.calls[start]["deployment"] == first.options[0].deploymentName
    finally:
        client.__exit__(None, None, None)


def test_unavailable_image_preferences_request_user_choice_instead_of_advertising_them():
    client = image_client()
    try:
        catalog = _catalog("image")
        first = catalog.models[0]
        client.app.state.catalog = catalog
        preferences = ImageGenerationPreferences(models=[first.id])
        tools, _ = image_capability(client, "owner", [], preferences)
        description = tools[0]["function"]["description"]
        assert first.id not in description
        assert "unavailable image model selections" in description
        assert preferences.models == [first.id]
        first.runtimeEnabled = True
        tools, _ = image_capability(client, "owner", [], preferences)
        assert f"currently selects {first.id}" in tools[0]["function"]["description"]
    finally:
        client.__exit__(None, None, None)
