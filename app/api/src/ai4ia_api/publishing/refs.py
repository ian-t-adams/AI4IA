"""Portable identity for an immutable published source version."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AssetVersionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["agent", "workflow"]
    ownerId: str = Field(min_length=1, max_length=256)
    assetId: str = Field(pattern=r"^[0-9a-f]{32}$")
    version: int = Field(ge=1, le=2**31 - 1, strict=True)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def publication_handle(asset_id: str) -> str:
    # 112 bits fit the existing 32-character mention grammar; collision checks
    # remain mandatory rather than treating a truncated hash as authority.
    return f"pub.{asset_id[:28]}"
