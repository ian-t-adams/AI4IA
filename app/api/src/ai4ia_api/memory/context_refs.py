"""Content/version identities for memory already supplied to a durable turn."""
from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field

from .models import MemoryRecord
from .preferences import MemoryPreference


class MemoryReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=0, strict=True)
    epoch: int = Field(ge=0, strict=True)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_record(cls, record: MemoryRecord) -> MemoryReference:
        return cls(
            id=record.id, version=record.version, epoch=record.write_epoch,
            digest=hashlib.sha256(record.text.encode("utf-8")).hexdigest(),
        )


class MemoryContextBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preference: MemoryPreference
    references: list[MemoryReference] = Field(min_length=1, max_length=128)
