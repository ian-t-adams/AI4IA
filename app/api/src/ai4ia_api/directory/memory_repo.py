"""In-memory user directory for local/dev/tests (not durable across restarts)."""
from __future__ import annotations

from collections.abc import Iterable

from .model import UserDirectoryEntry


class InMemoryUserDirectoryRepository:
    def __init__(self) -> None:
        self._by_user: dict[str, UserDirectoryEntry] = {}

    async def upsert(self, entry: UserDirectoryEntry) -> None:
        self._by_user[entry.userId] = entry

    async def resolve(
        self, user_ids: Iterable[str]
    ) -> dict[str, UserDirectoryEntry]:
        out: dict[str, UserDirectoryEntry] = {}
        for uid in set(user_ids):
            entry = self._by_user.get(uid)
            if entry is not None:
                out[uid] = entry
        return out

    async def close(self) -> None:
        return None
