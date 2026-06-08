"""WorkflowStore protocol + in-memory implementation.

The store persists per-user workflow records keyed by ``(userId, name)``. Reads
are single-partition point lookups on ``userId``; ``list`` returns one user's
workflows. A missing record returns ``None``. Unlike the agent store, workflow
reads are **not** on the chat hot path (they're only used by the explicit
``/api/workflows`` endpoints), so the service does not fail open — a store error
surfaces to the caller rather than being masked.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Workflow


@runtime_checkable
class WorkflowStore(Protocol):
    async def list(self, user_id: str) -> list[Workflow]: ...

    async def get(self, user_id: str, name: str) -> Workflow | None: ...

    async def put(self, workflow: Workflow) -> None: ...

    async def delete(self, user_id: str, name: str) -> None: ...

    async def close(self) -> None: ...


class InMemoryWorkflowStore:
    """Non-durable store for local/dev/tests."""

    def __init__(self) -> None:
        # userId -> name -> Workflow
        self._by_user: dict[str, dict[str, Workflow]] = {}

    async def list(self, user_id: str) -> list[Workflow]:
        return list(self._by_user.get(user_id, {}).values())

    async def get(self, user_id: str, name: str) -> Workflow | None:
        return self._by_user.get(user_id, {}).get(name)

    async def put(self, workflow: Workflow) -> None:
        self._by_user.setdefault(workflow.userId, {})[workflow.name] = workflow

    async def delete(self, user_id: str, name: str) -> None:
        self._by_user.get(user_id, {}).pop(name, None)

    async def close(self) -> None:
        return None
