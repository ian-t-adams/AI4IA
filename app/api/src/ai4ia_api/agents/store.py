"""UserAgentStore protocol + in-memory implementation.

The store persists per-user agent records keyed by ``(userId, name)``. Reads are
single-partition point lookups on ``userId``; ``list`` returns one user's agents.
A missing record returns ``None`` and a missing container/transient error must
never break the chat path (the service composes user agents on top of the curated
catalog and fails open to curated-only on any store error).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .user_agents import UserAgent


@runtime_checkable
class UserAgentStore(Protocol):
    async def list(self, user_id: str) -> list[UserAgent]: ...

    async def get(self, user_id: str, name: str) -> UserAgent | None: ...

    async def put(self, agent: UserAgent) -> None: ...

    async def delete(self, user_id: str, name: str) -> None: ...

    async def close(self) -> None: ...


class InMemoryUserAgentStore:
    """Non-durable store for local/dev/tests."""

    def __init__(self) -> None:
        # userId -> name -> UserAgent
        self._by_user: dict[str, dict[str, UserAgent]] = {}

    async def list(self, user_id: str) -> list[UserAgent]:
        return list(self._by_user.get(user_id, {}).values())

    async def get(self, user_id: str, name: str) -> UserAgent | None:
        return self._by_user.get(user_id, {}).get(name)

    async def put(self, agent: UserAgent) -> None:
        self._by_user.setdefault(agent.userId, {})[agent.name] = agent

    async def delete(self, user_id: str, name: str) -> None:
        self._by_user.get(user_id, {}).pop(name, None)

    async def close(self) -> None:
        return None
