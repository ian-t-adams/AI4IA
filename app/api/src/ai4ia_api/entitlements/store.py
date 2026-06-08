"""EntitlementStore protocol.

The store holds only *overrides*: a user with no document uses the (unlimited)
default entitlement composed from settings. Reads are point lookups on the
partition key (``userId``), so they are cheap; the service additionally caches
them behind a short TTL so the unlimited-default hot path never hits the store
twice in quick succession.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Entitlement


@runtime_checkable
class EntitlementStore(Protocol):
    async def get(self, user_id: str) -> Entitlement | None: ...

    async def put(self, entitlement: Entitlement) -> None: ...

    async def delete(self, user_id: str) -> None: ...

    async def list(self) -> list[Entitlement]: ...

    async def close(self) -> None: ...
