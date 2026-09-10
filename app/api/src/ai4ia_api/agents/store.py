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
from ..publishing.store import (
    InMemoryRecordStore, RecordStore, delete_definition, replace_definition,
)
from ..workflows.record_types import AGENT_DEFINITION_KIND, is_definition


@runtime_checkable
class UserAgentStore(Protocol):
    @property
    def records(self) -> RecordStore: ...

    async def list(self, user_id: str) -> list[UserAgent]: ...

    async def get(self, user_id: str, name: str) -> UserAgent | None: ...

    async def put(self, agent: UserAgent) -> None: ...
    async def create_if_absent(self, agent: UserAgent) -> bool: ...
    async def replace_if_revision(self, agent: UserAgent, expected_revision: int) -> bool: ...

    async def delete(self, user_id: str, name: str) -> None: ...

    async def close(self) -> None: ...


class InMemoryUserAgentStore:
    """Non-durable store for local/dev/tests."""

    def __init__(self) -> None:
        self.records = InMemoryRecordStore()

    async def list(self, user_id: str) -> list[UserAgent]:
        return [
            UserAgent.model_validate(body) for body in self.records.definitions(user_id)
            if is_definition(body, user_id=user_id, kind=AGENT_DEFINITION_KIND)
        ]

    async def get(self, user_id: str, name: str) -> UserAgent | None:
        item = await self.records.read(user_id, name)
        if item is None or not is_definition(
            item.body, user_id=user_id, kind=AGENT_DEFINITION_KIND, name=name,
        ):
            return None
        return UserAgent.model_validate(item.body)

    async def put(self, agent: UserAgent) -> None:
        await self.records.put(agent.userId, agent.model_dump(mode="json"))

    async def create_if_absent(self, agent: UserAgent) -> bool:
        return await replace_definition(
            self.records, agent.model_dump(mode="json"), expected_revision=None, create=True,
        )

    async def replace_if_revision(self, agent: UserAgent, expected_revision: int) -> bool:
        return await replace_definition(
            self.records, agent.model_dump(mode="json"), expected_revision=expected_revision,
        )

    async def delete(self, user_id: str, name: str) -> None:
        from .user_agents import AgentConflictError

        if await self.get(user_id, name) is not None and not await delete_definition(
            self.records, user_id, name,
        ):
            raise AgentConflictError("Agent changed before deletion; refresh and retry.")

    async def close(self) -> None:
        return None
