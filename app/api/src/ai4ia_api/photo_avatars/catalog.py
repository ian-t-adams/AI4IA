"""Typed loader for the ``photoAvatars`` block of the packaged voice catalog.

``infra/voice-providers.json`` is the source; ``scripts/gen-voice-provider-catalog.py``
validates it (including the home region against ``infra/models.json``) and
packages it beside the voice providers. The voice provider loader ignores this
block, and this loader reads only it.

Provider-shaped values here -- the api-version, the Limited Access feature
name, the attribute values and the preview host -- are observations of the
Foundry portal's undocumented creation surface, pinned for review.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..catalog import DeploymentOption

_PACKAGED = Path(__file__).resolve().parents[1] / "data" / "voice_provider_catalog.json"

AttributeName = Literal["gender", "age", "ethnicity", "style"]
ATTRIBUTE_NAMES: tuple[AttributeName, ...] = ("gender", "age", "ethnicity", "style")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PhotoAvatarAttributeCatalog(_Frozen):
    gender: tuple[str, ...] = Field(min_length=1)
    age: tuple[str, ...] = Field(min_length=1)
    ethnicity: tuple[str, ...] = Field(min_length=1)
    style: tuple[str, ...] = Field(min_length=1)

    def options(self, name: AttributeName) -> tuple[str, ...]:
        return getattr(self, name)


class PhotoAvatarPreviewCatalog(_Frozen):
    # One exact provider storage host. Anything else is refused before DNS.
    host: str = Field(pattern=r"^[a-z0-9]{3,24}\.blob\.core\.windows\.net$")
    maxBytes: int = Field(ge=1024, le=16 * 1024 * 1024)
    maxDimension: int = Field(ge=64, le=8192)
    contentTypes: tuple[Literal["application/octet-stream", "image/png"], ...] = Field(min_length=1)


class PhotoAvatarCatalog(_Frozen):
    displayName: str = Field(min_length=1)
    description: str = Field(min_length=1)
    homeRegion: str = Field(pattern=r"^[a-z][a-z0-9]{1,30}$")
    homeDataZone: Literal["US", "EU"]
    apiVersion: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(-preview)?$")
    requiredFeature: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9]{1,63}$")
    projectSuffix: Literal["_PhotoAvatar"]
    baseModel: str = Field(pattern=r"^[a-z][a-z0-9.-]{1,31}$")
    billingModelId: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    promptMaxChars: int = Field(ge=1, le=4000)
    attributes: PhotoAvatarAttributeCatalog
    preview: PhotoAvatarPreviewCatalog

    def satisfies_residency(self, policy: str) -> bool:
        """Whether regional processing in the home account meets ``policy``.

        Avatar creation runs in the home account's region, the same guarantee as
        a regional ``Standard`` deployment, so the catalog's residency rules are
        reused rather than restated.
        """
        return DeploymentOption(
            region=self.homeRegion,
            dataZone=self.homeDataZone,
            sku="Standard",
            deploymentName="photo-avatar",
        ).satisfies(policy)


@lru_cache
def load_photo_avatar_catalog(explicit_path: str | None = None) -> PhotoAvatarCatalog:
    path = Path(explicit_path) if explicit_path else _PACKAGED
    raw = json.loads(path.read_text(encoding="utf-8"))
    block = raw.get("photoAvatars") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        raise ValueError("The packaged voice catalog has no photoAvatars block.")
    return PhotoAvatarCatalog.model_validate(block)
