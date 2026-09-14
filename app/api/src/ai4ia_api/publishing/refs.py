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


class PublicationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: AssetVersionRef
    approvedProfileDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    effectiveSubsetDigest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    approvalDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: str = Field(min_length=1, max_length=32)
    scope: str = Field(min_length=1, max_length=64)
    narrowing: tuple[str, ...] = Field(default=(), max_length=8)
    exclusions: tuple[str, ...] = Field(default=(), max_length=8)
