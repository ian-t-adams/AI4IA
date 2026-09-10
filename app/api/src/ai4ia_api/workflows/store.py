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
from ..publishing.store import (
    InMemoryRecordStore, RecordStore, delete_definition, replace_definition,
)
from .record_types import WORKFLOW_DEFINITION_KIND, is_definition


@runtime_checkable
class WorkflowStore(Protocol):
    @property
    def records(self) -> RecordStore: ...

    async def list(self, user_id: str) -> list[Workflow]: ...

    async def get(self, user_id: str, name: str) -> Workflow | None: ...

    async def put(self, workflow: Workflow) -> None: ...
    async def create_if_absent(self, workflow: Workflow) -> bool: ...
    async def replace_if_revision(self, workflow: Workflow, expected_revision: int) -> bool: ...

    async def delete(self, user_id: str, name: str) -> None: ...

    async def close(self) -> None: ...


class InMemoryWorkflowStore:
    """Non-durable store for local/dev/tests."""

    def __init__(self) -> None:
        self.records = InMemoryRecordStore()

    async def list(self, user_id: str) -> list[Workflow]:
        return [
            Workflow.model_validate(body) for body in self.records.definitions(user_id)
            if is_definition(body, user_id=user_id, kind=WORKFLOW_DEFINITION_KIND)
        ]

    async def get(self, user_id: str, name: str) -> Workflow | None:
        item = await self.records.read(user_id, name)
        if item is None or not is_definition(
            item.body, user_id=user_id, kind=WORKFLOW_DEFINITION_KIND, name=name,
        ):
            return None
        return Workflow.model_validate(item.body)

    async def put(self, workflow: Workflow) -> None:
        await self.records.put(workflow.userId, workflow.model_dump(mode="json"))

    async def create_if_absent(self, workflow: Workflow) -> bool:
        return await replace_definition(
            self.records, workflow.model_dump(mode="json"), expected_revision=None, create=True,
        )

    async def replace_if_revision(self, workflow: Workflow, expected_revision: int) -> bool:
        return await replace_definition(
            self.records, workflow.model_dump(mode="json"), expected_revision=expected_revision,
        )

    async def delete(self, user_id: str, name: str) -> None:
        from .models import WorkflowConflictError

        if await self.get(user_id, name) is not None and not await delete_definition(
            self.records, user_id, name,
        ):
            raise WorkflowConflictError("Workflow changed before deletion; refresh and retry.")

    async def close(self) -> None:
        return None
