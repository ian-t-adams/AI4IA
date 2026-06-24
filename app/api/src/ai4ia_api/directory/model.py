"""User directory data model.

One document per user, keyed by the existing hashed internal ``userId`` (the
Cosmos partition key). ``id`` is the fixed constant ``profile`` so each user's
partition holds exactly one profile document, making reads/writes single-partition
point operations. ``displayName``/``email`` come straight from the token claims.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

# Fixed document id within each user's partition (one profile per userId).
PROFILE_ID = "profile"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserDirectoryEntry(BaseModel):
    """An admin-only mapping from the hashed internal ``userId`` to the user's
    display name + email, as last seen on their token."""

    # ``id`` is constant so (id, userId) is the single profile doc per partition.
    id: str = PROFILE_ID
    userId: str
    displayName: str | None = None
    email: str | None = None
    updatedAt: datetime = Field(default_factory=_now)

    @classmethod
    def build(
        cls, user_id: str, display_name: str | None, email: str | None
    ) -> "UserDirectoryEntry":
        return cls(
            id=PROFILE_ID,
            userId=user_id,
            displayName=display_name,
            email=email,
            updatedAt=_now(),
        )
