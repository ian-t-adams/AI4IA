"""In-memory entitlement override store (local/dev/tests; not durable)."""
from __future__ import annotations

from .models import Entitlement


class InMemoryEntitlementStore:
    def __init__(self) -> None:
        self._by_user: dict[str, Entitlement] = {}

    async def get(self, user_id: str) -> Entitlement | None:
        return self._by_user.get(user_id)

    async def put(self, entitlement: Entitlement) -> None:
        self._by_user[entitlement.userId] = entitlement

    async def delete(self, user_id: str) -> None:
        self._by_user.pop(user_id, None)

    async def list(self) -> list[Entitlement]:
        return list(self._by_user.values())

    async def close(self) -> None:
        return None
